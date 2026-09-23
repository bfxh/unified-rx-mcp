//! ast_scan 原生实现（S84）——结构化扫描域：Python 真 AST / JS 词法掩码+调用面 /
//! Rust 词法掩码+结构化信号 + S16 跨文件可达性。
//!
//! 与 tools/astscan.py 逐条对齐（对照实验后 Python 侧退役为薄壳）：
//! - Python：pyast::parse_module（S83 迷你解析器，S84 扩展 col/字符串值/算子名），
//!   ast.walk 等价 BFS；Call 节点的 col = 链首 token（CPython 口径）
//! - JS：手写状态机掩码（char 口径保长度）+ 调用面提取（_CALL_RE 手写等价：
//!   前视非 [.\w$]、可选 new\s+、链 seg(.seg)*、\s*、'('），finditer 不重叠续扫
//! - Rust：掩码 + 每行结构信号 + fn 花括号归属 + risky_fns 排序 + S16 reach
//! - 输出键序严格按 Python dict 字面量序（json::Value::Obj 保序）
//!
//! 已知偏离（语料与真实仓库不触发，见 S84 对照实验）：
//! - 旧 Python 的 _mask_rust 遇 `r#` 后非引号（Rust 原始标识符，如 r#type）会
//!   死循环——Rust 侧修复为 i += 1 继续扫描（仓库内无此形态，oracle 不受影响）
//! - f-string 字面量段不是独立 Constant 节点（pyast 只保留内插区域）→
//!   secret_literal 对 f-string 文本段不触发
//! - 恶意/畸形转义（b"\u…"、截断 \x）的 SyntaxError msg 与 CPython 的
//!   带位置前缀版本不同；超大整型/浮点 dump 用原文回退
//! - dump_expr 未覆盖的节点型（comprehension/DictComp 等）回退为类型名——
//!   规则只在 func 为 shell 属性链时才 dump，真实代码链上是 Name/Attribute/Subscript

use crate::json::Value;
use crate::pyast::{parse_module, Ctx, CVal, PyNode};
use crate::scan::read_text;
use std::collections::VecDeque;
use std::path::Path;

pub const AST_MAX_FILES: usize = 200;
const EXT_SET: [&str; 5] = [".py", ".js", ".mjs", ".cjs", ".rs"];
const SKIP_DIRS: [&str; 5] = ["node_modules", ".git", "__pycache__", ".venv", "target"];
const PY_SINKS_NAME: [&str; 3] = ["eval", "exec", "compile"];
const PY_ATTR_SHELL: [&str; 4] = ["system", "popen", "Popen", "spawnSync"];
const JS_SINKS_BARE: [&str; 3] = ["eval", "exec", "execSync"];

fn s(v: &str) -> Value {
    Value::Str(v.to_string())
}
fn i(v: usize) -> Value {
    Value::Int(v as i128)
}
fn o(kvs: Vec<(&str, Value)>) -> Value {
    Value::Obj(kvs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}
fn err_obj(msg: &str) -> Value {
    o(vec![("error", s(msg))])
}

// ---------- 遍历（os.walk 等价：额度在 yield 目录元组时检查） ----------

/// os.path.splitext 等价（扩展名小写；点文件无扩展名）。
fn splitext_ext(name: &str) -> String {
    match name.rfind('.') {
        None | Some(0) => String::new(),
        Some(k) => name[k..].to_ascii_lowercase(),
    }
}

fn join_name(dir: &Path, name: &str) -> String {
    let mut s = dir.to_string_lossy().into_owned();
    if !s.ends_with('\\') && !s.ends_with('/') {
        s.push('\\');
    }
    s.push_str(name);
    s
}

fn walk_dir(dir: &Path, out: &mut Vec<String>, max: usize) {
    if out.len() >= max {
        return; // Python: for dp,... in os.walk: if len(targets) >= max: break
    }
    let rd = match std::fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => return,
    };
    let mut files: Vec<String> = Vec::new();
    let mut dirs: Vec<String> = Vec::new();
    for e in rd.filter_map(|e| e.ok()) {
        let name = e.file_name().to_string_lossy().into_owned();
        let Ok(ft) = e.file_type() else { continue };
        // os.walk 语义：符号链接解引用定类（断链跳过）
        let is_dir = if ft.is_symlink() {
            match std::fs::metadata(e.path()) {
                Ok(m) => m.is_dir(),
                Err(_) => continue,
            }
        } else {
            ft.is_dir()
        };
        if is_dir {
            dirs.push(name);
        } else {
            files.push(name);
        }
    }
    let by_upcase = |a: &String, b: &String| {
        a.to_uppercase().cmp(&b.to_uppercase()).then_with(|| a.cmp(b))
    };
    files.sort_by(by_upcase);
    dirs.sort_by(by_upcase);
    for name in files {
        if EXT_SET.contains(&splitext_ext(&name).as_str()) && out.len() < max {
            out.push(join_name(dir, &name));
        }
    }
    for name in dirs {
        if SKIP_DIRS.contains(&name.as_str()) {
            continue;
        }
        walk_dir(&dir.join(&name), out, max);
    }
}

// ---------- os.path 等价（relpath / dirname） ----------

fn abs_norm(p: &str) -> String {
    let mut s = p.replace('/', "\\");
    let rooted = (s.len() >= 2 && s.as_bytes()[1] == b':') || s.starts_with("\\\\");
    if !rooted {
        let cwd = std::env::current_dir()
            .map(|d| d.to_string_lossy().replace('/', "\\"))
            .unwrap_or_default();
        s = format!("{}\\{}", cwd.trim_end_matches('\\'), s);
    }
    let (prefix, rest): (String, Vec<String>) = if s.starts_with("\\\\") {
        let segs: Vec<&str> = s.split('\\').filter(|x| !x.is_empty()).collect();
        if segs.len() >= 2 {
            (
                format!("\\\\{}\\{}", segs[0], segs[1]),
                segs[2..].iter().map(|x| x.to_string()).collect(),
            )
        } else {
            (s.clone(), Vec::new())
        }
    } else if s.len() >= 2 && s.as_bytes()[1] == b':' {
        (
            s[..2].to_string(),
            s[2..].split('\\').filter(|x| !x.is_empty()).map(|x| x.to_string()).collect(),
        )
    } else {
        (String::new(), s.split('\\').filter(|x| !x.is_empty()).map(|x| x.to_string()).collect())
    };
    let mut parts: Vec<String> = Vec::new();
    for seg in rest {
        match seg.as_str() {
            "." => {}
            ".." => {
                parts.pop();
            }
            x => parts.push(x.to_string()),
        }
    }
    if prefix.is_empty() {
        parts.join("\\")
    } else {
        format!("{}\\{}", prefix, parts.join("\\"))
    }
}

/// os.path.relpath 等价（盘内后缀为主路径；跨树回退 .. 拼接）。
fn relpath(fp: &str, base: &str) -> String {
    let f = abs_norm(fp);
    let b = abs_norm(base);
    if f == b {
        return ".".into();
    }
    let bp = format!("{}\\", b);
    if f.starts_with(&bp) {
        return f[bp.len()..].to_string();
    }
    let fs: Vec<&str> = f.split('\\').collect();
    let bs: Vec<&str> = b.split('\\').collect();
    let mut k = 0;
    while k < fs.len() && k < bs.len() && fs[k].eq_ignore_ascii_case(bs[k]) {
        k += 1;
    }
    let mut out: Vec<String> = vec!["..".into(); bs.len() - k];
    out.extend(fs[k..].iter().map(|x| x.to_string()));
    out.join("\\")
}

fn py_dirname(p: &str) -> String {
    match p.rfind(['\\', '/']) {
        None => String::new(),
        Some(k) => p[..k].to_string(),
    }
}

// ---------- dump_expr（ast.dump 等价，None/空列表字段省略） ----------


// ── S168：按域拆出的子模块（子目录 astscan/；子模块用 use super::*; 取父作用域）
mod py;
mod js;
mod rust;
mod rust_reach;

pub(crate) use self::{js::*, py::*, rust::*, rust_reach::*};   // S168：子模块条目再导出（子模块的 use super::* 即可见）

/// 单块的扫描产物（issues / per_unit / rs_sources 三路，按序合并即与串行同）。
type ChunkOut = (Vec<Value>, Vec<Value>, Vec<(String, String, bool)>);

fn scan_chunk_body(files: &[String], base: &str) -> ChunkOut {
    let mut all_issues: Vec<Value> = Vec::new();
    let mut per_unit: Vec<Value> = Vec::new();
    let mut rs_sources: Vec<(String, String, bool)> = Vec::new();
    for fp in files {
        let Some(raw) = read_text(Path::new(fp)) else {
            continue; // OSError → 静默跳过（与旧实现一致）
        };
        // Python open(..., "r") 的 universal newlines 契约：\r\n → \n、孤立 \r → \n。
        // CRLF 文件否则在字符串掩码里 \ 先吞 \r、真 \n 反而截断字符串，行号全盘漂移。
        let src = raw.replace("\r\n", "\n").replace('\r', "\n");
        let fp_rel = relpath(fp, base);
        let lines = src.matches('\n').count() + 1;
        if fp.ends_with(".py") {
            let issues = scan_python(&src, &fp_rel);
            per_unit.push(o(vec![
                ("file", s(&fp_rel)),
                ("lang", s("python")),
                ("lines", i(lines)),
            ]));
            all_issues.extend(issues);
        } else if fp.ends_with(".rs") {
            let (masked, mstrings, mcomments) = mask_rust(&src);
            let (issues, meta) = scan_rust_struct(&masked, &fp_rel);
            // is_test_dir：fp_rel 路径组件（除文件名）含 tests/tests.*，或全路径含 \tests\
            let is_test_dir = {
                let fp_norm = fp_rel.replace('\\', "/");
                let comps: Vec<&str> = fp_norm.split('/').collect();
                comps[..comps.len().saturating_sub(1)]
                    .iter()
                    .any(|pp| *pp == "tests" || pp.starts_with("tests."))
                    || fp.contains("\\tests\\")
                    || fp.contains("/tests/")
            };
            rs_sources.push((fp_rel.clone(), src.clone(), is_test_dir));
            per_unit.push(o(vec![
                ("file", s(&fp_rel)),
                ("lang", s("rust")),
                ("lines", i(lines)),
                ("strings_masked", i(mstrings)),
                ("comments_masked", i(mcomments)),
                ("fn_count", i(meta.fn_count)),
                ("unsafe_count", i(meta.unsafe_count)),
                ("risky_fns", Value::Arr(meta.risky)),
                ("fns", Value::Arr(meta.fns.iter().map(|f| s(f)).collect())),
            ]));
            all_issues.extend(issues);
        } else {
            // 注意与旧实现一致的怪癖：单文件直扫不经扩展名过滤，.txt 也走 JS 管线；
            // 目录模式下 ".PY"（大写）能进 targets 但 endswith(".py") 不成立 → JS 管线
            let (masked, st_strings, st_templates, st_comments) = mask_js(&src);
            let (issues, calls_total) = scan_js_calls(&masked, &fp_rel);
            per_unit.push(o(vec![
                ("file", s(&fp_rel)),
                ("lang", s("javascript")),
                ("lines", i(lines)),
                ("strings_masked", i(st_strings)),
                ("templates_masked", i(st_templates)),
                ("comments_masked", i(st_comments)),
                ("calls_total", i(calls_total)),
            ]));
            all_issues.extend(issues);
        }
    }
    (all_issues, per_unit, rs_sources)
}

fn reach_annotate(all_issues: &mut [Value], rs_sources: &[(String, String, bool)], reach_summary: &mut Value) {
    let ReachResult { lmap, helpers } = rust_reach(rs_sources);
    // (file, fn) → reach 标注（仅三条 rust 规则；键序追加在 fn 之后）
    for it in all_issues.iter_mut() {
        let Value::Obj(kv) = it else { continue };
        let file = kv.iter().find(|(k, _)| k == "file").map(|(_, v)| match v {
            Value::Str(x) => x.clone(),
            _ => String::new(),
        });
        let fnv = kv.iter().find(|(k, _)| k == "fn").map(|(_, v)| match v {
            Value::Str(x) => x.clone(),
            _ => String::new(),
        });
        let rule = kv.iter().find(|(k, _)| k == "rule").map(|(_, v)| match v {
            Value::Str(x) => x.clone(),
            _ => String::new(),
        });
        if let (Some(file), Some(fnv), Some(rule)) = (file, fnv, rule)
            && matches!(
                rule.as_str(),
                "rust_unwrap_expect" | "rust_panic_macro" | "rust_unsafe"
            )
                && let Some((_, lst)) = lmap.iter().find(|(k, _)| *k == fnv)
                    && let Some((_, _, v)) =
                        lst.iter().find(|(f, _, _)| *f == file)
                    {
                        kv.push(("reach".into(), s(v)));
                    }
    }
    let mut c_prod = 0i128;
    let mut c_unref = 0i128;
    for (_, lst) in &lmap {
        for (_, _, v) in lst {
            match *v {
                "prod" => c_prod += 1,
                "unreferenced" => c_unref += 1,
                _ => {}
            }
        }
    }
    let mut entries: Vec<Value> = Vec::new();
    for (k, lst) in &lmap {
        for (f, l, v) in lst {
            entries.push(o(vec![
                ("fn", s(k)),
                ("file", s(f)),
                ("line", i(*l)),
                ("reach", s(v)),
            ]));
        }
    }
    entries.sort_by_key(|e| {
        let Value::Obj(kv) = e else { return (0u8, 0u8, String::new()) };
        let get = |key: &str| -> String {
            kv.iter()
                .find(|(k, _)| k == key)
                .map(|(_, v)| match v {
                    Value::Str(x) => x.clone(),
                    _ => String::new(),
                })
                .unwrap_or_default()
        };
        let reach = get("reach");
        let file = get("file");
        (
            (reach != "test_only") as u8,
            (reach != "unreferenced") as u8,
            file,
        )
    });
    entries.truncate(60);
    let defs_evaluated: usize = lmap.iter().map(|(_, lst)| lst.len()).sum();
    *reach_summary = o(vec![
        ("defs_evaluated", i(defs_evaluated)),
        (
            "by_reach",
            o(vec![
                ("prod", Value::Int(c_prod)),
                ("test_only", Value::Int(helpers.len() as i128)),
                ("unreferenced", Value::Int(c_unref)),
            ]),
        ),
        (
            "test_only_helpers",
            Value::Arr(helpers.into_iter().take(30).collect()),
        ),
        ("entries", Value::Arr(entries)),
    ]);
}

pub fn ast_scan(path: &str, max_files: usize) -> Value {
    let p = Path::new(path);
    if !p.exists() {
        return err_obj(&format!("路径不存在: {path}"));
    }
    let mut targets: Vec<String> = Vec::new();
    if p.is_file() {
        targets.push(path.to_string());
    } else {
        walk_dir(p, &mut targets, max_files);
    }
    if targets.is_empty() {
        return err_obj("无可扫目标（仅支持 .py/.js/.mjs/.cjs/.rs）");
    }
    let base = if p.is_dir() { path.to_string() } else { py_dirname(path) };

    // S166：分块并行（同 S153 `bug.rs::scan_files` 范式——文件 <8 或并行度 ≤1 走串行，
    // **按块序合并**三个向量 ⇒ 输出与串行逐字节同；`UNIFIED_RX_NO_PAR=1` 强制串行做 A/B）。
    // 动机（2026-09-23 实测）：1446 文件语料上本函数原为纯串行，并行/串行 = 1.02×（没吃多核）。
    // 跨文件可达性（rust_reach）仍在第二阶段串行跑——它本来就要全量 rs_sources 才能开工。
    let n = crate::par::par_degree(0); // S167：0 = 用满可用并行度（按机器来）
    let (mut all_issues, per_unit, rs_sources) = if targets.len() < 8 || n <= 1 {
        scan_chunk_body(&targets, &base)
    } else {
        let chunk = targets.len().div_ceil(n);
        let mut a: Vec<Value> = Vec::new();
        let mut u: Vec<Value> = Vec::new();
        let mut r: Vec<(String, String, bool)> = Vec::new();
        let base_ref = &base; // 先取引用：inner 是 move 闭包，直接写 &base 会被判为移出
        std::thread::scope(|s| {
            let handles: Vec<_> = targets
                .chunks(chunk)
                .map(|c| s.spawn(move || scan_chunk_body(c, base_ref)))
                .collect();
            for h in handles {
                let (mut a2, mut u2, mut r2) = h.join().unwrap_or_default();
                a.append(&mut a2);
                u.append(&mut u2);
                r.append(&mut r2);
            }
        });
        (a, u, r)
    };

    let mut reach_summary: Value = Value::Null;
    if !rs_sources.is_empty() {
        reach_annotate(&mut all_issues, &rs_sources, &mut reach_summary);
    }

    let mut by_rule: Vec<(String, i128)> = Vec::new();
    for it in &all_issues {
        let Value::Obj(kv) = it else { continue };
        let Some((_, Value::Str(rule))) = kv.iter().find(|(k, _)| k == "rule") else {
            continue;
        };
        match by_rule.iter_mut().find(|(k, _)| k == rule) {
            Some(e) => e.1 += 1,
            None => by_rule.push((rule.clone(), 1)),
        }
    }
    let total = all_issues.len();
    // files = 截断前的 units 数（Python: len(per_unit)，units 只是 [:200] 视图）
    let files_count = per_unit.len();
    let mut units = per_unit;
    units.truncate(200);
    o(vec![
        ("files", i(files_count)),
        ("total", i(total)),
        ("by_rule", Value::Obj(by_rule.into_iter().map(|(k, v)| (k, Value::Int(v))).collect())),
        ("issues", Value::Arr(all_issues)),
        ("units", Value::Arr(units)),
        (
            "layer_note",
            s("layer=structural（token/call 级）；上层聚合请基于 issues 自行收敛"),
        ),
        ("rust_reach", reach_summary),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relpath_suffix_and_dot() {
        assert_eq!(relpath("D:\\x\\a\\b.py", "D:\\x"), "a\\b.py");
        assert_eq!(relpath("D:\\x\\b.py", "D:\\x\\b.py"), ".");
    }

    #[test]
    fn secret_shape_matches_and_masks_detail() {
        let key = format!("sk-{}", "a".repeat(30));
        let (m, _) = find_secret(&key).unwrap();
        assert_eq!(m, key);
        // 词边界：前缀粘连不算
        assert!(find_secret(&format!("x{key}")).is_none());
        // gh / AKIA 形
        assert!(find_secret(&format!("ghp_{}", "a".repeat(30))).is_some());
        assert!(find_secret(&format!("AKIA{}", "A".repeat(16))).is_some());
        assert!(find_secret("AKIA123").is_none());
    }

    #[test]
    fn python_rules_cols_and_kinds() {
        let issues = scan_python("eval(\"1+1\")\nexec(user)\n", "t.py");
        let kinds: Vec<&str> = issues
            .iter()
            .map(|v| match v {
                Value::Obj(kv) => kv
                    .iter()
                    .find(|(k, _)| k == "arg_kind")
                    .map(|(_, v)| match v {
                        Value::Str(x) => x.as_str(),
                        _ => "",
                    })
                    .unwrap_or(""),
                _ => "",
            })
            .collect();
        assert_eq!(kinds, vec!["literal", "dynamic"]);
        // col = 链首：(eval)(x) 的 Call col 指到 '('（col=4），os.system 在 col=0
        let issues2 = scan_python("y = (eval)(x)\nos.system(cmd)\n", "t.py");
        let cols: Vec<i128> = issues2
            .iter()
            .map(|v| match v {
                Value::Obj(kv) => kv
                    .iter()
                    .find(|(k, _)| k == "col")
                    .map(|(_, v)| match v {
                        Value::Int(x) => *x,
                        _ => -1,
                    })
                    .unwrap_or(-1),
                _ => -1,
            })
            .collect();
        // shell_like 的 callee 是 ast.dump 形态
        let callee = match &issues2[1] {
            Value::Obj(kv) => match &kv.iter().find(|(k, _)| k == "callee").unwrap().1 {
                Value::Str(x) => x.clone(),
                _ => String::new(),
            },
            _ => String::new(),
        };
        assert_eq!(
            callee,
            // ast.dump(fn)[:60]：60 字符截断（Python 侧口径）
            "Attribute(value=Name(id='os', ctx=Load()), attr='system', ct"
        );
        assert_eq!(cols, vec![4, 0]); // Call col 以链首为准
    }

    #[test]
    fn js_pipeline_mask_and_calls() {
        let src = "const a = \"eval(x)\";\nexec(cmd);\nconst f = new Function(\"return 1\");\n";
        let (masked, st, tp, cm) = mask_js(src);
        assert_eq!(st, 2);
        assert_eq!(tp, 0);
        assert_eq!(cm, 0);
        assert!(masked.iter().collect::<String>().contains("exec(cmd)"));
        let (issues, total) = scan_js_calls(&masked, "t.js");
        // 掩码后字符串内容不计：exec(cmd) + new Function( 共 2 个调用面
        assert_eq!(total, 2);
        let rules: Vec<String> = issues
            .iter()
            .map(|v| match v {
                Value::Obj(kv) => kv
                    .iter()
                    .find(|(k, _)| k == "rule")
                    .map(|(_, v)| match v {
                        Value::Str(x) => x.clone(),
                        _ => String::new(),
                    })
                    .unwrap_or_default(),
                _ => String::new(),
            })
            .collect();
        assert_eq!(rules, vec!["js_dynamic_exec", "js_new_function"]);
    }

    #[test]
    fn rust_struct_and_reach_smoke() {
        let src = "fn dirty() {\n    x.unwrap();\n    unsafe { z() }\n}\n";
        let (masked, _, _) = mask_rust(src);
        let (issues, meta) = scan_rust_struct(&masked, "t.rs");
        assert_eq!(meta.fn_count, 1);
        assert_eq!(meta.unsafe_count, 1);
        assert_eq!(issues.len(), 2);
        let fns: Vec<String> = issues
            .iter()
            .map(|v| match v {
                Value::Obj(kv) => kv
                    .iter()
                    .find(|(k, _)| k == "fn")
                    .map(|(_, v)| match v {
                        Value::Str(x) => x.clone(),
                        _ => String::new(),
                    })
                    .unwrap_or_default(),
                _ => String::new(),
            })
            .collect();
        assert!(fns.iter().all(|f| f == "dirty"));
    }
}
