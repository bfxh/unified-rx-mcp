//! rx-ide —— ide_read 双件原生化（S92）：ide_outline / ide_read_symbol。
//!
//! 唯一实现在此（tools/ide_read.py 退役为薄壳）。语义逐条对齐
//! tools/scan.py::_symbol_spans（S70）+ tools/ide_read.py（S66）：
//! - 四语言行级正则的零依赖手写复刻（\w = unicode alphanumeric + '_'；
//!   \s = is_whitespace；贪婪可选组先试、失败回退，与正则回溯同序）；
//! - split('\n') 保留尾幻影行（Python str.split 语义——read_symbol 的 content
//!   对未闭合符号会把幻影行拼进末尾，字节级对齐）；
//! - params：outline 只在 kind=="fn" 且 header 有括号时数逗号+1；
//!   read_symbol 只看括号不看 kind（旧实现两条路各查各的）；
//! - brace 语言（rust/go/js）仅列 0 起点做 `{}` 深度回 0 定真尾
//!   （上限 i+FUNC_LONG*6）；python 一律缩进回归定 body 末行；
//!   默认跨度 = 下一符号起点，末位 i+FUNC_LONG*4；
//! - 怪癖原样保留：js class 落 kind="fn"（type 判定只搜
//!   struct/enum/trait/impl）；rust/go 列 0 才捕获、python/js 允许缩进；
//!   `impl<T>` 泛型头不捕获；一行 `fn q() { struct S; }` 的 q 翻 kind=type。
//! 沙盒门：SandboxCfg::resolve（Err → exit 2 → Python 壳 raise ValueError），
//! 与 fs.rs 同纪律；文件级错误走 exit 0 + {"error": ...}（registry 转 ok:false）。

use crate::json::Value;
use crate::sandbox::SandboxCfg;
use crate::scan::{lang_of, read_text};
use std::borrow::Cow;
use std::path::Path;

const OUTLINE_CAP: usize = 300;
/// tools/scan.py::_FUNC_LONG
const FUNC_LONG: usize = 80;

fn err_obj(msg: &str) -> Value {
    Value::Obj(vec![("error".into(), Value::Str(msg.into()))])
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

fn starts_with_at(cs: &[char], i: usize, s: &str) -> bool {
    let mut j = i;
    for c in s.chars() {
        if cs.get(j) != Some(&c) {
            return false;
        }
        j += 1;
    }
    true
}

/// \s*：返回前导空白之后的下标。
fn leading_ws(cs: &[char]) -> usize {
    let mut j = 0;
    while j < cs.len() && cs[j].is_whitespace() {
        j += 1;
    }
    j
}

/// \s+：至少一个空白，失败 None。
fn ws_at(cs: &[char], i: usize) -> Option<usize> {
    let mut j = i;
    while j < cs.len() && cs[j].is_whitespace() {
        j += 1;
    }
    if j == i {
        None
    } else {
        Some(j)
    }
}

/// \w+：贪婪标识符（unicode alphanumeric + '_'）。
fn ident_at(cs: &[char], i: usize) -> Option<(String, usize)> {
    let mut j = i;
    while j < cs.len() && is_word(cs[j]) {
        j += 1;
    }
    if j == i {
        None
    } else {
        Some((cs[i..j].iter().collect(), j))
    }
}

fn indent_of(line: &str) -> usize {
    leading_ws(&line.chars().collect::<Vec<char>>())
}

// ---------- python：^(\s*)(?:async\s+)?def\s+(\w+)|^(\s*)class\s+(\w+) ----------

fn match_python(line: &str) -> Option<(String, bool)> {
    let cs: Vec<char> = line.chars().collect();
    let i0 = leading_ws(&cs);
    if let Some(name) = py_def(&cs, i0) {
        return Some((name, false));
    }
    if starts_with_at(&cs, i0, "class") {
        if let Some(j) = ws_at(&cs, i0 + 5) {
            if let Some((name, _)) = ident_at(&cs, j) {
                return Some((name, true));
            }
        }
    }
    None
}

fn py_def(cs: &[char], i0: usize) -> Option<String> {
    if starts_with_at(cs, i0, "async") {
        if let Some(j) = ws_at(cs, i0 + 5) {
            if let Some(name) = py_def_after(cs, j) {
                return Some(name);
            }
        }
    }
    py_def_after(cs, i0)
}

fn py_def_after(cs: &[char], i: usize) -> Option<String> {
    if !starts_with_at(cs, i, "def") {
        return None;
    }
    let j = ws_at(cs, i + 3)?;
    ident_at(cs, j).map(|(n, _)| n)
}

// ---------- rust：^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+) ----------
//            |^(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|impl)\s+(\w+)

fn match_rust(line: &str) -> Option<String> {
    let cs: Vec<char> = line.chars().collect();
    rust_fn(&cs, 0).or_else(|| rust_type(&cs, 0))
}

/// (?:pub(?:\([^)]*\))?\s+)? —— pub 组整体贪婪先试；括号未闭合则组失败回退无 pub。
fn after_pub(cs: &[char], i: usize) -> Option<usize> {
    if !starts_with_at(cs, i, "pub") {
        return None;
    }
    let mut j = i + 3;
    if cs.get(j) == Some(&'(') {
        let mut k = j + 1;
        while k < cs.len() && cs[k] != ')' {
            k += 1;
        }
        if k < cs.len() {
            j = k + 1;
        }
    }
    ws_at(cs, j)
}

fn rust_fn(cs: &[char], i: usize) -> Option<String> {
    if let Some(j) = after_pub(cs, i) {
        if let Some(name) = rust_fn_after_async(cs, j) {
            return Some(name);
        }
    }
    rust_fn_after_async(cs, i)
}

fn rust_fn_after_async(cs: &[char], i: usize) -> Option<String> {
    if starts_with_at(cs, i, "async") {
        if let Some(j) = ws_at(cs, i + 5) {
            if let Some(name) = rust_fn_after(cs, j) {
                return Some(name);
            }
        }
    }
    rust_fn_after(cs, i)
}

fn rust_fn_after(cs: &[char], i: usize) -> Option<String> {
    if !starts_with_at(cs, i, "fn") {
        return None;
    }
    let j = ws_at(cs, i + 2)?;
    ident_at(cs, j).map(|(n, _)| n)
}

fn rust_type(cs: &[char], i: usize) -> Option<String> {
    if let Some(j) = after_pub(cs, i) {
        if let Some(name) = rust_type_kw(cs, j) {
            return Some(name);
        }
    }
    rust_type_kw(cs, i)
}

fn rust_type_kw(cs: &[char], i: usize) -> Option<String> {
    for kw in ["struct", "enum", "trait", "impl"] {
        if starts_with_at(cs, i, kw) {
            if let Some(j) = ws_at(cs, i + kw.len()) {
                if let Some((name, _)) = ident_at(cs, j) {
                    return Some(name);
                }
            }
        }
    }
    None
}

// ---------- go：^func\s+(?:\([^)]*\)\s*)?(\w+)|^type\s+(\w+)\s* ----------

fn match_go(line: &str) -> Option<String> {
    let cs: Vec<char> = line.chars().collect();
    if starts_with_at(&cs, 0, "func") {
        if let Some(j) = ws_at(&cs, 4) {
            // 接收器组贪婪先试：(recv)\s* 零或多空白；组缺席回退裸 ident
            if cs.get(j) == Some(&'(') {
                let mut k = j + 1;
                while k < cs.len() && cs[k] != ')' {
                    k += 1;
                }
                if k < cs.len() {
                    let mut m = k + 1;
                    while m < cs.len() && cs[m].is_whitespace() {
                        m += 1;
                    }
                    if let Some((name, _)) = ident_at(&cs, m) {
                        return Some(name);
                    }
                }
            }
            if let Some((name, _)) = ident_at(&cs, j) {
                return Some(name);
            }
        }
    }
    if starts_with_at(&cs, 0, "type") {
        if let Some(j) = ws_at(&cs, 4) {
            if let Some((name, _)) = ident_at(&cs, j) {
                return Some(name);
            }
        }
    }
    None
}

// ---------- javascript：^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)|^\s*class\s+(\w+) ----------

fn match_js(line: &str) -> Option<String> {
    let cs: Vec<char> = line.chars().collect();
    let i0 = leading_ws(&cs);
    if let Some(name) = js_export(&cs, i0) {
        return Some(name);
    }
    if starts_with_at(&cs, i0, "class") {
        if let Some(j) = ws_at(&cs, i0 + 5) {
            if let Some((name, _)) = ident_at(&cs, j) {
                return Some(name);
            }
        }
    }
    None
}

fn js_export(cs: &[char], i0: usize) -> Option<String> {
    if starts_with_at(cs, i0, "export") {
        if let Some(j) = ws_at(cs, i0 + 6) {
            if let Some(name) = js_fn(&cs, j) {
                return Some(name);
            }
        }
    }
    js_fn(cs, i0)
}

fn js_fn(cs: &[char], i: usize) -> Option<String> {
    if starts_with_at(cs, i, "async") {
        if let Some(j) = ws_at(cs, i + 5) {
            if let Some(name) = js_fn_after(cs, j) {
                return Some(name);
            }
        }
    }
    js_fn_after(cs, i)
}

fn js_fn_after(cs: &[char], i: usize) -> Option<String> {
    if !starts_with_at(cs, i, "function") {
        return None;
    }
    let j = ws_at(cs, i + 8)?;
    ident_at(cs, j).map(|(n, _)| n)
}

// ---------- kind 判定与跨度 ----------

/// rust/go/js 的 kind：整行搜 \b(struct|enum|trait|impl)\s+\w（python 不走此路）。
fn has_type_kw(line: &str) -> bool {
    let cs: Vec<char> = line.chars().collect();
    for kw in ["struct", "enum", "trait", "impl"] {
        let kwcs: Vec<char> = kw.chars().collect();
        let mut from = 0;
        while let Some(p) = find_sub(&cs, &kwcs, from) {
            if p == 0 || !is_word(cs[p - 1]) {
                if let Some(j) = ws_at(&cs, p + kwcs.len()) {
                    if j < cs.len() && is_word(cs[j]) {
                        return true;
                    }
                }
            }
            from = p + 1;
        }
    }
    false
}

fn find_sub(cs: &[char], pat: &[char], from: usize) -> Option<usize> {
    if pat.is_empty() || cs.len() < pat.len() {
        return None;
    }
    let mut i = from;
    while i + pat.len() <= cs.len() {
        if cs[i..i + pat.len()] == *pat {
            return Some(i);
        }
        i += 1;
    }
    None
}

/// Python str.split('\n')：universal newlines 归一后逐段切，保留尾幻影行。
pub(crate) fn split_py_lines(text: &str) -> Vec<String> {
    let t: Cow<str> = if text.contains('\r') {
        Cow::Owned(text.replace("\r\n", "\n").replace('\r', "\n"))
    } else {
        Cow::Borrowed(text)
    };
    t.split('\n').map(|s| s.to_string()).collect()
}

pub(crate) struct Span {
    pub name: String,
    pub start: usize,
    pub end: usize,
    pub kind: &'static str,
}

pub(crate) fn symbol_spans(lines: &[String], lang: &str) -> Vec<Span> {
    let brace_lang = matches!(lang, "rust" | "go" | "javascript");
    let mut starts: Vec<(String, usize, &'static str)> = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        let (name, kind) = match lang {
            "python" => match match_python(line) {
                Some((n, is_class)) => (n, if is_class { "type" } else { "fn" }),
                None => continue,
            },
            "rust" => match match_rust(line) {
                Some(n) => (n, if has_type_kw(line) { "type" } else { "fn" }),
                None => continue,
            },
            "go" => match match_go(line) {
                Some(n) => (n, if has_type_kw(line) { "type" } else { "fn" }),
                None => continue,
            },
            "javascript" => match match_js(line) {
                Some(n) => (n, if has_type_kw(line) { "type" } else { "fn" }),
                None => continue,
            },
            _ => return Vec::new(),
        };
        starts.push((name, i, kind));
    }
    let mut spans = Vec::new();
    for (j, (name, i, kind)) in starts.iter().enumerate() {
        let i = *i;
        let mut end = if j + 1 < starts.len() {
            starts[j + 1].1
        } else {
            lines.len().min(i + FUNC_LONG * 4)
        };
        if brace_lang {
            let first = lines[i].chars().next();
            if first != Some(' ') && first != Some('\t') {
                let cap = lines.len().min(i + FUNC_LONG * 6);
                let mut depth: i64 = 0;
                let mut opened = false;
                for k in i..cap {
                    let l = &lines[k];
                    depth += l.matches('{').count() as i64 - l.matches('}').count() as i64;
                    if l.contains('{') {
                        opened = true;
                    }
                    if opened && depth <= 0 {
                        end = k + 1;
                        break;
                    }
                }
            }
        } else if lang == "python" {
            let ind = indent_of(&lines[i]);
            let mut last_body = i;
            for k in i + 1..lines.len() {
                let l = &lines[k];
                if !l.trim().is_empty() {
                    let cur = indent_of(l);
                    if cur <= ind {
                        break;
                    }
                    last_body = k;
                }
            }
            end = last_body + 1;
        }
        spans.push(Span {
            name: name.clone(),
            start: i,
            end,
            kind,
        });
    }
    spans
}

// ---------- 装载（_load 等价） ----------

enum LoadErr {
    /// 沙盒 resolve 拒绝 → exit 2 → Python 壳 ValueError
    Sandbox(String),
    /// 文件级错误 → exit 0 + {"error": ...}
    Tool(Value),
}

fn load(cfg: &SandboxCfg, orig: &str) -> Result<(String, &'static str, Vec<String>), LoadErr> {
    // 与 tools/fs.py::_resolve 首道校验逐字对齐（旧 ide 路径同经 _resolve）
    if orig.is_empty() {
        return Err(LoadErr::Sandbox("path 必填".into()));
    }
    let p = cfg.resolve(Path::new(orig)).map_err(LoadErr::Sandbox)?;
    if !p.is_file() {
        return Err(LoadErr::Tool(err_obj(&format!("文件不存在: {}", orig))));
    }
    let real = p.to_string_lossy().into_owned();
    let lang = lang_of(&real);
    if lang.is_empty() {
        return Err(LoadErr::Tool(err_obj(&format!(
            "文件不可读或非代码文件: {}",
            orig
        ))));
    }
    // 读取失败按工具级错误如实上报（不静默降级）；与旧实现 OSError 包络的差异
    // 属 S86 口径的 OS 发散边缘
    let text = match read_text(&p) {
        Some(t) => t,
        None => {
            return Err(LoadErr::Tool(err_obj(&format!("读取失败: {}", orig))));
        }
    };
    Ok((real, lang, split_py_lines(&text)))
}

fn param_count(header: &str) -> i128 {
    if header.contains('(') && header.contains(')') {
        header.matches(',').count() as i128 + 1
    } else {
        0
    }
}

// ---------- 两个工具入口 ----------

pub fn outline(cfg: &SandboxCfg, orig: &str) -> Result<Value, String> {
    let (real, lang, lines) = match load(cfg, orig) {
        Ok(v) => v,
        Err(LoadErr::Sandbox(e)) => return Err(e),
        Err(LoadErr::Tool(v)) => return Ok(v),
    };
    let mut symbols = Vec::new();
    for sp in symbol_spans(&lines, lang).iter().take(OUTLINE_CAP) {
        // outline 的 params 只认 fn（旧实现 p=="fn" 条件）
        let params = if sp.kind == "fn" {
            param_count(&lines[sp.start])
        } else {
            0
        };
        symbols.push(Value::Obj(vec![
            ("name".into(), Value::Str(sp.name.clone())),
            ("kind".into(), Value::Str(sp.kind.into())),
            ("line".into(), Value::Int(sp.start as i128 + 1)),
            ("end_line".into(), Value::Int(sp.end as i128)),
            ("params".into(), Value::Int(params)),
        ]));
    }
    Ok(Value::Obj(vec![
        ("file".into(), Value::Str(real)),
        ("lang".into(), Value::Str(lang.into())),
        ("total".into(), Value::Int(symbols.len() as i128)),
        ("symbols".into(), Value::Arr(symbols)),
    ]))
}

pub fn read_symbol(
    cfg: &SandboxCfg,
    orig: &str,
    name: &str,
    occurrence: i64,
) -> Result<Value, String> {
    let (real, lang, lines) = match load(cfg, orig) {
        Ok(v) => v,
        Err(LoadErr::Sandbox(e)) => return Err(e),
        Err(LoadErr::Tool(v)) => return Ok(v),
    };
    let all = symbol_spans(&lines, lang);
    let spans: Vec<&Span> = all.iter().filter(|s| s.name == name).collect();
    if spans.is_empty() {
        return Ok(err_obj(&format!(
            "符号 {} 不存在——用 ide_outline 查清单",
            name
        )));
    }
    if occurrence < 1 || occurrence as usize > spans.len() {
        return Ok(err_obj(&format!(
            "occurrence={} 越界（{} 共 {} 处）",
            occurrence,
            name,
            spans.len()
        )));
    }
    let sp = spans[(occurrence - 1) as usize];
    // read_symbol 的 params 不看 kind，只看括号（旧实现另一条路）
    let params = param_count(&lines[sp.start]);
    let content = lines[sp.start..sp.end].join("\n");
    Ok(Value::Obj(vec![
        ("file".into(), Value::Str(real)),
        ("lang".into(), Value::Str(lang.into())),
        ("name".into(), Value::Str(name.into())),
        ("kind".into(), Value::Str(sp.kind.into())),
        ("start".into(), Value::Int(sp.start as i128 + 1)),
        ("end".into(), Value::Int(sp.end as i128)),
        ("lines".into(), Value::Int((sp.end - sp.start) as i128)),
        ("params".into(), Value::Int(params)),
        ("content".into(), Value::Str(content)),
    ]))
}

// ---------- S93：ide_edit 三件原生化（locate_edit / code_context / ide_rename）----------
//
// 语义逐条对齐 tools/ide_edit.py + tools/ide_common.py：
// - _iter_files：os.walk 两段式（先本层文件后子目录）、无显式排序（scandir
//   枚举序即 NTFS B-tree 序）、13 个跳过目录、max_files 只计代码文件；
// - os.walk 3.14 junction 语义：junction islink()=False → 照常下钻（Rust 侧
//   用 reparse tag 判别，见 appclone::is_junction）；真目录符号链接
//   followlinks=False → 剪除不下钻；悬空联接 read_dir 失败静默剪；
// - _read：resolve → isfile → utf-8 errors=replace（from_utf8_lossy 的
//   受限续字节 DFA 与 CPython 逐字节同判，S93 oracle badutf8b/c 钉死）；
// - locate_edit：query.strip()（Python str.strip 等价，含 \x1c-\x1f）、
//   命中 = 精确 ∨ 忽略大小写（A∨(B∧¬A) ⟺ A∨B）、snippet 窗口
//   [idx-2, idx+3)（1 前导 + 当前行 + 2 后随）、limit*3 双层停机、
//   references_in_scan 对已读全量源码做区分大小写计数（含触发停机的文件）；
// - code_context：getsize 门在 resolve 之前（裸路径，OSError 放行）——
//   沙盒外文件报"文件不可读"而非越界（旧实现的漏斗，原样保留）；
//   split("\n") RAW 不剥 \r；radius = max(5, min(x or 30, 200))；
//   cursor 0 → 头窗 min(80, 行数)；end 可为负 → Python 负切片语义；
// - ide_rename：固定 200 上限、空符号匹配一切文件（count("")=len+1 怪癖
//   原样保留）、plan=null（include_plan=false）。

const IDE_SKIP_DIRS: [&str; 13] = [
    ".git", "node_modules", "target", "__pycache__", "dist", "build",
    ".unified-rx-index", ".codegraph", "backups", "assets", "data", "models",
    "docs",
];

/// tools/ide_common.py::_MAX_EDIT_BYTES
const MAX_EDIT_BYTES: u64 = 10 * 1024 * 1024;

/// tools/ide_common.py::_lang_of：10 扩展名判型表（与 scan.rs 的 21 表不同源，
/// 缺省 "text"——text 不进遍历、不占 max_files 额度）。
pub(crate) fn ide_lang_of(path: &str) -> &'static str {
    let ext = crate::scan::splitext(path).to_lowercase();
    let ext = ext.strip_prefix('.').unwrap_or(&ext);
    match ext {
        "py" => "python",
        "rs" => "rust",
        "go" => "go",
        "ts" | "tsx" => "typescript",
        "js" | "jsx" => "javascript",
        "gd" => "gdscript",
        "cs" => "csharp",
        "dart" => "dart",
        _ => "text",
    }
}

/// Python str.strip() 等价：is_whitespace 之外还要剥 \x1c-\x1f（文件分隔符）。
fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// Python 单侧切片下标钳制：负数加 len，越界收口。
fn py_slice_i(v: i64, len: i64) -> usize {
    let mut i = v;
    if i < 0 {
        i += len;
        if i < 0 {
            i = 0;
        }
    }
    if i > len {
        i = len;
    }
    i.max(0) as usize
}

/// tools/ide_common.py::_read：resolve → isfile → 全文解码（失败 None）。
fn ide_read(cfg: &SandboxCfg, path: &str) -> Option<String> {
    if path.is_empty() {
        return None; // _resolve("path 必填") → ValueError → None
    }
    let p = cfg.resolve(Path::new(path)).ok()?;
    if !p.is_file() {
        return None;
    }
    crate::scan::read_text(&p)
}

struct IdeWalk {
    out: Vec<String>,
    count: usize,
    max: i64,
}

fn ide_push_file(dir: &Path, name: &str, st: &mut IdeWalk) {
    let fp = crate::scan::join_name(dir, name);
    if ide_lang_of(&fp) == "text" {
        return; // 非代码：不占额度
    }
    if st.count as i64 >= st.max {
        return;
    }
    st.count += 1;
    st.out.push(fp);
}

/// os.walk 两段式等价：本层文件先尽、子目录后钻；枚举序即产出序（无排序）。
fn ide_walk(dir: &Path, st: &mut IdeWalk) {
    let rd = match std::fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => return, // 悬空 junction / 不可读目录：静默剪
    };
    let mut files: Vec<String> = Vec::new();
    // (路径, 名字, 是否下钻)：junction 下钻；真目录符号链接不钻
    let mut dirs: Vec<(std::path::PathBuf, String, bool)> = Vec::new();
    for e in rd.filter_map(|e| e.ok()) {
        let name = e.file_name().to_string_lossy().into_owned();
        let p = e.path();
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_dir() {
            dirs.push((p, name, true));
        } else if ft.is_symlink() {
            if crate::appclone::is_junction(&p) {
                dirs.push((p, name, true));
            } else {
                match std::fs::metadata(&p) {
                    Ok(m) if m.is_dir() => dirs.push((p, name, false)),
                    _ => files.push(name), // 文件符号链接 / 悬空链接 → 文件侧
                }
            }
        } else {
            files.push(name);
        }
    }
    for name in &files {
        ide_push_file(dir, name, st);
    }
    for (p, name, descend) in &dirs {
        if IDE_SKIP_DIRS.contains(&name.as_str()) {
            continue;
        }
        if st.count as i64 >= st.max {
            return;
        }
        if *descend {
            ide_walk(p, st);
        }
    }
}

/// _iter_files 等价（调用方已 isdir 过，root 恒为目录）。
pub(crate) fn iter_files_ide(root: &Path, max_files: i64) -> Vec<String> {
    let mut st = IdeWalk { out: Vec::new(), count: 0, max: max_files };
    ide_walk(root, &mut st);
    st.out
}

/// locate_edit 原生化。沙盒/解析错误按旧实现回落为工具级
/// {"error": ...}（exit 0），不走 S92 的 exit-2 ValueError 路。
pub fn locate_edit(
    cfg: &SandboxCfg,
    path_arg: &str,
    query: &str,
    max_files: i64,
    limit: i64,
) -> Result<Value, String> {
    if path_arg.is_empty() {
        return Ok(err_obj("path 必填"));
    }
    let root = match cfg.resolve(Path::new(path_arg)) {
        Ok(p) => p,
        Err(e) => return Ok(err_obj(&e)),
    };
    if !root.is_dir() {
        return Ok(err_obj(&format!("不是目录: {}", root.display())));
    }
    let q = py_strip(query);
    if q.is_empty() {
        return Ok(err_obj("query 为空——请提供符号或关键词"));
    }
    let stop = limit.saturating_mul(3);
    let mut hits: Vec<Value> = Vec::new();
    let mut all_sources: Vec<(String, String)> = Vec::new();
    for fp in iter_files_ide(&root, max_files) {
        let Some(src) = ide_read(cfg, &fp) else { continue };
        all_sources.push((fp.clone(), src.clone()));
        let lines: Vec<&str> = src.split('\n').collect();
        let ql = q.to_lowercase();
        for (i, line) in lines.iter().enumerate() {
            // 旧实现：query in line or (query.lower() in line.lower()
            //                        and query not in line) —— 化简为 A ∨ B
            if line.contains(q) || line.to_lowercase().contains(&ql) {
                let lo = i.saturating_sub(1);
                let hi = (i + 4).min(lines.len());
                hits.push(Value::Obj(vec![
                    ("file".into(), Value::Str(fp.clone())),
                    ("line".into(), Value::Int(i as i128 + 1)),
                    ("snippet".into(), Value::Str(lines[lo..hi].join("\n"))),
                ]));
                if hits.len() as i64 >= stop {
                    break;
                }
            }
        }
        if hits.len() as i64 >= stop {
            break;
        }
    }
    // 影响面事实：区分大小写全量计数（含触发停机的文件、不含未读文件）
    let ref_count: i128 = all_sources
        .iter()
        .map(|(_, s)| s.matches(q).count() as i128)
        .sum();
    let take = py_slice_i(limit, hits.len() as i64);
    Ok(Value::Obj(vec![
        ("query".into(), Value::Str(q.into())),
        ("total".into(), Value::Int(hits.len() as i128)),
        ("references_in_scan".into(), Value::Int(ref_count)),
        ("hits".into(), Value::Arr(hits.into_iter().take(take).collect())),
    ]))
}

/// code_context 原生化。getsize 门在沙盒 resolve 之前（裸路径裸调用，
/// OSError 放行）——沙盒外文件走到 _read 才失败，报"文件不可读"。
pub fn code_context(
    cfg: &SandboxCfg,
    path: &str,
    cursor_line: i64,
    radius: i64,
) -> Result<Value, String> {
    if let Ok(m) = std::fs::metadata(path) {
        if m.len() > MAX_EDIT_BYTES {
            return Ok(err_obj("文件超过 10MB——拒绝读取"));
        }
    }
    let Some(src) = ide_read(cfg, path) else {
        return Ok(err_obj(&format!("文件不可读: {path}")));
    };
    let lines: Vec<&str> = src.split('\n').collect();
    let total = lines.len() as i64;
    let r = if radius == 0 { 30 } else { radius };
    let r = r.max(5).min(200);
    let (start, end) = if cursor_line == 0 {
        (0, total.min(80))
    } else {
        ((cursor_line - 1 - r).max(0), (cursor_line - 1 + r).min(total))
    };
    let s = py_slice_i(start, total);
    let e = py_slice_i(end, total);
    Ok(Value::Obj(vec![
        ("file".into(), Value::Str(path.into())),
        ("lang".into(), Value::Str(ide_lang_of(path).into())),
        ("total_lines".into(), Value::Int(total as i128)),
        ("start".into(), Value::Int(start as i128 + 1)),
        ("end".into(), Value::Int(end as i128)),
        ("content".into(), Value::Str(lines[s..e].join("\n"))),
    ]))
}

/// ide_rename 原生化。L3 只建议不落盘；空符号匹配一切文件（count("")=len+1）
/// 是旧实现的怪癖，原样保留。
pub fn ide_rename(
    cfg: &SandboxCfg,
    root_arg: &str,
    symbol: &str,
    new_name: &str,
    include_plan: bool,
) -> Result<Value, String> {
    if root_arg.is_empty() {
        return Ok(err_obj("path 必填"));
    }
    let root = match cfg.resolve(Path::new(root_arg)) {
        Ok(p) => p,
        Err(e) => return Ok(err_obj(&e)),
    };
    if !root.is_dir() {
        return Ok(err_obj(&format!("不是目录: {}", root.display())));
    }
    let mut plan: Vec<(String, i128)> = Vec::new();
    for fp in iter_files_ide(&root, 200) {
        let Some(src) = ide_read(cfg, &fp) else { continue };
        if src.contains(symbol) {
            plan.push((fp, src.matches(symbol).count() as i128));
        }
    }
    let total_occ: i128 = plan.iter().map(|(_, n)| *n).sum();
    Ok(Value::Obj(vec![
        ("symbol".into(), Value::Str(symbol.into())),
        ("new_name".into(), Value::Str(new_name.into())),
        ("files_affected".into(), Value::Int(plan.len() as i128)),
        ("total_occurrences".into(), Value::Int(total_occ)),
        (
            "plan".into(),
            if include_plan {
                Value::Arr(
                    plan.into_iter()
                        .map(|(f, n)| {
                            Value::Obj(vec![
                                ("file".into(), Value::Str(f)),
                                ("occurrences".into(), Value::Int(n)),
                            ])
                        })
                        .collect(),
                )
            } else {
                Value::Null
            },
        ),
        (
            "note".into(),
            Value::Str("L3 只建议不落盘；确认后可用 fs_write 应用".into()),
        ),
    ]))
}
