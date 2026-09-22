//! nameres —— 单文件名字解析（S107，spec/NAMERES.md 阶段 A）。
//!
//! 事实：每个 `Name(Load)` 解析到最近的绑定，边 kind ∈ {local, module, builtin}；
//! 解析不到者如实入 `unresolved`（不猜）。跨文件 import 拼接留给 S108。
//!
//! 作用域规则（NAMERES §三 十条，逐条落地）：
//! 1. class 体不构成闭包——从嵌套函数解析时跳过 class 作用域；
//! 2. 推导式独立作用域，但**首个 comprehension 的 iter 在外层求值**；
//! 3. lambda 同函数；
//! 4. global/nonlocal 声明改变上溯路径；
//! 5. except-as 目标按 local 记录（与 symtable 口径一致，fixture 锁死）；
//! 6. 参数默认值 / 装饰器 / 返回注解在**定义处外层**求值（本实现把 FunctionDef
//!    args 之后的非语句子节点一律按外层表达式处理——装饰器与注解都在其列）；
//! 7. del 不建模（静态解析不做控制流）；
//! 8. star import 记 `star_import` 未解析；
//! 9. 动态特性天然落入 unresolved（not_found）；
//! 10. 同名遮蔽取"最后一次绑定"（模块级简化，不做控制流）。
//!
//! 边界：不做类型推断/属性链/跨文件控制流（属性访问只解析基名，`attr_accesses`
//! 计入 stats）。定位是"比文本级准、比 LSP 轻"。

use std::collections::{HashMap, HashSet};
use std::path::Path;

use crate::bug::BUILTINS;
use crate::json::Value;
use crate::pyast::{parse_module, Ctx, PyNode};

#[derive(Clone, Copy, PartialEq)]
enum SK {
    Module,
    Func,
    Class,
    Comp,
}

struct Scope {
    kind: SK,
    label: String,
    bindings: HashMap<String, (usize, &'static str)>,
    globals: HashSet<String>,
    nonlocals: HashSet<String>,
}

impl Scope {
    fn new(kind: SK, label: &str) -> Scope {
        Scope { kind, label: label.to_string(), bindings: HashMap::new(),
                globals: HashSet::new(), nonlocals: HashSet::new() }
    }

    fn bind(&mut self, name: &str, line: usize, kind: &'static str) {
        if name.is_empty() {
            return;
        }
        // 规则 10：同名遮蔽取最后一次绑定（行号更大者覆盖）
        let e = self.bindings.entry(name.to_string()).or_insert((line, kind));
        if line >= e.0 {
            *e = (line, kind);
        }
    }
}

/// 一条 import 事实（S108 跨文件拼接用）：原样记录语法信息，解析在 resolve_dir。
struct ImportFact {
    line: usize,
    /// 相对导入层级（from ...m import x 的前导点数；import 语句恒 0）
    level: usize,
    /// 模块路径（`import a.b` → "a.b"；`from m import x` → m）
    module: String,
    /// from 导入的名字（import 语句为空）
    name: String,
    /// as 别名（无则空）
    asname: String,
    is_from: bool,
}

struct Resolver {
    file: String,
    /// 模块名（S125 调用图模式：qualname 前缀；旧路径为空串）
    modname: String,
    /// S125：调用图采集开关（节点/调用事实只在 callgraph 路径产出）
    collect: bool,
    scopes: Vec<Scope>,
    edges: Vec<Value>,
    unresolved: Vec<Value>,
    n_local: usize,
    n_module: usize,
    n_builtin: usize,
    n_unresolved: usize,
    n_attr: usize,
    star_import: bool,
    bindings: Vec<Value>,
    imports: Vec<ImportFact>,
    // ---------- S125 调用图字段 ----------
    /// 定义节点（qual/file/line/kind），collect 时产出
    nodes: Vec<Value>,
    /// 已解析调用边（同文件内解析完成；跨文件由 stitch 补齐）
    calls: Vec<Value>,
    /// 跨文件待定调用（目标模块 + 绑定名，stitch 阶段裁决）
    deferred: Vec<Value>,
    /// 不可解析调用（如实列出，不猜）
    calls_unresolved: Vec<Value>,
    n_calls: usize,
    n_builtin_calls: usize,
    /// 类作用域绑定表（label → 名字 → (行, 种类)）：self.m()/X.m() 的类方法解析用
    class_tables: HashMap<String, HashMap<String, (usize, &'static str)>>,
    /// 定义栈（标签路径，如 "C.m"；模块级为空）——调用者归属
    def_stack: Vec<String>,
}

impl Resolver {
    fn new(file: &str, modname: &str, collect: bool) -> Resolver {
        Resolver {
            file: file.to_string(),
            modname: modname.to_string(),
            collect,
            scopes: vec![Scope::new(SK::Module, "module")],
            edges: Vec::new(),
            unresolved: Vec::new(),
            n_local: 0,
            n_module: 0,
            n_builtin: 0,
            n_unresolved: 0,
            n_attr: 0,
            star_import: false,
            bindings: Vec::new(),
            imports: Vec::new(),
            nodes: Vec::new(),
            calls: Vec::new(),
            deferred: Vec::new(),
            calls_unresolved: Vec::new(),
            n_calls: 0,
            n_builtin_calls: 0,
            class_tables: HashMap::new(),
            def_stack: Vec::new(),
        }
    }

    /// 当前定义的全限定名（模块级为空串；与 callee 同为节点键口径）。
    fn cur_def(&self) -> String {
        match self.def_stack.last() {
            Some(l) if !self.modname.is_empty() => format!("{}.{}", self.modname, l),
            Some(l) => l.clone(),
            None => String::new(),
        }
    }

    /// 定义处限定名：modname + 标签路径 + 名字（模块级 = "mod.name"）。
    fn qual_at(&self, scope_idx: usize, name: &str) -> String {
        let label = &self.scopes[scope_idx].label;
        if label == "module" {
            format!("{}.{}", self.modname, name)
        } else if self.modname.is_empty() {
            format!("{}.{}", label, name)
        } else {
            format!("{}.{}.{}", self.modname, label, name)
        }
    }

    /// self/cls 形态检测：`base` 解析到某方法的参数，且该方法的父作用域是类。
    /// 返回类作用域标签（如 "C"）。
    fn self_like_class(&self, base: &str) -> Option<String> {
        let (idx, _l, bk) = self.resolve(base)?;
        if bk != "param" || idx == 0 {
            return None;
        }
        if self.scopes[idx].kind != SK::Func || self.scopes[idx - 1].kind != SK::Class {
            return None;
        }
        Some(self.scopes[idx - 1].label.clone())
    }

    /// 类标签定位：绑定作用域标签 + 名字（模块级 "C"；嵌套 "Outer.Inner"）。
    fn class_label_at(&self, scope_idx: usize, name: &str) -> String {
        let label = &self.scopes[scope_idx].label;
        if label == "module" {
            name.to_string()
        } else {
            format!("{}.{}", label, name)
        }
    }
}

/// 语句节点集合（用于区分"子节点是语句还是表达式"——FunctionDef 的装饰器/注解
/// 与 ClassDef 的 bases/kws 都以外层表达式处理）。
fn is_stmt(kind: &str) -> bool {
    matches!(kind,
        "FunctionDef" | "AsyncFunctionDef" | "ClassDef" | "Return" | "Delete"
        | "Assign" | "AugAssign" | "AnnAssign" | "For" | "AsyncFor" | "While"
        | "If" | "With" | "AsyncWith" | "Match" | "Raise" | "Try" | "TryStar"
        | "Assert" | "Import" | "ImportFrom" | "Global" | "Nonlocal" | "Expr"
        | "Pass" | "Break" | "Continue")
}

fn is_builtin(name: &str) -> bool {
    BUILTINS.contains(&name)
}

/// 调用表达式文本形态（限深 3 防深链爆炸）：Name/Attribute 链、Call 加 "()"、
/// Subscript 加 "[]"；其余按节点种类名。
fn expr_text(n: &PyNode) -> String {
    fn go(n: &PyNode, d: usize) -> String {
        if d == 0 {
            return n.kind.to_string();
        }
        match n.kind {
            "Name" => n.name.clone(),
            "Attribute" => match n.children.first() {
                Some(b) => format!("{}.{}", go(b, d - 1), n.name),
                None => n.name.clone(),
            },
            "Call" => match n.children.first() {
                Some(f) => format!("{}()", go(f, d - 1)),
                None => "()".into(),
            },
            "Subscript" => match n.children.first() {
                Some(b) => format!("{}[]", go(b, d - 1)),
                _ => "[]".into(),
            },
            _ => n.kind.to_string(),
        }
    }
    go(n, 3)
}

impl Resolver {
    fn cur(&mut self) -> &mut Scope {
        self.scopes.last_mut().expect("scope stack 非空")
    }

    /// 记录绑定（作用域路径 + 名字 + 行 + 种类），S108 跨文件与 symtable oracle 用。
    /// 子作用域标签：父标签 + "." + 名字（模块层直接名字）。
    fn child_label(&self, name: &str) -> String {
        let parent = self.scopes.last().map(|s| s.label.clone()).unwrap_or_default();
        if parent == "module" || parent.is_empty() {
            name.to_string()
        } else {
            format!("{}.{}", parent, name)
        }
    }

    fn bind(&mut self, name: &str, line: usize, kind: &'static str) {
        if name.is_empty() {
            return;
        }
        // global/nonlocal 声明：绑定落到声明的目标作用域（规则 4 的写侧）
        let n = self.scopes.len();
        let mut target = n - 1;
        for i in (0..n).rev() {
            if matches!(self.scopes[i].kind, SK::Func | SK::Comp) {
                if self.scopes[i].globals.contains(name) {
                    target = 0;
                } else if self.scopes[i].nonlocals.contains(name) {
                    for j in (0..i).rev() {
                        if self.scopes[j].kind == SK::Class {
                            continue;
                        }
                        target = j;
                        break;
                    }
                }
                break;
            }
        }
        let label = self.scopes[target].label.clone();
        let parent = if target > 0 {
            self.scopes[target - 1].label.clone()
        } else {
            String::new()
        };
        self.scopes[target].bind(name, line, kind);
        self.bindings.push(Value::Obj(vec![
            ("scope".into(), Value::Str(label)),
            ("parent".into(), Value::Str(parent)),
            ("name".into(), Value::Str(name.to_string())),
            ("line".into(), Value::Int(line as i128)),
            ("kind".into(), Value::Str(kind.to_string())),
        ]));
        // S125：调用图节点（def/class 才成节点；lambda/嵌套闭包按定义点计）
        if self.collect && (kind == "def" || kind == "class") {
            let node_kind = if kind == "class" {
                "class"
            } else if self.scopes[target].kind == SK::Class {
                "method"
            } else {
                "func"
            };
            let qual = self.qual_at(target, name);
            self.nodes.push(Value::Obj(vec![
                ("qual".into(), Value::Str(qual)),
                ("file".into(), Value::Str(self.file.clone())),
                ("line".into(), Value::Int(line as i128)),
                ("kind".into(), Value::Str(node_kind.into())),
            ]));
        }
    }

    /// 解析 `name` → (作用域下标, 绑定行, 绑定种类)。
    fn resolve(&self, name: &str) -> Option<(usize, usize, &'static str)> {
        let n = self.scopes.len();
        if n == 0 {
            return None;
        }
        // 就近函数类作用域（Func/Comp）的 global / nonlocal 声明优先
        let mut func_idx = None;
        for i in (0..n).rev() {
            if matches!(self.scopes[i].kind, SK::Func | SK::Comp) {
                func_idx = Some(i);
                break;
            }
        }
        if let Some(fi) = func_idx {
            if self.scopes[fi].globals.contains(name) {
                return self.scopes[0].bindings.get(name).map(|(l, k)| (0, *l, *k));
            }
            if self.scopes[fi].nonlocals.contains(name) {
                for i in (0..fi).rev() {
                    if self.scopes[i].kind == SK::Class {
                        continue;
                    }
                    if let Some((l, k)) = self.scopes[i].bindings.get(name) {
                        return Some((i, *l, *k));
                    }
                }
                return None;
            }
        }
        // 常规上溯：最内层含自身；再往外跳过 class 作用域（规则 1）
        for i in (0..n).rev() {
            let innermost = i == n - 1;
            if self.scopes[i].kind == SK::Class && !innermost {
                continue;
            }
            if let Some((l, k)) = self.scopes[i].bindings.get(name) {
                return Some((i, *l, *k));
            }
        }
        None
    }

    fn load(&mut self, name: &str, line: usize) {
        if name.is_empty() {
            return;
        }
        match self.resolve(name) {
            Some((idx, to_line, _bk)) => {
                let kind = if idx == 0 { "module" } else { "local" };
                match kind {
                    "module" => self.n_module += 1,
                    _ => self.n_local += 1,
                }
                self.edges.push(Value::Obj(vec![
                    ("file".into(), Value::Str(self.file.clone())),
                    ("line".into(), Value::Int(line as i128)),
                    ("name".into(), Value::Str(name.to_string())),
                    ("kind".into(), Value::Str(kind.to_string())),
                    ("to_line".into(), Value::Int(to_line as i128)),
                ]));
            }
            None => {
                if is_builtin(name) {
                    self.n_builtin += 1;
                    self.edges.push(Value::Obj(vec![
                        ("file".into(), Value::Str(self.file.clone())),
                        ("line".into(), Value::Int(line as i128)),
                        ("name".into(), Value::Str(name.to_string())),
                        ("kind".into(), Value::Str("builtin".into())),
                        ("to_line".into(), Value::Int(0)),
                    ]));
                } else {
                    self.n_unresolved += 1;
                    self.unresolved.push(Value::Obj(vec![
                        ("line".into(), Value::Int(line as i128)),
                        ("name".into(), Value::Str(name.to_string())),
                        ("reason".into(), Value::Str("not_found".into())),
                    ]));
                }
            }
        }
    }

    // ---------- 绑定位置 ----------

    /// 目标绑定：Name/Tuple/List/Starred 记绑定；Attribute/Subscript 只解析基名。
    fn bind_target(&mut self, n: &PyNode, kind: &'static str) {
        match n.kind {
            "Name" => {
                let (name, line) = (n.name.clone(), n.line);
                self.bind(&name, line, kind);
            }
            "Tuple" | "List" | "Starred" => {
                for c in &n.children {
                    self.bind_target(c, kind);
                }
            }
            "Attribute" => {
                if let Some(base) = n.children.first() {
                    self.expr(base);
                }
            }
            "Subscript" => {
                if let Some(base) = n.children.first() {
                    self.expr(base);
                }
                for c in n.children.iter().skip(1) {
                    self.expr(c);
                }
            }
            _ => {
                for c in &n.children {
                    self.expr(c);
                }
            }
        }
    }

    // ---------- 表达式 ----------

    fn expr(&mut self, n: &PyNode) {
        match n.kind {
            "Call" if self.collect => self.call(n),
            "Name" => {
                if matches!(n.ctx, Ctx::Load) {
                    let (name, line) = (n.name.clone(), n.line);
                    self.load(&name, line);
                }
            }
            "Attribute" => {
                self.n_attr += 1;
                if let Some(base) = n.children.first() {
                    self.expr(base);
                }
            }
            "Lambda" => self.lambda_(n),
            "ListComp" | "SetComp" | "GeneratorExp" | "DictComp" => self.comp(n),
            "NamedExpr" => {
                // 海象运算符：绑定到最近的函数/模块作用域（跳过推导式作用域）
                if let Some(t) = n.children.first()
                    && t.kind == "Name" {
                        let (name, line) = (t.name.clone(), t.line);
                        let mut target = 0usize;
                        for i in (0..self.scopes.len()).rev() {
                            if matches!(self.scopes[i].kind, SK::Module | SK::Func) {
                                target = i;
                                break;
                            }
                        }
                        self.scopes[target].bind(&name, line, "assign");
                    }
                for c in n.children.iter().skip(1) {
                    self.expr(c);
                }
            }
            _ => {
                for c in &n.children {
                    self.expr(c);
                }
            }
        }
    }

    // ---------- S125：调用点采集与解析 ----------

    fn call(&mut self, n: &PyNode) {
        self.n_calls += 1;
        let line = n.line;
        let caller = self.cur_def();
        if let Some(func) = n.children.first() {
            match func.kind {
                "Name" => self.call_name(func, line, &caller),
                "Attribute" => self.call_attr(func, line, &caller),
                _ => {
                    let expr = expr_text(func);
                    self.calls_unresolved.push(self.ures(line, &caller, &expr, "expr"));
                }
            }
        }
        // func 与实参照常下钻（Name(Load) 仍记 load 边，口径与旧路径一致）
        for c in &n.children {
            self.expr(c);
        }
    }

    fn ures(&self, line: usize, caller: &str, expr: &str, reason: &str) -> Value {
        Value::Obj(vec![
            ("file".into(), Value::Str(self.file.clone())),
            ("line".into(), Value::Int(line as i128)),
            ("caller".into(), Value::Str(caller.to_string())),
            ("expr".into(), Value::Str(expr.to_string())),
            ("reason".into(), Value::Str(reason.to_string())),
        ])
    }

    fn edge(&self, line: usize, caller: &str, callee: &str, to_file: &str,
            to_line: usize, kind: &str) -> Value {
        Value::Obj(vec![
            ("file".into(), Value::Str(self.file.clone())),
            ("line".into(), Value::Int(line as i128)),
            ("caller".into(), Value::Str(caller.to_string())),
            ("callee".into(), Value::Str(callee.to_string())),
            ("to_file".into(), Value::Str(to_file.to_string())),
            ("to_line".into(), Value::Int(to_line as i128)),
            ("kind".into(), Value::Str(kind.to_string())),
        ])
    }

    /// 裸名调用 `f()`：同文件解析完成，或转跨文件待定，或如实未解析。
    fn call_name(&mut self, f: &PyNode, line: usize, caller: &str) {
        let name = f.name.clone();
        match self.resolve(&name) {
            Some((idx, to_line, bk)) => match bk {
                "def" | "class" => {
                    let qual = self.qual_at(idx, &name);
                    let kind = if bk == "class" { "class" } else { "name" };
                    let to_file = self.file.clone();
                    self.calls.push(self.edge(line, caller, &qual, &to_file, to_line, kind));
                }
                "import" => self.defer_import(&name, line, caller, ""),
                _ => {
                    let (e, r) = (name.clone(), "var_call");
                    self.calls_unresolved.push(self.ures(line, caller, &e, r));
                }
            },
            None => {
                if is_builtin(&name) {
                    self.n_builtin_calls += 1;
                } else {
                    let reason = if self.star_import { "star_import" } else { "not_found" };
                    let expr = name.clone();
                    self.calls_unresolved.push(self.ures(line, caller, &expr, reason));
                }
            }
        }
    }

    /// 属性调用 `base.attr()`：self/cls 方法、同文件类方法、跨文件模块成员，或如实未解析。
    fn call_attr(&mut self, f: &PyNode, line: usize, caller: &str) {
        let attr = f.name.clone();
        let Some(base) = f.children.first() else { return };
        let expr = expr_text(f);
        if base.kind != "Name" {
            // 链式调用（a.b.c() / f()()）：根是 import 的延迟到 stitch 区分 external/
            // attr_chain（对外部模块的链式调用单列为 external，噪声归类更诚实）
            let mut root = base;
            while matches!(root.kind, "Attribute" | "Call" | "Subscript") {
                match root.children.first() {
                    Some(c) => root = c,
                    None => break,
                }
            }
            if root.kind == "Name"
                && matches!(self.resolve(&root.name), Some((_, _, "import"))) {
                    let rn = root.name.clone();
                    self.defer_chain(&rn, line, caller, &expr);
            } else {
                self.calls_unresolved.push(self.ures(line, caller, &expr, "attr_chain"));
            }
            return;
        }
        let bname = base.name.clone();
        // self.m() / cls.m()：基名是某方法的参数且方法直接位于类体
        if let Some(cls_label) = self.self_like_class(&bname) {
            match self.class_tables.get(&cls_label).and_then(|t| t.get(&attr)).copied() {
                Some((tl, tk)) if tk == "def" || tk == "class" => {
                    let qual = if self.modname.is_empty() {
                        format!("{}.{}", cls_label, attr)
                    } else {
                        format!("{}.{}.{}", self.modname, cls_label, attr)
                    };
                    let to_file = self.file.clone();
                    self.calls.push(self.edge(line, caller, &qual, &to_file, tl, "self_attr"));
                }
                Some(_) => self.calls_unresolved.push(self.ures(line, caller, &expr, "self_attr_var")),
                None => self.calls_unresolved.push(self.ures(line, caller, &expr, "self_attr_missing")),
            }
            return;
        }
        match self.resolve(&bname) {
            Some((idx, _l, bk)) => match bk {
                "import" => self.defer_import(&bname, line, caller, &attr),
                "class" => {
                    let cls_label = self.class_label_at(idx, &bname);
                    match self.class_tables.get(&cls_label).and_then(|t| t.get(&attr)).copied() {
                        Some((tl, tk)) if tk == "def" || tk == "class" => {
                            let qual = if self.modname.is_empty() {
                                format!("{}.{}", cls_label, attr)
                            } else {
                                format!("{}.{}.{}", self.modname, cls_label, attr)
                            };
                            let to_file = self.file.clone();
                            self.calls.push(self.edge(line, caller, &qual, &to_file, tl, "module_attr"));
                        }
                        Some(_) => self.calls_unresolved.push(self.ures(line, caller, &expr, "attr_var")),
                        None => self.calls_unresolved.push(self.ures(line, caller, &expr, "attr_missing")),
                    }
                }
                // 对普通变量取属性调用（含函数对象）：无类型推断，不猜
                _ => self.calls_unresolved.push(self.ures(line, caller, &expr, "receiver_var")),
            },
            None => {
                let reason = if self.star_import { "star_import" } else { "not_found" };
                self.calls_unresolved.push(self.ures(line, caller, &expr, reason));
            }
        }
    }

    /// import 基名/名字 → 跨文件待定（stitch 阶段查模块索引）。
    /// attr 为空 = 名字调用（`f()`）；非空 = `m.f()` 形态的成员调用（记 base）。
    fn defer_import(&mut self, bound: &str, line: usize, caller: &str, attr: &str) {
        let fact = self.imports.iter().find(|i| {
            let b = if i.is_from {
                if i.asname.is_empty() { i.name.clone() } else { i.asname.clone() }
            } else if i.asname.is_empty() {
                i.module.split('.').next().unwrap_or("").to_string()
            } else {
                i.asname.clone()
            };
            b == bound
        }).map(|i| (i.is_from, i.level, i.module.clone(), i.name.clone()));
        let (expr, reason) = if attr.is_empty() {
            (bound.to_string(), "not_found")
        } else {
            (format!("{}.{}", bound, attr), "attr_missing")
        };
        let Some((is_from, level, module, fname)) = fact else {
            // 绑定在但 import 事实缺失（罕见序）→ 如实未解析
            self.calls_unresolved.push(self.ures(line, caller, &expr, reason));
            return;
        };
        if attr.is_empty() && !is_from {
            // `import a.b` 的裸名 `a()`：模块对象不可调用
            self.calls_unresolved.push(self.ures(line, caller, &expr, "var_call"));
            return;
        }
        // 目标模块名（相对导入按层级上溯包；公式与 resolve_dir 一致）
        let target_mod = if is_from && level > 0 {
            let mut base = pkg_of(&self.modname);
            for _ in 1..level {
                base = pkg_of(&base);
            }
            if module.is_empty() { base }
            else if base.is_empty() { module.clone() }
            else { format!("{}.{}", base, module) }
        } else {
            module.clone()
        };
        let (name, base_f, kind) = if attr.is_empty() {
            (fname, String::new(), "from_import")
        } else {
            (attr.to_string(), bound.to_string(), "module_attr")
        };
        self.deferred.push(Value::Obj(vec![
            ("file".into(), Value::Str(self.file.clone())),
            ("line".into(), Value::Int(line as i128)),
            ("caller".into(), Value::Str(caller.to_string())),
            ("expr".into(), Value::Str(expr)),
            ("target_mod".into(), Value::Str(target_mod)),
            ("base".into(), Value::Str(base_f)),
            ("name".into(), Value::Str(name)),
            ("kind".into(), Value::Str(kind.into())),
        ]));
    }

    /// 链式调用的根 import → 待定（仅用于 external/attr_chain 归类）。
    fn defer_chain(&mut self, root_bound: &str, line: usize, caller: &str, expr: &str) {
        let fact = self.imports.iter().find(|i| {
            let b = if i.is_from {
                if i.asname.is_empty() { i.name.clone() } else { i.asname.clone() }
            } else if i.asname.is_empty() {
                i.module.split('.').next().unwrap_or("").to_string()
            } else {
                i.asname.clone()
            };
            b == root_bound
        }).map(|i| (i.is_from, i.level, i.module.clone()));
        let Some((is_from, level, module)) = fact else {
            self.calls_unresolved.push(self.ures(line, caller, expr, "attr_chain"));
            return;
        };
        let target_mod = if is_from && level > 0 {
            let mut base = pkg_of(&self.modname);
            for _ in 1..level {
                base = pkg_of(&base);
            }
            if module.is_empty() { base }
            else if base.is_empty() { module.clone() }
            else { format!("{}.{}", base, module) }
        } else {
            module.clone()
        };
        self.deferred.push(Value::Obj(vec![
            ("file".into(), Value::Str(self.file.clone())),
            ("line".into(), Value::Int(line as i128)),
            ("caller".into(), Value::Str(caller.to_string())),
            ("expr".into(), Value::Str(expr.to_string())),
            ("target_mod".into(), Value::Str(target_mod)),
            ("base".into(), Value::Str(root_bound.to_string())),
            ("name".into(), Value::Str(String::new())),
            ("kind".into(), Value::Str("attr_chain_root".into())),
        ]));
    }

    fn lambda_(&mut self, n: &PyNode) {
        // 默认值在外层求值（规则 6）
        if let Some(args) = n.children.first() {
            for c in &args.children {
                if !matches!(c.kind, "arg" | "vararg" | "kwarg") {
                    self.expr(c);
                }
            }
        }
        let label = format!("<lambda@{}>", n.line);
        self.scopes.push(Scope::new(SK::Func, &label));
        if let Some(args) = n.children.first() {
            for c in &args.children {
                if matches!(c.kind, "arg" | "vararg" | "kwarg") {
                    let (name, line) = (c.name.clone(), c.line);
                    self.bind(&name, line, "param");
                }
            }
        }
        for c in n.children.iter().skip(1) {
            self.expr(c);
        }
        self.scopes.pop();
    }

    fn comp(&mut self, n: &PyNode) {
        let comps: Vec<&PyNode> = n.children.iter().filter(|c| c.kind == "comprehension").collect();
        // 规则 2：首个 comprehension 的 iter 在外层求值
        if let Some(first) = comps.first()
            && let Some(iter) = first.children.get(1) {
                self.expr(iter);
            }
        self.scopes.push(Scope::new(SK::Comp, "<comp>"));
        for (gi, g) in comps.iter().enumerate() {
            // 目标绑定进推导式作用域；后续 iter 也在其中
            if let Some(t) = g.children.first() {
                self.bind_target(t, "comp");
            }
            if gi > 0
                && let Some(iter) = g.children.get(1) {
                    self.expr(iter);
                }
            for c in g.children.iter().skip(2) {
                self.expr(c);
            }
        }
        // 元素表达式（ListComp/SetComp/GenExp = children[0]；DictComp = key+value）
        let head = if n.kind == "DictComp" { 2 } else { 1 };
        for c in n.children.iter().take(head) {
            self.expr(c);
        }
        self.scopes.pop();
    }

    // ---------- 语句 ----------

    fn stmt(&mut self, n: &PyNode) {
        match n.kind {
            "FunctionDef" | "AsyncFunctionDef" => self.funcdef(n),
            "ClassDef" => self.classdef(n),
            "Import" => {
                for a in &n.children {
                    let bound = if a.name2.is_empty() {
                        a.name.split('.').next().unwrap_or("").to_string()
                    } else {
                        a.name2.clone()
                    };
                    self.imports.push(ImportFact {
                        line: a.line, level: 0, module: a.name.clone(),
                        name: String::new(), asname: a.name2.clone(), is_from: false,
                    });
                    self.bind(&bound, a.line, "import");
                }
            }
            "ImportFrom" => {
                for a in &n.children {
                    self.imports.push(ImportFact {
                        line: a.line, level: n.aux, module: n.name.clone(),
                        name: a.name.clone(), asname: a.name2.clone(), is_from: true,
                    });
                    if a.name == "*" {
                        // 规则 8：star import 静态不可解，如实标记
                        self.star_import = true;
                        self.n_unresolved += 1;
                        self.unresolved.push(Value::Obj(vec![
                            ("line".into(), Value::Int(a.line as i128)),
                            ("name".into(), Value::Str("*".into())),
                            ("reason".into(), Value::Str("star_import".into())),
                        ]));
                    } else {
                        let bound = if a.name2.is_empty() { a.name.clone() } else { a.name2.clone() };
                        self.bind(&bound, a.line, "import");
                    }
                }
            }
            "Global" | "Nonlocal" => {
                let names = n.names.clone();
                let g = n.kind == "Global";
                let cur = self.cur();
                for nm in names {
                    if g {
                        cur.globals.insert(nm);
                    } else {
                        cur.nonlocals.insert(nm);
                    }
                }
            }
            "Assign" => {
                let last = n.children.len().saturating_sub(1);
                for (i, c) in n.children.iter().enumerate() {
                    if i == last {
                        self.expr(c);
                    } else {
                        self.bind_target(c, "assign");
                    }
                }
            }
            "AnnAssign" => {
                if let Some(t) = n.children.first() {
                    self.bind_target(t, "assign");
                }
                for c in n.children.iter().skip(1) {
                    self.expr(c);
                }
            }
            "AugAssign" => {
                if let Some(t) = n.children.first() {
                    // x += 1 既读又写：先按读取解析，再记绑定
                    if t.kind == "Name" {
                        let (name, line) = (t.name.clone(), t.line);
                        self.load(&name, line);
                    }
                    self.bind_target(t, "aug");
                }
                for c in n.children.iter().skip(1) {
                    self.expr(c);
                }
            }
            "For" | "AsyncFor" => {
                if let Some(t) = n.children.first() {
                    self.bind_target(t, "for");
                }
                for c in n.children.iter().skip(1) {
                    if is_stmt(c.kind) {
                        self.stmt(c);
                    } else {
                        self.expr(c);
                    }
                }
            }
            "With" | "AsyncWith" => {
                for c in &n.children {
                    if c.kind == "withitem" {
                        if let Some(ctx) = c.children.first() {
                            self.expr(ctx);
                        }
                        if let Some(t) = c.children.get(1) {
                            self.bind_target(t, "with");
                        }
                    } else if is_stmt(c.kind) {
                        self.stmt(c);
                    } else {
                        self.expr(c);
                    }
                }
            }
            "Try" | "TryStar" => {
                for c in &n.children {
                    if c.kind == "ExceptHandler" {
                        if !c.name.is_empty() {
                            let (name, line) = (c.name.clone(), c.line);
                            self.bind(&name, line, "except");
                        }
                        for b in &c.children {
                            self.stmt(b);
                        }
                    } else if is_stmt(c.kind) {
                        self.stmt(c);
                    } else {
                        self.expr(c);
                    }
                }
            }
            "Match" => {
                // Match children = [subject, match_case...]；
                // match_case children = [pattern, guard?, body...]
                for (i, c) in n.children.iter().enumerate() {
                    if i == 0 {
                        self.expr(c);
                    } else if c.kind == "match_case" {
                        if let Some(p) = c.children.first() {
                            self.match_node(p);
                        }
                        for b in c.children.iter().skip(1) {
                            if is_stmt(b.kind) {
                                self.stmt(b);
                            } else {
                                self.expr(b); // guard 表达式
                            }
                        }
                    } else {
                        self.match_node(c);
                    }
                }
            }
            "Delete" => {
                for c in &n.children {
                    self.expr(c);
                }
            }
            _ => {
                for c in &n.children {
                    if is_stmt(c.kind) {
                        self.stmt(c);
                    } else {
                        self.expr(c);
                    }
                }
            }
        }
    }

    /// 模式节点：捕获名进当前作用域；类名/值表达式按读取解析。
    fn match_node(&mut self, n: &PyNode) {
        match n.kind {
            "MatchAs" => {
                if !n.name.is_empty() {
                    let (name, line) = (n.name.clone(), n.line);
                    self.bind(&name, line, "match");
                }
                for c in &n.children {
                    self.match_node(c);
                }
            }
            "MatchStar" | "MatchMapping" => {
                if !n.name.is_empty() {
                    let (name, line) = (n.name.clone(), n.line);
                    self.bind(&name, line, "match");
                }
                for c in &n.children {
                    self.match_node(c);
                }
            }
            "MatchValue" | "MatchClass" => {
                for c in &n.children {
                    self.expr(c);
                }
            }
            _ => {
                for c in &n.children {
                    self.match_node(c);
                }
            }
        }
    }

    fn funcdef(&mut self, n: &PyNode) {
        // 名字绑到外层
        let (fname, fline) = (n.name.clone(), n.line);
        self.bind(&fname, fline, "def");
        // 默认值 / 装饰器 / 返回注解在外层求值（规则 6）
        if let Some(args) = n.children.first() {
            for c in &args.children {
                if !matches!(c.kind, "arg" | "vararg" | "kwarg") {
                    self.expr(c);
                }
            }
        }
        for c in n.children.iter().skip(1) {
            if !is_stmt(c.kind) {
                self.expr(c); // 装饰器 / 返回注解
            }
        }
        // 新函数作用域：参数绑定 + 函数体
        let label = self.child_label(&n.name);
        self.scopes.push(Scope::new(SK::Func, &label));
        // S125：定义栈（调用者归属）；lambda 不入栈（归属最近的外层定义）
        if self.collect {
            self.def_stack.push(label.clone());
        }
        if let Some(args) = n.children.first() {
            for c in &args.children {
                if matches!(c.kind, "arg" | "vararg" | "kwarg") {
                    let (name, line) = (c.name.clone(), c.line);
                    self.bind(&name, line, "param");
                }
            }
        }
        for c in n.children.iter().skip(1) {
            if is_stmt(c.kind) {
                self.stmt(c);
            }
        }
        if self.collect {
            self.def_stack.pop();
        }
        self.scopes.pop();
    }

    fn classdef(&mut self, n: &PyNode) {
        let (cname, cline) = (n.name.clone(), n.line);
        self.bind(&cname, cline, "class");
        // bases（前 aux 个）与关键字实参在外层求值
        for c in n.children.iter().take(n.aux) {
            self.expr(c);
        }
        let label = self.child_label(&n.name);
        self.scopes.push(Scope::new(SK::Class, &label));
        // S125：类作用域预种子——self.m() 的 m 可能在方法之后定义（前向引用）
        if self.collect
            && let Some(tbl) = self.class_tables.get(&label) {
                for (k, v) in tbl {
                    self.scopes.last_mut().expect("scope 非空").bindings.insert(k.clone(), *v);
                }
            }
        // S125：类体直接执行的代码（如类级常量计算）归属该类
        if self.collect {
            self.def_stack.push(label.clone());
        }
        for c in n.children.iter().skip(n.aux) {
            if c.kind == "keyword" {
                self.expr(c);
            } else if is_stmt(c.kind) {
                self.stmt(c);
            } else {
                self.expr(c);
            }
        }
        if self.collect {
            self.def_stack.pop();
        }
        self.scopes.pop();
    }
}

/// 单文件解析入口：返回 `{file, edges, unresolved, stats}`；语法错误返回 `{error}`。
pub fn resolve_file(path: &str, src: &str) -> Value {
    let tree = match parse_module(src) {
        Ok(t) => t,
        Err(e) => {
            return Value::Obj(vec![
                ("error".into(),
                 Value::Str(format!("语法错误 行 {}: {}", e.line, e.msg))),
            ]);
        }
    };
    let mut r = Resolver::new(path, "", false);
    for c in &tree.children {
        r.stmt(c);
    }
    // 稳定排序：按 (line, name)
    let key = |v: &Value| -> (i128, String) {
        let line = match v.get("line") {
            Some(Value::Int(i)) => *i,
            _ => 0,
        };
        let name = match v.get("name") {
            Some(Value::Str(s)) => s.clone(),
            _ => String::new(),
        };
        (line, name)
    };
    r.edges.sort_by_key(|a| key(a));
    r.unresolved.sort_by_key(|a| key(a));
    Value::Obj(vec![
        ("file".into(), Value::Str(path.to_string())),
        ("edges".into(), Value::Arr(r.edges)),
        ("unresolved".into(), Value::Arr(r.unresolved)),
        ("bindings".into(), Value::Arr(r.bindings)),
        ("imports".into(), Value::Arr(r.imports.iter().map(|i| Value::Obj(vec![
            ("line".into(), Value::Int(i.line as i128)),
            ("level".into(), Value::Int(i.level as i128)),
            ("module".into(), Value::Str(i.module.clone())),
            ("name".into(), Value::Str(i.name.clone())),
            ("asname".into(), Value::Str(i.asname.clone())),
            ("is_from".into(), Value::Bool(i.is_from)),
        ])).collect())),
        ("stats".into(), Value::Obj(vec![
            ("local".into(), Value::Int(r.n_local as i128)),
            ("module".into(), Value::Int(r.n_module as i128)),
            ("builtin".into(), Value::Int(r.n_builtin as i128)),
            ("unresolved".into(), Value::Int(r.n_unresolved as i128)),
            ("attr_accesses".into(), Value::Int(r.n_attr as i128)),
            ("star_import".into(), Value::Bool(r.star_import)),
        ])),
    ])
}

// ---------- S108：跨文件 import 拼接 ----------

const SKIP_DIRS: [&str; 8] = [".git", "node_modules", "target", "__pycache__",
    "dist", "build", ".venv", "venv"];

fn walk_py(dir: &Path, out: &mut Vec<std::path::PathBuf>, max: usize) {
    if out.len() >= max {
        return;
    }
    let rd = match std::fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => return,
    };
    let mut files: Vec<std::path::PathBuf> = Vec::new();
    let mut dirs: Vec<std::path::PathBuf> = Vec::new();
    for e in rd.filter_map(|e| e.ok()) {
        let name = e.file_name().to_string_lossy().into_owned();
        let p = e.path();
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_dir() {
            if !SKIP_DIRS.contains(&name.as_str()) {
                dirs.push(p);
            }
        } else if name.ends_with(".py") {
            files.push(p);
        }
    }
    files.sort();
    dirs.sort();
    for f in files {
        if out.len() >= max {
            return;
        }
        out.push(f);
    }
    for d in dirs {
        walk_py(&d, out, max);
    }
}

/// 文件相对路径 → 模块名（`a/b/__init__.py` 与 `a/b.py` 都 → "a.b"）。
fn rel_mod(rel: &str) -> String {
    let stem = rel.strip_suffix(".py").unwrap_or(rel).replace('\\', "/");
    let mut parts: Vec<&str> = stem.split('/').filter(|x| !x.is_empty()).collect();
    if parts.last() == Some(&"__init__") {
        parts.pop();
    }
    parts.join(".")
}

fn pkg_of(modname: &str) -> String {
    match modname.rfind('.') {
        Some(i) => modname[..i].to_string(),
        None => String::new(),
    }
}

struct FileRes {
    rel: String,
    modname: String,
    bindings: HashMap<String, (usize, &'static str)>,
    imports: Vec<ImportFact>,
}

fn analyze_file(root: &Path, p: &Path) -> Option<FileRes> {
    let rel = match p.strip_prefix(root) {
        Ok(r) => r.to_string_lossy().replace('\\', "/"),
        Err(_) => return None,
    };
    let src = match crate::rcache::read(p) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(_) => return None,
    };
    let tree = match parse_module(&src) {
        Ok(t) => t,
        Err(_) => return None,   // 语法错误文件跳过（不拖垮整仓）
    };
    let mut r = Resolver::new(&rel, "", false);
    for c in &tree.children {
        r.stmt(c);
    }
    Some(FileRes {
        modname: rel_mod(&rel),
        bindings: r.scopes[0].bindings.clone(),
        rel,
        imports: r.imports,
    })
}

/// 目录级解析（S108）：逐文件解析 + import 拼接。
/// 输出 `{root, files, imports, external, unresolved, stats}`（路径均相对 root）。
pub fn resolve_dir(root: &Path, max_files: usize) -> Value {
    let mut py: Vec<std::path::PathBuf> = Vec::new();
    walk_py(root, &mut py, max_files);
    let mut files: Vec<FileRes> = py.iter().filter_map(|p| analyze_file(root, p)).collect();
    // root 自身是包（有 __init__.py）时，模块名带包前缀（与 Python 在父目录
    // 导入时的视角一致：`tools/fs.py` → "tools.fs"）
    if root.join("__init__.py").is_file()
        && let Some(base) = root.file_name().map(|s| s.to_string_lossy().into_owned()) {
            for f in files.iter_mut() {
                f.modname = if f.modname.is_empty() {
                    base.clone()
                } else {
                    format!("{}.{}", base, f.modname)
                };
            }
        }

    // 模块索引：模块名 → 文件下标
    let mut index: HashMap<String, usize> = HashMap::new();
    for (i, f) in files.iter().enumerate() {
        if !f.modname.is_empty() {
            index.insert(f.modname.clone(), i);
        }
    }

    let mut imports_out: Vec<Value> = Vec::new();
    let mut external_out: Vec<Value> = Vec::new();
    let mut unresolved_out: Vec<Value> = Vec::new();
    let (mut n_internal, mut n_external, mut n_unresolved) = (0usize, 0usize, 0usize);

    for f in &files {
        for imp in &f.imports {
            // 目标模块名（相对导入按层级上溯包）
            let target_mod = if imp.is_from && imp.level > 0 {
                let mut base = pkg_of(&f.modname);
                for _ in 1..imp.level {
                    base = pkg_of(&base);
                }
                if imp.module.is_empty() {
                    base
                } else if base.is_empty() {
                    imp.module.clone()
                } else {
                    format!("{}.{}", base, imp.module)
                }
            } else {
                imp.module.clone()
            };
            if imp.is_from && imp.name == "*" {
                n_unresolved += 1;
                unresolved_out.push(Value::Obj(vec![
                    ("file".into(), Value::Str(f.rel.clone())),
                    ("line".into(), Value::Int(imp.line as i128)),
                    ("name".into(), Value::Str("*".into())),
                    ("module".into(), Value::Str(target_mod)),
                    ("reason".into(), Value::Str("star_import".into())),
                ]));
                continue;
            }
            let Some(ti) = index.get(&target_mod).copied() else {
                n_external += 1;
                external_out.push(Value::Obj(vec![
                    ("file".into(), Value::Str(f.rel.clone())),
                    ("line".into(), Value::Int(imp.line as i128)),
                    ("module".into(), Value::Str(target_mod)),
                ]));
                continue;
            };
            let tf = &files[ti];
            if imp.is_from {
                let bound = if imp.asname.is_empty() { imp.name.clone() } else { imp.asname.clone() };
                match tf.bindings.get(&imp.name) {
                    Some((line, _k)) => {
                        n_internal += 1;
                        imports_out.push(Value::Obj(vec![
                            ("file".into(), Value::Str(f.rel.clone())),
                            ("line".into(), Value::Int(imp.line as i128)),
                            ("name".into(), Value::Str(bound)),
                            ("module".into(), Value::Str(target_mod.clone())),
                            ("to_file".into(), Value::Str(tf.rel.clone())),
                            ("to_line".into(), Value::Int(*line as i128)),
                            ("kind".into(), Value::Str("from".into())),
                        ]));
                    }
                    None => {
                        // from pkg import submodule 形态：目标不是绑定而是子模块
                        let sub = format!("{}.{}", target_mod, imp.name);
                        if let Some(si) = index.get(&sub).copied() {
                            n_internal += 1;
                            imports_out.push(Value::Obj(vec![
                                ("file".into(), Value::Str(f.rel.clone())),
                                ("line".into(), Value::Int(imp.line as i128)),
                                ("name".into(), Value::Str(bound)),
                                ("module".into(), Value::Str(sub)),
                                ("to_file".into(), Value::Str(files[si].rel.clone())),
                                ("to_line".into(), Value::Int(1)),
                                ("kind".into(), Value::Str("from_submodule".into())),
                            ]));
                        } else {
                            n_unresolved += 1;
                            unresolved_out.push(Value::Obj(vec![
                                ("file".into(), Value::Str(f.rel.clone())),
                                ("line".into(), Value::Int(imp.line as i128)),
                                ("name".into(), Value::Str(imp.name.clone())),
                                ("module".into(), Value::Str(target_mod.clone())),
                                ("reason".into(), Value::Str("name_not_found".into())),
                            ]));
                        }
                    }
                }
            } else {
                let bound = if imp.asname.is_empty() {
                    imp.module.split('.').next().unwrap_or("").to_string()
                } else {
                    imp.asname.clone()
                };
                n_internal += 1;
                imports_out.push(Value::Obj(vec![
                    ("file".into(), Value::Str(f.rel.clone())),
                    ("line".into(), Value::Int(imp.line as i128)),
                    ("name".into(), Value::Str(bound)),
                    ("module".into(), Value::Str(target_mod.clone())),
                    ("to_file".into(), Value::Str(tf.rel.clone())),
                    ("to_line".into(), Value::Int(1)),
                    ("kind".into(), Value::Str("import".into())),
                ]));
            }
        }
    }

    let key = |v: &Value| -> (String, i128) {
        let f = match v.get("file") { Some(Value::Str(s)) => s.clone(), _ => String::new() };
        let l = match v.get("line") { Some(Value::Int(i)) => *i, _ => 0 };
        (f, l)
    };
    imports_out.sort_by_key(|a| key(a));
    external_out.sort_by_key(|a| key(a));
    unresolved_out.sort_by_key(|a| key(a));

    Value::Obj(vec![
        ("root".into(), Value::Str(root.to_string_lossy().into_owned())),
        ("files".into(), Value::Int(files.len() as i128)),
        ("imports".into(), Value::Arr(imports_out)),
        ("external".into(), Value::Arr(external_out)),
        ("unresolved".into(), Value::Arr(unresolved_out)),
        ("stats".into(), Value::Obj(vec![
            ("internal".into(), Value::Int(n_internal as i128)),
            ("external".into(), Value::Int(n_external as i128)),
            ("unresolved".into(), Value::Int(n_unresolved as i128)),
        ])),
    ])
}

// ---------- S125：调用图（阶段 1 预扫描 + 阶段 2 主遍历 + stitch） ----------

/// 预扫描结果：模块级/类级绑定表 + import 事实。
/// 只扫 def/class/import/赋值目标（不下函数体）——前向引用（互递归、先调用后定义）
/// 在主遍历开始前即有种子；函数体内的顺序绑定由主遍历按序处理（局部互递归是
/// 文档化边界，见 spec/CALLGRAPH.md）。
struct PreScan {
    module: HashMap<String, (usize, &'static str)>,
    classes: HashMap<String, HashMap<String, (usize, &'static str)>>,
    imports: Vec<ImportFact>,
    star: bool,
}

fn table_bind(ps: &mut PreScan, level: &str, name: &str, line: usize, kind: &'static str) {
    if name.is_empty() {
        return;
    }
    // 同名遮蔽取最后一次绑定（与 Scope::bind 同口径）
    if level == "module" {
        let e = ps.module.entry(name.to_string()).or_insert((line, kind));
        if line >= e.0 {
            *e = (line, kind);
        }
    } else {
        let tbl = ps.classes.entry(level.to_string()).or_default();
        let e = tbl.entry(name.to_string()).or_insert((line, kind));
        if line >= e.0 {
            *e = (line, kind);
        }
    }
}

fn target_names(n: &PyNode, out: &mut Vec<(String, usize)>) {
    match n.kind {
        "Name" => out.push((n.name.clone(), n.line)),
        "Tuple" | "List" | "Starred" => {
            for c in &n.children {
                target_names(c, out);
            }
        }
        _ => {} // Attribute/Subscript：基名是读取，不构成绑定
    }
}

fn child_label_of(level: &str, name: &str) -> String {
    if level == "module" {
        name.to_string()
    } else {
        format!("{}.{}", level, name)
    }
}

fn prenode(n: &PyNode, level: &str, ps: &mut PreScan) {
    match n.kind {
        "FunctionDef" | "AsyncFunctionDef" => {
            let (nm, ln) = (n.name.clone(), n.line);
            table_bind(ps, level, &nm, ln, "def");
        }
        "ClassDef" => {
            let (nm, ln) = (n.name.clone(), n.line);
            table_bind(ps, level, &nm, ln, "class");
            let label = child_label_of(level, &nm);
            ps.classes.entry(label.clone()).or_default();
            for c in n.children.iter().skip(n.aux) {
                if is_stmt(c.kind) {
                    prenode(c, &label, ps);
                }
            }
        }
        "Import" => {
            for a in &n.children {
                let bound = if a.name2.is_empty() {
                    a.name.split('.').next().unwrap_or("").to_string()
                } else {
                    a.name2.clone()
                };
                ps.imports.push(ImportFact {
                    line: a.line, level: 0, module: a.name.clone(),
                    name: String::new(), asname: a.name2.clone(), is_from: false,
                });
                table_bind(ps, level, &bound, a.line, "import");
            }
        }
        "ImportFrom" => {
            for a in &n.children {
                if a.name == "*" {
                    ps.star = true;
                    continue;
                }
                ps.imports.push(ImportFact {
                    line: a.line, level: n.aux, module: n.name.clone(),
                    name: a.name.clone(), asname: a.name2.clone(), is_from: true,
                });
                let bound = if a.name2.is_empty() { a.name.clone() } else { a.name2.clone() };
                table_bind(ps, level, &bound, a.line, "import");
            }
        }
        "Assign" => {
            let last = n.children.len().saturating_sub(1);
            for (i, c) in n.children.iter().enumerate() {
                if i == last {
                    continue;
                }
                let mut names = Vec::new();
                target_names(c, &mut names);
                for (nm, ln) in names {
                    table_bind(ps, level, &nm, ln, "assign");
                }
            }
        }
        "AnnAssign" | "AugAssign" => {
            if let Some(t) = n.children.first() {
                let mut names = Vec::new();
                target_names(t, &mut names);
                for (nm, ln) in names {
                    table_bind(ps, level, &nm, ln, "assign");
                }
            }
        }
        "For" | "AsyncFor" => {
            if let Some(t) = n.children.first() {
                let mut names = Vec::new();
                target_names(t, &mut names);
                for (nm, ln) in names {
                    table_bind(ps, level, &nm, ln, "for");
                }
            }
            for c in n.children.iter().skip(1) {
                if is_stmt(c.kind) {
                    prenode(c, level, ps);
                }
            }
        }
        "With" | "AsyncWith" => {
            for c in &n.children {
                if c.kind == "withitem" {
                    if let Some(t) = c.children.get(1) {
                        let mut names = Vec::new();
                        target_names(t, &mut names);
                        for (nm, ln) in names {
                            table_bind(ps, level, &nm, ln, "with");
                        }
                    }
                } else if is_stmt(c.kind) {
                    prenode(c, level, ps);
                }
            }
        }
        "If" | "While" | "Try" | "TryStar" | "Match" => {
            for c in &n.children {
                if is_stmt(c.kind) {
                    prenode(c, level, ps);
                }
            }
        }
        _ => {}
    }
}

fn vstr(v: &Value, k: &str) -> String {
    match v.get(k) {
        Some(Value::Str(s)) => s.clone(),
        _ => String::new(),
    }
}

fn vint(v: &Value, k: &str) -> usize {
    match v.get(k) {
        Some(Value::Int(i)) => (*i).max(0) as usize,
        _ => 0,
    }
}

/// 目录级调用图（S125）：`{root, files, nodes, edges, unresolved, stats}`。
/// 节点 = def/class 定义；边 = 调用点 → 定义（同文件直接裁决，跨文件 stitch）；
/// 不可解析调用如实入 unresolved（reason 分类）。路径均相对 root。
pub fn callgraph_dir(root: &Path, max_files: usize) -> Value {
    let mut py: Vec<std::path::PathBuf> = Vec::new();
    walk_py(root, &mut py, max_files);

    let prefix = if root.join("__init__.py").is_file() {
        root.file_name().map(|s| s.to_string_lossy().into_owned())
    } else {
        None
    };

    // 阶段 1：预扫描（S156：**分块并行 + 有序收集**——逐文件独立：读+解析+预扫描；
    // 收集按块序 = 文件序，阶段 2 消费顺序不变 → 输出逐字节同，金标准/审计测试锁）
    let _dbg = std::env::var("UNIFIED_RX_DEBUG_TIMING").is_ok();
    let _t0 = std::time::Instant::now();
    let mut pre: Vec<CgPre> = prescan_parallel(&py, root, prefix.as_deref());

    // 模块索引
    let mut index: HashMap<String, usize> = HashMap::new();
    for (i, f) in pre.iter().enumerate() {
        if !f.modname.is_empty() {
            index.insert(f.modname.clone(), i);
        }
    }

    if _dbg { eprintln!("NM_TIMING phase1={}ms", _t0.elapsed().as_millis()); }
    let _t1 = std::time::Instant::now();
    // 阶段 2：主遍历（种子 + 调用采集）——S157：**分块并行**（每文件自包含：
    // Resolver 只吃本文件 ps+tree，共享只读 index 仅 stitch 用）；按块序合并 =
    // 文件序，输出逐字节同（金标准/审计测试锁）。
    let mut nodes: Vec<Value> = Vec::new();
    let mut edges: Vec<Value> = Vec::new();
    let mut unresolved: Vec<Value> = Vec::new();
    let mut deferred: Vec<Value> = Vec::new();
    let (mut n_calls, mut n_builtin_calls) = (0usize, 0usize);
    {
        let n_thr = crate::par::par_degree(0); // S167：0 = 用满可用并行度
        if pre.len() < 8 || n_thr <= 1 {
            for f in pre.iter_mut() {
                let o = phase2_one(f);
                nodes.extend(o.nodes);
                edges.extend(o.calls);
                unresolved.extend(o.unresolved);
                deferred.extend(o.deferred);
                n_calls += o.n_calls;
                n_builtin_calls += o.n_builtin_calls;
            }
        } else {
            let chunk = pre.len().div_ceil(n_thr);
            std::thread::scope(|s| {
                let handles: Vec<_> = pre.chunks_mut(chunk).map(|c| {
                    s.spawn(move || {
                        let mut o = Phase2Out::default();
                        for f in c {
                            let x = phase2_one(f);
                            o.nodes.extend(x.nodes);
                            o.calls.extend(x.calls);
                            o.unresolved.extend(x.unresolved);
                            o.deferred.extend(x.deferred);
                            o.n_calls += x.n_calls;
                            o.n_builtin_calls += x.n_builtin_calls;
                        }
                        o
                    })
                }).collect();
                for h in handles {
                    let o = h.join().unwrap_or_default();
                    nodes.extend(o.nodes);
                    edges.extend(o.calls);
                    unresolved.extend(o.unresolved);
                    deferred.extend(o.deferred);
                    n_calls += o.n_calls;
                    n_builtin_calls += o.n_builtin_calls;
                }
            });
        }
    }

    if _dbg { eprintln!("NM_TIMING phase2={}ms", _t1.elapsed().as_millis()); }
    // stitch：跨文件待定调用裁决（S157：分块并行）
    // S157：stitch **分块并行**（每条 deferred 独立、只读 index/pre）——按 deferred
    // 序合并，与串行逐字节同（金标准/审计测试锁）。
    let mut n_stitched = 0usize;
    {
        let n_thr = crate::par::par_degree(8);
        if deferred.len() < 8 || n_thr <= 1 {
            for d in &deferred {
                let (e, u, st) = stitch_one(d, &index, &pre);
                if let Some(e) = e { edges.push(e); }
                if let Some(u) = u { unresolved.push(u); }
                if st { n_stitched += 1; }
            }
        } else {
            let chunk = deferred.len().div_ceil(n_thr);
            let idx_ref: &HashMap<String, usize> = &index;
            let pre_ref: &[CgPre] = &pre;
            std::thread::scope(|s| {
                let handles: Vec<_> = deferred.chunks(chunk).map(|c| {
                    s.spawn(move || {
                        let mut e_v: Vec<Value> = Vec::new();
                        let mut u_v: Vec<Value> = Vec::new();
                        let mut st_n = 0usize;
                        for d in c {
                            let (e, u, st) = stitch_one(d, idx_ref, pre_ref);
                            if let Some(e) = e { e_v.push(e); }
                            if let Some(u) = u { u_v.push(u); }
                            if st { st_n += 1; }
                        }
                        (e_v, u_v, st_n)
                    })
                }).collect();
                for h in handles {
                    let (mut e_v, mut u_v, st_n) = h.join().unwrap_or_default();
                    edges.append(&mut e_v);
                    unresolved.append(&mut u_v);
                    n_stitched += st_n;
                }
            });
        }
    }

    // 稳定排序 + 统计
    nodes.sort_by_key(|a| (vstr(a, "file"), vint(a, "line"), vstr(a, "qual")));
    edges.sort_by_key(|a| (vstr(a, "file"), vint(a, "line"), vstr(a, "callee")));
    unresolved.sort_by_key(|a| (vstr(a, "file"), vint(a, "line"), vstr(a, "expr")));
    let mut by_reason: HashMap<String, usize> = HashMap::new();
    for u in &unresolved {
        *by_reason.entry(vstr(u, "reason")).or_insert(0) += 1;
    }
    let mut reasons: Vec<(String, usize)> = by_reason.into_iter().collect();
    reasons.sort();
    let n_nodes = nodes.len();
    let n_resolved = edges.len();
    let n_unresolved = unresolved.len();

    Value::Obj(vec![
        ("root".into(), Value::Str(root.to_string_lossy().into_owned())),
        ("files".into(), Value::Int(pre.len() as i128)),
        ("nodes".into(), Value::Arr(nodes)),
        ("edges".into(), Value::Arr(edges)),
        ("unresolved".into(), Value::Arr(unresolved)),
        ("stats".into(), Value::Obj(vec![
            ("files".into(), Value::Int(pre.len() as i128)),
            ("nodes".into(), Value::Int(n_nodes as i128)),
            ("calls".into(), Value::Int(n_calls as i128)),
            ("resolved".into(), Value::Int(n_resolved as i128)),
            ("unresolved".into(), Value::Int(n_unresolved as i128)),
            ("builtin_calls".into(), Value::Int(n_builtin_calls as i128)),
            ("stitched".into(), Value::Int(n_stitched as i128)),
            ("by_reason".into(), Value::Obj(reasons.into_iter()
                .map(|(k, c)| (k, Value::Int(c as i128))).collect())),
        ])),
    ])
}

/// S157：阶段 2 单文件结果（合并顺序 = 文件序）。
#[derive(Default)]
struct Phase2Out {
    nodes: Vec<Value>,
    calls: Vec<Value>,
    unresolved: Vec<Value>,
    deferred: Vec<Value>,
    n_calls: usize,
    n_builtin_calls: usize,
}

/// S157：阶段 2 单文件主遍历（原循环体逐字搬入；含绑定表放回）。
fn phase2_one(f: &mut CgPre) -> Phase2Out {
    let tree = &f.tree;
    let mut r = Resolver::new(&f.rel, &f.modname, true);
    r.scopes[0].bindings = std::mem::take(&mut f.ps.module);
    r.class_tables = std::mem::take(&mut f.ps.classes);
    r.imports = std::mem::take(&mut f.ps.imports);
    r.star_import = f.ps.star;
    for c in &tree.children {
        r.stmt(c);
    }
    let out = Phase2Out {
        nodes: r.nodes,
        calls: r.calls,
        unresolved: r.calls_unresolved,
        deferred: r.deferred,
        n_calls: r.n_calls,
        n_builtin_calls: r.n_builtin_calls,
    };
    // 主遍历后的模块绑定表放回（stitch 用）
    f.ps.module = std::mem::take(&mut r.scopes[0].bindings);
    out
}

/// S156：callgraph 预扫描单元（原 `callgraph_dir` 函数内 `struct Pre` 提升到模块级，
/// 以便被并行辅助函数复用；字段与语义一字不变）。
struct CgPre {
    rel: String,
    modname: String,
    ps: PreScan,
    /// S155：阶段 1 的解析结果，阶段 2 直接复用（原先重读重解析，纯浪费）
    tree: PyNode,
}

/// S156：阶段 1 单文件预扫描（原循环体逐字搬入；失败返回 None = 原 `continue`）。
fn prescan_one(p: &std::path::Path, root: &Path, prefix: Option<&str>) -> Option<CgPre> {
    let rel = match p.strip_prefix(root) {
        Ok(r) => r.to_string_lossy().replace('\\', "/"),
        Err(_) => return None,
    };
    let src = match crate::rcache::read(p) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(_) => return None,
    };
    let tree = match parse_module(&src) {
        Ok(t) => t,
        Err(_) => return None, // 语法错误文件跳过（与 resolve_dir 一致）
    };
    let mut ps = PreScan {
        module: HashMap::new(),
        classes: HashMap::new(),
        imports: Vec::new(),
        star: false,
    };
    for c in &tree.children {
        prenode(c, "module", &mut ps);
    }
    let mut m = rel_mod(&rel);
    if let Some(base) = prefix {
        m = if m.is_empty() { base.to_string() } else { format!("{}.{}", base, m) };
    }
    Some(CgPre { rel, modname: m, ps, tree })
}

/// S156：分块并行（**S167：线程数＝用满可用并行度**；文件 < 8 或单核走串行——与旧版同）。
fn prescan_parallel(py: &[std::path::PathBuf], root: &Path, prefix: Option<&str>)
    -> Vec<CgPre> {
    let n = crate::par::par_degree(0); // S167：0 = 用满可用并行度（按机器来，不再固定 8）
    if py.len() < 8 || n <= 1 {
        return py.iter().filter_map(|p| prescan_one(p, root, prefix)).collect();
    }
    let chunk = py.len().div_ceil(n);
    let mut out: Vec<CgPre> = Vec::new();
    std::thread::scope(|s| {
        let handles: Vec<_> = py.chunks(chunk).map(|c| {
            s.spawn(move || {
                let mut v: Vec<CgPre> = Vec::new();
                for p in c {
                    if let Some(x) = prescan_one(p, root, prefix) {
                        v.push(x);
                    }
                }
                v
            })
        }).collect();
        for h in handles {
            let mut v = h.join().unwrap_or_default();
            out.append(&mut v);
        }
    });
    out
}

/// S157：stitch 单条 deferred 裁决（原循环体逐字搬入；continue → 早返回，
/// push → 局部输出，计数 → 标志）。返回 (edge, unresolved, stitched)。
fn stitch_one(d: &Value, index: &HashMap<String, usize>, pre: &[CgPre])
    -> (Option<Value>, Option<Value>, bool) {
    let mut edge_out: Option<Value> = None;
    let mut unresolved_out: Option<Value> = None;
    let mut stitched = false;
        let file = vstr(d, "file");
        let line = vint(d, "line");
        let caller = vstr(d, "caller");
        let expr = vstr(d, "expr");
        let target_mod = vstr(d, "target_mod");
        let base = vstr(d, "base");
        let name = vstr(d, "name");
        let kind = vstr(d, "kind");
        let mk_unres = |reason: &str| {
            Value::Obj(vec![
                ("file".into(), Value::Str(file.clone())),
                ("line".into(), Value::Int(line as i128)),
                ("caller".into(), Value::Str(caller.clone())),
                ("expr".into(), Value::Str(expr.clone())),
                ("reason".into(), Value::Str(reason.to_string())),
            ])
        };
        let Some(&ti) = index.get(&target_mod) else {
            return (None, Some(mk_unres("external")), false);
        };
        let tf = &pre[ti];
        // 链根分类：根 import 命中内部模块 → 链式调用静态不可解，归 attr_chain
        if kind == "attr_chain_root" {
            return (None, Some(mk_unres("attr_chain")), false);
        }
        // 1) 目标模块直接找 name（`import mod; mod.f()` / `from m import f; f()`）
        let mut hit: Option<(String, String, usize, &'static str)> = None;
        if let Some((tl, tk)) = tf.ps.module.get(&name) {
            hit = Some((format!("{}.{}", tf.modname, name), tf.rel.clone(), *tl, *tk));
        }
        // 2) module_attr 的基名回退：`from pkg import sub; sub.f()`
        if hit.is_none() && kind == "module_attr" && !base.is_empty() {
            let sub = format!("{}.{}", target_mod, base);
            if let Some(&si) = index.get(&sub)
                && let Some((tl, tk)) = pre[si].ps.module.get(&name) {
                    hit = Some((format!("{}.{}", pre[si].modname, name),
                                pre[si].rel.clone(), *tl, *tk));
                }
        }
        match hit {
            Some((callee, tfile, tline, tk)) if tk == "def" || tk == "class" => {
                stitched = true;
                edge_out = Some(Value::Obj(vec![
                    ("file".into(), Value::Str(file.clone())),
                    ("line".into(), Value::Int(line as i128)),
                    ("caller".into(), Value::Str(caller.clone())),
                    ("callee".into(), Value::Str(callee)),
                    ("to_file".into(), Value::Str(tfile)),
                    ("to_line".into(), Value::Int(tline as i128)),
                    ("kind".into(), Value::Str(kind.clone())),
                ]));
            }
            Some((_, _, _, "import")) => unresolved_out = Some(mk_unres("re_export")),
            Some(_) => unresolved_out = Some(mk_unres("var_call")),
            None => {
                // from-import 的子模块形态：模块对象不可调用
                let sub = format!("{}.{}", target_mod, name);
                if kind == "from_import" && index.contains_key(&sub) {
                    unresolved_out = Some(mk_unres("var_call"));
                } else if kind == "module_attr" {
                    unresolved_out = Some(mk_unres("attr_missing"));
                } else {
                    unresolved_out = Some(mk_unres("name_not_found"));
                }
            }
        }
    (edge_out, unresolved_out, stitched)
}
