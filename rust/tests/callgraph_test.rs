//! callgraph（S125）契约测试：调用边解析 + 跨文件 stitch + 未解析如实分类。
//! 口径见 spec/CALLGRAPH.md：同一作用域引擎（nameres）+ 预扫描种子（前向引用）。
//! 锁定行为：可解析路径逐条断言；不可解析路径按 reason 分类锁定（含文档化边界）。

use rxrs::json::Value;
use rxrs::nameres;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-callgraph-test-{}-{}", tag, n));
        fs::create_dir_all(&p).unwrap();
        TempDir(p)
    }
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn write_rel(root: &Path, rel: &str, content: &str) {
    let p = root.join(rel);
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(&p, content).unwrap();
}

fn arr<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    match v.get(k) {
        Some(Value::Arr(a)) => a,
        other => panic!("{} 应为数组，实得 {:?}", k, other),
    }
}

fn s(v: &Value, k: &str) -> String {
    match v.get(k) {
        Some(Value::Str(x)) => x.clone(),
        _ => String::new(),
    }
}

fn i(v: &Value, k: &str) -> i128 {
    match v.get(k) {
        Some(Value::Int(x)) => *x,
        _ => 0,
    }
}

/// 找 (file, line) 的调用边；返回 (caller, callee, kind)。
fn edge(v: &Value, file: &str, line: i128) -> Option<(String, String, String)> {
    for e in arr(v, "edges") {
        if s(e, "file") == file && i(e, "line") == line {
            return Some((s(e, "caller"), s(e, "callee"), s(e, "kind")));
        }
    }
    None
}

/// 找 (file, line, expr) 的未解析条目；返回 reason。
fn unres(v: &Value, file: &str, line: i128, expr: &str) -> Option<String> {
    for u in arr(v, "unresolved") {
        if s(u, "file") == file && i(u, "line") == line && s(u, "expr") == expr {
            return Some(s(u, "reason"));
        }
    }
    None
}

fn stat(v: &Value, k: &str) -> i128 {
    match v.get("stats").and_then(|st| st.get(k)) {
        Some(Value::Int(x)) => *x,
        other => panic!("stats.{} 应为 Int，实得 {:?}", k, other),
    }
}

fn by_reason(v: &Value, k: &str) -> i128 {
    match v.get("stats").and_then(|st| st.get("by_reason")).and_then(|b| b.get(k)) {
        Some(Value::Int(x)) => *x,
        None => 0,
        other => panic!("stats.by_reason.{} 应为 Int，实得 {:?}", k, other),
    }
}

#[test]
fn module_level_call_and_forward_reference() {
    // 前向引用（先调用后定义）靠预扫描种子解析——互递归/主调用在下的常态代码
    let td = TempDir::new("fwd");
    write_rel(td.path(), "t.py",
        "def a():\n    return b()\n\ndef b():\n    return 1\n\nx = a()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    // 函数体内调用：caller 应归属定义（全限定），callee 解析到后面的定义
    assert_eq!(edge(&v, "t.py", 2), Some(("t.a".into(), "t.b".into(), "name".into())), "{:?}", v);
    // 模块级调用：caller 为空串（模块作用域伪节点）
    assert_eq!(edge(&v, "t.py", 7), Some((String::new(), "t.a".into(), "name".into())), "{:?}", v);
    // 节点（def 定义点）
    assert!(arr(&v, "nodes").iter().any(|n| s(n, "qual") == "t.a" && i(n, "line") == 1));
    assert!(arr(&v, "nodes").iter().any(|n| s(n, "qual") == "t.b" && i(n, "line") == 4));
}

#[test]
fn mutual_recursion_both_directions() {
    let td = TempDir::new("mutual");
    write_rel(td.path(), "t.py",
        "def even(n):\n    return odd(n - 1)\n\ndef odd(n):\n    return even(n - 1)\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(edge(&v, "t.py", 2), Some(("t.even".into(), "t.odd".into(), "name".into())), "{:?}", v);
    assert_eq!(edge(&v, "t.py", 5), Some(("t.odd".into(), "t.even".into(), "name".into())), "{:?}", v);
}

#[test]
fn self_method_call_resolves_via_class_table() {
    // helper 定义在 m 之后（前向引用）——类作用域预种子解析
    let td = TempDir::new("selfm");
    write_rel(td.path(), "t.py",
        "class C:\n    def m(self):\n        return self.helper()\n\n    def helper(self):\n        return 1\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(edge(&v, "t.py", 3), Some(("t.C.m".into(), "t.C.helper".into(), "self_attr".into())), "{:?}", v);
    assert!(arr(&v, "nodes").iter().any(|n| s(n, "qual") == "t.C.helper" && s(n, "kind") == "method"));
}

#[test]
fn class_method_via_class_name() {
    let td = TempDir::new("clsm");
    write_rel(td.path(), "t.py",
        "class C:\n    def m(self):\n        return 1\n\ndef go():\n    return C.m(None)\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(edge(&v, "t.py", 6), Some(("t.go".into(), "t.C.m".into(), "module_attr".into())), "{:?}", v);
}

#[test]
fn self_attr_missing_is_honest() {
    // __init__ 里赋的实例属性静态看不见 → self_attr_missing（不猜）
    let td = TempDir::new("selfmiss");
    write_rel(td.path(), "t.py",
        "class C:\n    def m(self):\n        return self.nope()\n\n    def __init__(self):\n        self.handler = print\n\n    def n(self):\n        return self.handler()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(unres(&v, "t.py", 3, "self.nope"), Some("self_attr_missing".into()), "{:?}", v);
    assert_eq!(unres(&v, "t.py", 9, "self.handler"), Some("self_attr_missing".into()), "{:?}", v);
}

#[test]
fn cross_file_from_import_and_relative() {
    let td = TempDir::new("xfrom");
    write_rel(td.path(), "pkg/__init__.py", "");
    write_rel(td.path(), "pkg/mod_a.py", "def alpha():\n    return 1\n");
    write_rel(td.path(), "pkg/mod_b.py",
        "from .mod_a import alpha as al\n\ndef run():\n    return al()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(edge(&v, "pkg/mod_b.py", 4),
               Some(("pkg.mod_b.run".into(), "pkg.mod_a.alpha".into(), "from_import".into())), "{:?}", v);
    assert!(stat(&v, "stitched") >= 1);
}

#[test]
fn cross_file_submodule_member_and_documented_chain_boundary() {
    let td = TempDir::new("xsub");
    write_rel(td.path(), "pkg/__init__.py", "");
    write_rel(td.path(), "pkg/leaf.py", "def f():\n    return 1\n");
    write_rel(td.path(), "pkg/use.py",
        "from pkg import leaf\n\ndef go():\n    return leaf.f()\n");
    write_rel(td.path(), "pkg/use2.py",
        "import pkg.leaf\n\ndef go2():\n    return pkg.leaf.f()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    // `from pkg import leaf; leaf.f()` → 子模块回退解析
    assert_eq!(edge(&v, "pkg/use.py", 4),
               Some(("pkg.use.go".into(), "pkg.leaf.f".into(), "module_attr".into())), "{:?}", v);
    // `import pkg.leaf; pkg.leaf.f()` → 链式调用（文档化边界：静态不可解）
    assert_eq!(unres(&v, "pkg/use2.py", 4, "pkg.leaf.f"), Some("attr_chain".into()), "{:?}", v);
}

#[test]
fn external_builtin_and_chain_classification() {
    let td = TempDir::new("ext");
    write_rel(td.path(), "t.py",
        "import os\n\ndef f():\n    print(1)\n    os.path.join(\"a\")\n    return os.getcwd()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(stat(&v, "builtin_calls"), 1, "print 应计内建：{:?}", v);
    assert_eq!(unres(&v, "t.py", 5, "os.path.join"), Some("external".into()), "{:?}", v);
    assert_eq!(unres(&v, "t.py", 6, "os.getcwd"), Some("external".into()), "{:?}", v);
    assert_eq!(unres(&v, "t.py", 4, "print"), None, "内建不应入 unresolved");
}

#[test]
fn var_call_and_receiver_var() {
    let td = TempDir::new("var");
    write_rel(td.path(), "t.py",
        "import os\nh = os.getcwd\n\ndef f():\n    h()\n    h.attr()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(unres(&v, "t.py", 5, "h"), Some("var_call".into()), "{:?}", v);
    assert_eq!(unres(&v, "t.py", 6, "h.attr"), Some("receiver_var".into()), "{:?}", v);
}

#[test]
fn re_export_not_followed() {
    // `from pkg import f` 而 pkg/__init__.py 只是再导出 → re_export（与 NAMERES §九 同边界）
    let td = TempDir::new("re");
    write_rel(td.path(), "pkg/__init__.py", "from .leaf import f\n");
    write_rel(td.path(), "pkg/leaf.py", "def f():\n    return 1\n");
    write_rel(td.path(), "pkg/use.py", "from pkg import f\n\ndef go():\n    return f()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(unres(&v, "pkg/use.py", 4, "f"), Some("re_export".into()), "{:?}", v);
}

#[test]
fn nested_local_def_sequential_binding() {
    let td = TempDir::new("nested");
    write_rel(td.path(), "t.py",
        "def outer():\n    def inner():\n        return 1\n\n    return inner()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(edge(&v, "t.py", 5),
               Some(("t.outer".into(), "t.outer.inner".into(), "name".into())), "{:?}", v);
}

#[test]
fn local_mutual_recursion_is_documented_boundary() {
    // 函数体内互相调用的局部定义：顺序绑定，a 里调 b 时 b 尚未绑定 →
    // 如实未解析（文档化边界，不做函数体内预扫描）
    let td = TempDir::new("localmutual");
    write_rel(td.path(), "t.py",
        "def outer():\n    def a():\n        return b()\n\n    def b():\n        return a()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(unres(&v, "t.py", 3, "b"), Some("not_found".into()), "{:?}", v);
    // b 里调用 a：a 已绑定 → 正常解析
    assert_eq!(edge(&v, "t.py", 6),
               Some(("t.outer.b".into(), "t.outer.a".into(), "name".into())), "{:?}", v);
}

#[test]
fn star_import_marks_not_found_as_star_import() {
    let td = TempDir::new("star");
    write_rel(td.path(), "t.py", "from os import *\n\ndef f():\n    return getcwd2()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert_eq!(unres(&v, "t.py", 4, "getcwd2"), Some("star_import".into()), "{:?}", v);
}

#[test]
fn syntax_error_file_skipped() {
    let td = TempDir::new("bad");
    write_rel(td.path(), "good.py", "def f():\n    return 1\n");
    write_rel(td.path(), "bad.py", "def f(:\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    assert!(v.get("error").is_none(), "{:?}", v);
    assert_eq!(v.get("files"), Some(&Value::Int(1)), "语法错误文件应跳过: {:?}", v);
}

#[test]
fn deterministic_output() {
    let td = TempDir::new("det");
    write_rel(td.path(), "pkg/__init__.py", "");
    write_rel(td.path(), "pkg/a.py", "def f():\n    return g()\n\ndef g():\n    return 1\n");
    write_rel(td.path(), "pkg/b.py", "from .a import f\n\ndef run():\n    return f()\n");
    let j1 = nameres::callgraph_dir(td.path(), 100).to_json();
    let j2 = nameres::callgraph_dir(td.path(), 100).to_json();
    assert_eq!(j1, j2, "两次运行必须逐字节一致");
}

#[test]
fn resolution_stats_consistent() {
    // stats 口径自洽：calls = resolved + unresolved + builtin
    let td = TempDir::new("stats");
    write_rel(td.path(), "t.py",
        "import os\n\ndef a():\n    return b()\n\ndef b():\n    print(1)\n    os.getcwd()\n    return a()\n");
    let v = nameres::callgraph_dir(td.path(), 100);
    let calls = stat(&v, "calls");
    let resolved = stat(&v, "resolved");
    let unresolved = stat(&v, "unresolved");
    let builtin = stat(&v, "builtin_calls");
    assert_eq!(calls, resolved + unresolved + builtin,
               "calls={} resolved={} unresolved={} builtin={}", calls, resolved, unresolved, builtin);
    assert_eq!(by_reason(&v, "external"), 1, "{:?}", v);
}
