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
        for c in n.children.iter().skip(n.aux) {
            if c.kind == "keyword" {
                self.expr(c);
            } else if is_stmt(c.kind) {
                self.stmt(c);
            } else {
                self.expr(c);
            }
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
    let mut r = Resolver {
        file: path.to_string(),
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
    };
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
    let src = match std::fs::read(p) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(_) => return None,
    };
    let tree = match parse_module(&src) {
        Ok(t) => t,
        Err(_) => return None,   // 语法错误文件跳过（不拖垮整仓）
    };
    let mut r = Resolver {
        file: rel.clone(),
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
    };
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
