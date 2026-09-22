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
pub(crate) enum SK {
    Module,
    Func,
    Class,
    Comp,
}

pub(crate) struct Scope {
    pub(crate) kind: SK,    pub(crate) label: String,    pub(crate) bindings: HashMap<String, (usize, &'static str)>,    pub(crate) globals: HashSet<String>,    pub(crate) nonlocals: HashSet<String>,}

impl Scope {
    pub(crate) fn new(kind: SK, label: &str) -> Scope {
        Scope { kind, label: label.to_string(), bindings: HashMap::new(),
                globals: HashSet::new(), nonlocals: HashSet::new() }
    }

    pub(crate) fn bind(&mut self, name: &str, line: usize, kind: &'static str) {
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
pub(crate) struct ImportFact {
    pub(crate) line: usize,    /// 相对导入层级（from ...m import x 的前导点数；import 语句恒 0）
    pub(crate) level: usize,    /// 模块路径（`import a.b` → "a.b"；`from m import x` → m）
    pub(crate) module: String,    /// from 导入的名字（import 语句为空）
    pub(crate) name: String,    /// as 别名（无则空）
    pub(crate) asname: String,    pub(crate) is_from: bool,}

pub(crate) struct Resolver {
    pub(crate) file: String,    /// 模块名（S125 调用图模式：qualname 前缀；旧路径为空串）
    pub(crate) modname: String,    /// S125：调用图采集开关（节点/调用事实只在 callgraph 路径产出）
    pub(crate) collect: bool,    pub(crate) scopes: Vec<Scope>,    pub(crate) edges: Vec<Value>,    pub(crate) unresolved: Vec<Value>,    pub(crate) n_local: usize,    pub(crate) n_module: usize,    pub(crate) n_builtin: usize,    pub(crate) n_unresolved: usize,    pub(crate) n_attr: usize,    pub(crate) star_import: bool,    pub(crate) bindings: Vec<Value>,    pub(crate) imports: Vec<ImportFact>,    // ---------- S125 调用图字段 ----------
    /// 定义节点（qual/file/line/kind），collect 时产出
    pub(crate) nodes: Vec<Value>,    /// 已解析调用边（同文件内解析完成；跨文件由 stitch 补齐）
    pub(crate) calls: Vec<Value>,    /// 跨文件待定调用（目标模块 + 绑定名，stitch 阶段裁决）
    pub(crate) deferred: Vec<Value>,    /// 不可解析调用（如实列出，不猜）
    pub(crate) calls_unresolved: Vec<Value>,    pub(crate) n_calls: usize,    pub(crate) n_builtin_calls: usize,    /// 类作用域绑定表（label → 名字 → (行, 种类)）：self.m()/X.m() 的类方法解析用
    pub(crate) class_tables: HashMap<String, HashMap<String, (usize, &'static str)>>,    /// 定义栈（标签路径，如 "C.m"；模块级为空）——调用者归属
    pub(crate) def_stack: Vec<String>,}

impl Resolver {
    pub(crate) fn new(file: &str, modname: &str, collect: bool) -> Resolver {
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
    pub(crate) fn cur_def(&self) -> String {
        match self.def_stack.last() {
            Some(l) if !self.modname.is_empty() => format!("{}.{}", self.modname, l),
            Some(l) => l.clone(),
            None => String::new(),
        }
    }

    /// 定义处限定名：modname + 标签路径 + 名字（模块级 = "mod.name"）。
    pub(crate) fn qual_at(&self, scope_idx: usize, name: &str) -> String {
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
    pub(crate) fn self_like_class(&self, base: &str) -> Option<String> {
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
    pub(crate) fn class_label_at(&self, scope_idx: usize, name: &str) -> String {
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
pub(crate) fn is_stmt(kind: &str) -> bool {
    matches!(kind,
        "FunctionDef" | "AsyncFunctionDef" | "ClassDef" | "Return" | "Delete"
        | "Assign" | "AugAssign" | "AnnAssign" | "For" | "AsyncFor" | "While"
        | "If" | "With" | "AsyncWith" | "Match" | "Raise" | "Try" | "TryStar"
        | "Assert" | "Import" | "ImportFrom" | "Global" | "Nonlocal" | "Expr"
        | "Pass" | "Break" | "Continue")
}

pub(crate) fn is_builtin(name: &str) -> bool {
    BUILTINS.contains(&name)
}

/// 调用表达式文本形态（限深 3 防深链爆炸）：Name/Attribute 链、Call 加 "()"、
/// Subscript 加 "[]"；其余按节点种类名。
pub(crate) fn expr_text(n: &PyNode) -> String {
    pub(crate) fn go(n: &PyNode, d: usize) -> String {
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


// ── S168：按域拆出的子模块（子目录 nameres/；子模块用 use super::*; 取父作用域）
pub(crate) use self::{files::*, prescan::*};   // S168：子模块自由项（供同模块其它子模块用）
pub use self::{callgraph::callgraph_dir, files::{resolve_dir, resolve_file}};   // bin 走 nameres:: 的三个入口（它们本就是 pub）

mod resolver_scope;
mod resolver_walk;
mod files;
mod prescan;
mod callgraph;

/// S156：callgraph 预扫描单元（原 `callgraph_dir` 函数内 `struct Pre` 提升到模块级，
/// 以便被并行辅助函数复用；字段与语义一字不变）。
pub(crate) struct CgPre {
    pub(crate) rel: String,    pub(crate) modname: String,    pub(crate) ps: PreScan,    /// S155：阶段 1 的解析结果，阶段 2 直接复用（原先重读重解析，纯浪费）
    pub(crate) tree: PyNode,}

/// S156：阶段 1 单文件预扫描（原循环体逐字搬入；失败返回 None = 原 `continue`）。
pub(crate) fn prescan_one(p: &std::path::Path, root: &Path, prefix: Option<&str>) -> Option<CgPre> {
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
pub(crate) fn prescan_parallel(py: &[std::path::PathBuf], root: &Path, prefix: Option<&str>)
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
pub(crate) fn stitch_one(d: &Value, index: &HashMap<String, usize>, pre: &[CgPre])
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
