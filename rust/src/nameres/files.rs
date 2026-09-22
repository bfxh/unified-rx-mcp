//! nameres 子模块（S168 从 nameres.rs 拆出；纯搬移，未改语义）。
use super::*;


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

pub(crate) const SKIP_DIRS: [&str; 8] = [".git", "node_modules", "target", "__pycache__",
    "dist", "build", ".venv", "venv"];

pub(crate) fn walk_py(dir: &Path, out: &mut Vec<std::path::PathBuf>, max: usize) {
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
pub(crate) fn rel_mod(rel: &str) -> String {
    let stem = rel.strip_suffix(".py").unwrap_or(rel).replace('\\', "/");
    let mut parts: Vec<&str> = stem.split('/').filter(|x| !x.is_empty()).collect();
    if parts.last() == Some(&"__init__") {
        parts.pop();
    }
    parts.join(".")
}

pub(crate) fn pkg_of(modname: &str) -> String {
    match modname.rfind('.') {
        Some(i) => modname[..i].to_string(),
        None => String::new(),
    }
}

pub(crate) struct FileRes {
    pub(crate) rel: String,    pub(crate) modname: String,    pub(crate) bindings: HashMap<String, (usize, &'static str)>,    pub(crate) imports: Vec<ImportFact>,}

pub(crate) fn analyze_file(root: &Path, p: &Path) -> Option<FileRes> {
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

