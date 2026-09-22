//! nameres 子模块（S168 从 nameres.rs 拆出；纯搬移，未改语义）。
use super::*;


/// 预扫描结果：模块级/类级绑定表 + import 事实。
/// 只扫 def/class/import/赋值目标（不下函数体）——前向引用（互递归、先调用后定义）
/// 在主遍历开始前即有种子；函数体内的顺序绑定由主遍历按序处理（局部互递归是
/// 文档化边界，见 spec/CALLGRAPH.md）。
pub(crate) struct PreScan {
    pub(crate) module: HashMap<String, (usize, &'static str)>,    pub(crate) classes: HashMap<String, HashMap<String, (usize, &'static str)>>,    pub(crate) imports: Vec<ImportFact>,    pub(crate) star: bool,}

pub(crate) fn table_bind(ps: &mut PreScan, level: &str, name: &str, line: usize, kind: &'static str) {
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

pub(crate) fn target_names(n: &PyNode, out: &mut Vec<(String, usize)>) {
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

pub(crate) fn child_label_of(level: &str, name: &str) -> String {
    if level == "module" {
        name.to_string()
    } else {
        format!("{}.{}", level, name)
    }
}

pub(crate) fn prenode(n: &PyNode, level: &str, ps: &mut PreScan) {
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

pub(crate) fn vstr(v: &Value, k: &str) -> String {
    match v.get(k) {
        Some(Value::Str(s)) => s.clone(),
        _ => String::new(),
    }
}

pub(crate) fn vint(v: &Value, k: &str) -> usize {
    match v.get(k) {
        Some(Value::Int(i)) => (*i).max(0) as usize,
        _ => 0,
    }
}

