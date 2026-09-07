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
fn split_py_lines(text: &str) -> Vec<String> {
    let t: Cow<str> = if text.contains('\r') {
        Cow::Owned(text.replace("\r\n", "\n").replace('\r', "\n"))
    } else {
        Cow::Borrowed(text)
    };
    t.split('\n').map(|s| s.to_string()).collect()
}

struct Span {
    name: String,
    start: usize,
    end: usize,
    kind: &'static str,
}

fn symbol_spans(lines: &[String], lang: &str) -> Vec<Span> {
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
