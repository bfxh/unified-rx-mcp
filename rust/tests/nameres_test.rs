//! nameres（S107）契约测试：十条作用域规则逐条锁定。
//! 口径见 spec/NAMERES.md §三；推导式可见性以**运行时实测**为准
//! （3.14 实测 `[n for n in ...]` 之后 `n` 是 NameError → 推导式独立作用域）。

use rxrs::json::Value;
use rxrs::nameres;

fn resolve(src: &str) -> Value {
    let v = nameres::resolve_file("t.py", src);
    assert!(v.get("error").is_none(), "不应报错: {:?}", v);
    v
}

fn arr<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    match v.get(k) {
        Some(Value::Arr(a)) => a,
        other => panic!("{} 应为数组，实得 {:?}", k, other),
    }
}

/// 找 (line, name) 的边；返回 (kind, to_line)。
fn edge(v: &Value, line: i128, name: &str) -> Option<(String, i128)> {
    for e in arr(v, "edges") {
        let l = match e.get("line") {
            Some(Value::Int(i)) => *i,
            _ => continue,
        };
        let n = match e.get("name") {
            Some(Value::Str(s)) => s.as_str(),
            _ => continue,
        };
        if l == line && n == name {
            let kind = match e.get("kind") {
                Some(Value::Str(s)) => s.clone(),
                _ => String::new(),
            };
            let to = match e.get("to_line") {
                Some(Value::Int(i)) => *i,
                _ => 0,
            };
            return Some((kind, to));
        }
    }
    None
}

fn unresolved_has(v: &Value, line: i128, name: &str) -> bool {
    arr(v, "unresolved").iter().any(|u| {
        matches!(u.get("line"), Some(Value::Int(i)) if *i == line)
            && matches!(u.get("name"), Some(Value::Str(s)) if s == name)
    })
}

#[test]
fn local_param_and_module_const() {
    let v = resolve("X = 1\ndef f(a):\n    return a + X\n");
    assert_eq!(edge(&v, 3, "a"), Some(("local".into(), 2)));
    assert_eq!(edge(&v, 3, "X"), Some(("module".into(), 1)));
}

#[test]
fn builtin_marked() {
    let v = resolve("print(1)\n");
    assert_eq!(edge(&v, 1, "print"), Some(("builtin".into(), 0)));
}

#[test]
fn class_body_not_closure() {
    // 规则 1：方法内看不到类体变量
    let v = resolve("class C:\n    attr = 2\n    def m(self):\n        return attr\n");
    assert!(unresolved_has(&v, 4, "attr"), "类体变量不得被方法解析到: {:?}", v);
    assert!(arr(&v, "bindings").iter().any(|b|
        matches!(b.get("scope"), Some(Value::Str(s)) if s == "C.m")
        && matches!(b.get("name"), Some(Value::Str(s)) if s == "self")), "self 应绑定在 C.m");
}

#[test]
fn comprehension_has_own_scope_and_first_iter_outside() {
    // 规则 2：推导式独立作用域；首个 iter 在外层求值
    let v = resolve("src = [1]\nout = [n for n in src]\nafter = n\n");
    assert_eq!(edge(&v, 2, "src"), Some(("module".into(), 1)), "首个 iter 按外层解析");
    assert_eq!(edge(&v, 2, "n"), Some(("local".into(), 2)), "目标绑定在推导式作用域");
    assert!(unresolved_has(&v, 3, "n"), "推导式变量在外部不可见（运行时 NameError 实测）");
}

#[test]
fn generator_exp_own_scope() {
    let v = resolve("g = (m for m in range(3))\nafter = m\n");
    assert!(unresolved_has(&v, 2, "m"), "genexp 变量不外泄");
}

#[test]
fn lambda_params_local() {
    let v = resolve("B = 1\nf = lambda a: a + B\n");
    assert_eq!(edge(&v, 2, "a"), Some(("local".into(), 2)));
    assert_eq!(edge(&v, 2, "B"), Some(("module".into(), 1)));
}

#[test]
fn global_declaration_resolves_at_module() {
    let v = resolve("x = 1\ndef f():\n    global x\n    return x\n");
    assert_eq!(edge(&v, 4, "x"), Some(("module".into(), 1)));
}

#[test]
fn nonlocal_resolves_in_enclosing_function() {
    let v = resolve(
        "def outer():\n    v = 1\n    def inner():\n        nonlocal v\n        return v\n    return inner\n");
    assert_eq!(edge(&v, 5, "v"), Some(("local".into(), 2)));
}

#[test]
fn except_as_binds_local() {
    // 模块级 try → 绑定属模块作用域；函数内 try → local
    let v = resolve("try:\n    pass\nexcept Exception as e:\n    print(e)\n");
    assert_eq!(edge(&v, 4, "e"), Some(("module".into(), 3)));
    let v2 = resolve("def f():\n    try:\n        pass\n    except Exception as e:\n        print(e)\n");
    assert_eq!(edge(&v2, 5, "e"), Some(("local".into(), 4)));
}

#[test]
fn default_value_evaluated_outside() {
    // 规则 6：默认值在定义处外层求值
    let v = resolve("V = 1\ndef f(a=V):\n    return a\n");
    assert_eq!(edge(&v, 2, "V"), Some(("module".into(), 1)));
}

#[test]
fn decorator_evaluated_outside() {
    let v = resolve("def deco(f):\n    return f\n\n@deco\ndef g():\n    pass\n");
    assert_eq!(edge(&v, 4, "deco"), Some(("module".into(), 1)));
}

#[test]
fn star_import_marked_unresolved() {
    let v = resolve("from os import *\n");
    assert!(unresolved_has(&v, 1, "*"));
    match v.get("stats").and_then(|s| s.get("star_import")) {
        Some(Value::Bool(true)) => {}
        other => panic!("stats.star_import 应为 true，实得 {:?}", other),
    }
}

#[test]
fn shadowing_takes_last_binding() {
    let v = resolve("x = 1\nx = 2\nprint(x)\n");
    assert_eq!(edge(&v, 3, "x"), Some(("module".into(), 2)));
}

#[test]
fn walrus_binds_enclosing_function() {
    // 海象绑定到最近的函数作用域。注：pyast 暂不支持"推导式元素内的 walrus"
    // （`[(y := i) for i in ...]`）——该缺口记在 spec/NAMERES.md §三 边界。
    let v = resolve("def f():\n    if (y := 1):\n        return y\n    return 0\n");
    assert_eq!(edge(&v, 3, "y"), Some(("local".into(), 2)));
    let v2 = resolve("def g():\n    (z := 2)\n    return z\n");
    assert_eq!(edge(&v2, 3, "z"), Some(("local".into(), 2)));
}

#[test]
fn import_forms_bind_correctly() {
    let v = resolve("import os.path\nfrom collections import defaultdict as dd\nprint(os, dd)\n");
    assert_eq!(edge(&v, 3, "os"), Some(("module".into(), 1)));
    assert_eq!(edge(&v, 3, "dd"), Some(("module".into(), 2)));
}

#[test]
fn attribute_counts_and_base_resolved() {
    let v = resolve("import os\nos.path.join(\"a\")\n");
    assert_eq!(edge(&v, 2, "os"), Some(("module".into(), 1)));
    let attrs = match v.get("stats").and_then(|s| s.get("attr_accesses")) {
        Some(Value::Int(i)) => *i,
        other => panic!("attr_accesses 应为 Int，实得 {:?}", other),
    };
    assert!(attrs >= 2, "os.path.join 应计 2 次属性访问，实得 {}", attrs);
}

#[test]
fn match_capture_binds_local() {
    let v = resolve("match 1:\n    case [a]:\n        print(a)\n");
    assert_eq!(edge(&v, 3, "a"), Some(("module".into(), 2)),
               "模块级 match 的捕获名属模块作用域");
    let v2 = resolve("def f():\n    match 1:\n        case [a]:\n            print(a)\n");
    assert_eq!(edge(&v2, 4, "a"), Some(("local".into(), 3)));
}

#[test]
fn for_and_with_targets_bind() {
    let v = resolve("for i in range(3):\n    print(i)\nwith open(\"x\") as fh:\n    print(fh)\n");
    assert_eq!(edge(&v, 2, "i"), Some(("module".into(), 1)));
    assert_eq!(edge(&v, 4, "fh"), Some(("module".into(), 3)));
    assert_eq!(edge(&v, 3, "open"), Some(("builtin".into(), 0)));
}

#[test]
fn annassign_binds_module() {
    let v = resolve("X: int = 1\nprint(X)\n");
    assert_eq!(edge(&v, 2, "X"), Some(("module".into(), 1)));
}

#[test]
fn syntax_error_is_reported() {
    let v = nameres::resolve_file("t.py", "def f(:\n");
    assert!(v.get("error").is_some(), "语法错误应报 error: {:?}", v);
}

#[test]
fn bindings_scope_paths() {
    let v = resolve(
        "def outer():\n    x = 1\n    def inner():\n        y = x\n        return y\n    return inner\n");
    let bs = arr(&v, "bindings");
    assert!(bs.iter().any(|b| matches!(b.get("scope"), Some(Value::Str(s)) if s == "outer")
        && matches!(b.get("name"), Some(Value::Str(s)) if s == "x")), "{:?}", bs);
    assert!(bs.iter().any(|b| matches!(b.get("scope"), Some(Value::Str(s)) if s == "outer.inner")
        && matches!(b.get("name"), Some(Value::Str(s)) if s == "y")),
            "嵌套作用域路径应可区分: {:?}", bs);
    assert!(bs.iter().any(|b| matches!(b.get("scope"), Some(Value::Str(s)) if s == "outer")
        && matches!(b.get("name"), Some(Value::Str(s)) if s == "inner")), "{:?}", bs);
}
