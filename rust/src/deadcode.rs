//! deadcode —— 死符号可达性原生化（S135，HARDENING §六 候选二）。
//!
//! 语义与 tools/ide_deadcode.py（S123）**逐字节对齐**（对照实验为删码依据）：
//! 全库 ast 扫描，报"零引用"的顶层函数/类/私有方法；保守口径：
//! - 引用 = 所有 .py 出现的 ast.Name（任意 ctx）/ ast.Attribute 名（跨文件算活）；
//! - 名字出现在任意字符串字面量 → suspect_dynamic（不判死）；
//! - 带装饰器定义默认豁免（include_decorated=true 才纳入）；pytest 约定入口
//!   （test_*/pytest_*/setup_*/teardown_* 前缀与 conftest.py）豁免；
//! - 类只收**私有**方法（单下划线、非双下划线、无装饰器）；嵌套/dunder 不查；
//! - dead/suspect 按 (file, line) 排序；dead 截断 max_results、suspect 恒 50。
//!
//! 实现：复用 pyast.rs 迷你解析器（3.14 语义；S135 为其加 `deco` 装饰器计数）；
//! 文件遍历 skip 集与 `_walk_py` 逐字一致（venv/.tox/pytest 缓存等）。
//! 已知边界（如实）：parse_errors 的 error 文本为 pyast 报文（Python 版是
//! SyntaxError 文本）——形状一致、字数不同；多坏文件的 errors 顺序按各自遍历序。

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use crate::json::Value;
use crate::pyast::{self, CVal, PyNode};

const MAX_FILE_KB: u64 = 1024;
const PYTEST_PREFIXES: [&str; 4] = ["test_", "pytest_", "setup_", "teardown_"];

/// ide_common._SKIP_DIRS ∪ {venv,.venv,site-packages,.tox,.mypy_cache,.pytest_cache,.ruff_cache}
const SKIP_DIRS: [&str; 20] = [
    ".git", "node_modules", "target", "__pycache__", "dist", "build",
    ".unified-rx-index", ".codegraph", "backups", "assets", "data", "models",
    "docs", "venv", ".venv", "site-packages", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache",
];

fn walk_py(root: &Path, max_files: usize) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        let rd = match fs::read_dir(&d) {
            Ok(r) => r,
            Err(_) => continue,
        };
        let mut subdirs = Vec::new();
        for e in rd.flatten() {
            let p = e.path();
            let name = e.file_name().to_string_lossy().to_string();
            if p.is_dir() {
                if !SKIP_DIRS.contains(&name.as_str()) {
                    subdirs.push(p);
                }
            } else if name.ends_with(".py") {
                if out.len() >= max_files {
                    return out;
                }
                out.push(p);
            }
        }
        subdirs.sort();
        for d in subdirs.into_iter().rev() {
            stack.push(d);
        }
    }
    out
}

/// 类体切片：children[aux..len-deco] 中剔除 kind=="keyword"（class 的 **kwargs）。
fn class_body(c: &PyNode) -> Vec<&PyNode> {
    let end = c.children.len().saturating_sub(c.deco);
    let start = c.aux.min(end);
    c.children[start..end]
        .iter()
        .filter(|n| n.kind != "keyword")
        .collect()
}

type DefRow = (String, usize, &'static str, bool); // (name, line, kind, decorated)

fn collect_defs(root: &PyNode, out: &mut Vec<DefRow>) {
    for c in &root.children {
        match c.kind {
            "FunctionDef" | "AsyncFunctionDef" => {
                out.push((c.name.clone(), c.line, "function", c.deco > 0));
            }
            "ClassDef" => {
                out.push((c.name.clone(), c.line, "class", c.deco > 0));
                for sub in class_body(c) {
                    if matches!(sub.kind, "FunctionDef" | "AsyncFunctionDef")
                        && sub.name.starts_with('_')
                        && !sub.name.starts_with("__")
                        && sub.deco == 0
                    {
                        out.push((sub.name.clone(), sub.line, "method", false));
                    }
                }
            }
            _ => {}
        }
    }
}

fn shorthand_refs(
    root: &PyNode, names: &mut HashSet<String>, attrs: &mut HashSet<String>,
    strings: &mut Vec<String>,
) {
    let mut q: Vec<&PyNode> = vec![root];
    while let Some(n) = q.pop() {
        for c in &n.children {
            q.push(c);
        }
        match n.kind {
            "Name" => {
                names.insert(n.name.clone());
            }
            "Attribute" => {
                attrs.insert(n.name.clone());
            }
            "Constant" => {
                if let CVal::Str(s) = &n.cval {
                    strings.push(s.clone());
                }
            }
            _ => {}
        }
    }
}

fn is_pytest_entry(name: &str, kind: &str, basename: &str) -> bool {
    (kind == "function" || kind == "method")
        && (PYTEST_PREFIXES.iter().any(|p| name.starts_with(p)) || basename == "conftest.py")
}

/// 扫描目录 → 结果 json（root/elapsed/note 由薄壳补；键序与 Python 版一致）。
pub fn dead_code_scan(
    root: &Path, max_files: usize, max_results: usize, include_decorated: bool,
) -> Value {
    if !root.is_dir() {
        return Value::Obj(vec![(
            "error".into(),
            Value::Str(format!("目录不存在: {}", root.display())),
        )]);
    }
    let mut files = walk_py(root, max_files);
    files.sort(); // 结果排序后等价；遍历序仅影响截断边界与 errors 顺序（模块注记）

    let mut ref_names: HashSet<String> = HashSet::new();
    let mut ref_attrs: HashSet<String> = HashSet::new();
    let mut all_strings: Vec<String> = Vec::new();
    let mut defs: Vec<(String, DefRow)> = Vec::new(); // (rel, row)
    let mut files_scanned = 0usize;
    let mut parse_errors: Vec<Value> = Vec::new();

    for fp in &files {
        let rel = fp
            .strip_prefix(root)
            .unwrap_or(fp)
            .to_string_lossy()
            .replace('/', "\\"); // Python os.path.relpath 在 Windows 产出反斜杠
        if let Ok(md) = fs::metadata(fp)
            && md.len() > MAX_FILE_KB * 1024
        {
            continue;
        }
        let bytes = match fs::read(fp) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let src = String::from_utf8_lossy(&bytes).to_string();
        let tree = match pyast::parse_module(&src) {
            Ok(t) => t,
            Err(e) => {
                parse_errors.push(Value::Obj(vec![
                    ("file".into(), Value::Str(rel)),
                    ("error".into(), Value::Str(format!("{}（line {}）", e.msg, e.line))),
                ]));
                continue;
            }
        };
        files_scanned += 1;
        let mut rows: Vec<DefRow> = Vec::new();
        collect_defs(&tree, &mut rows);
        for r in rows {
            defs.push((rel.clone(), r));
        }
        shorthand_refs(&tree, &mut ref_names, &mut ref_attrs, &mut all_strings);
    }

    let blob = all_strings.join("\n");
    let mut dead: Vec<Value> = Vec::new();
    let mut suspect: Vec<Value> = Vec::new();
    let mut dead_lines: Vec<(String, usize)> = Vec::new();
    let mut suspect_lines: Vec<(String, usize)> = Vec::new();
    let mut exempted_decorated = 0usize;
    let mut exempted_pytest = 0usize;

    for (rel, (name, line, kind, decorated)) in &defs {
        if name.starts_with("__") && name.ends_with("__") {
            continue;
        }
        if *decorated && !include_decorated {
            exempted_decorated += 1;
            continue;
        }
        let basename = rel.rsplit(['\\', '/']).next().unwrap_or("");
        if is_pytest_entry(name, kind, basename) {
            exempted_pytest += 1;
            continue;
        }
        if ref_names.contains(name) || ref_attrs.contains(name) {
            continue;
        }
        let entry = |extra_hint: bool| {
            let mut pairs = vec![
                ("file".into(), Value::Str(rel.clone())),
                ("line".into(), Value::Int(*line as i128)),
                ("name".into(), Value::Str(name.clone())),
                ("kind".into(), Value::Str((*kind).to_string())),
            ];
            if extra_hint {
                pairs.push((
                    "hint".into(),
                    Value::Str(
                        "名字出现在字符串字面量（getattr/注册表/__all__），静态无法定论"
                            .to_string(),
                    ),
                ));
            }
            Value::Obj(pairs)
        };
        if blob.contains(name.as_str()) {
            suspect_lines.push((rel.clone(), *line));
            suspect.push(entry(true));
        } else {
            dead_lines.push((rel.clone(), *line));
            dead.push(entry(false));
        }
    }

    // 稳定排序 (file, line)：先按键排索引序，再重排两个列表
    let order = |lines: &[(String, usize)]| {
        let mut idx: Vec<usize> = (0..lines.len()).collect();
        idx.sort_by(|&a, &b| lines[a].cmp(&lines[b]));
        idx
    };
    let di = order(&dead_lines);
    let si = order(&suspect_lines);
    let dead_sorted: Vec<Value> = di.iter().map(|&i| dead[i].clone()).collect();
    let suspect_sorted: Vec<Value> = si.iter().map(|&i| suspect[i].clone()).collect();
    let dead_count = dead_sorted.len();
    let truncated = dead_count > max_results;
    let dead_out: Vec<Value> = dead_sorted.into_iter().take(max_results).collect();
    let suspect_out: Vec<Value> = suspect_sorted.into_iter().take(50).collect();

    let parse_err_out: Vec<Value> = parse_errors.into_iter().take(10).collect();
    Value::Obj(vec![
        ("files_scanned".into(), Value::Int(files_scanned as i128)),
        ("parse_errors".into(), Value::Arr(parse_err_out)),
        ("defs_total".into(), Value::Int(defs.len() as i128)),
        ("dead_count".into(), Value::Int(dead_count as i128)),
        ("dead".into(), Value::Arr(dead_out)),
        ("suspect_dynamic".into(), Value::Arr(suspect_out)),
        ("exempted_decorated".into(), Value::Int(exempted_decorated as i128)),
        ("exempted_pytest_entry".into(), Value::Int(exempted_pytest as i128)),
        ("truncated".into(), Value::Bool(truncated)),
    ])
}
