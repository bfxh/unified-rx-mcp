//! secrets —— 凭据/密钥泄漏扫描原生化（S134，CONSOLIDATION/HARDENING §六 候选一）。
//!
//! 语义与 tools/secrets.py（S123）**逐字节对齐**（对照实验为删码依据）：
//! 8 条模式规则 + 熵层（Shannon ≥ 阈值），输出一律**掩码**（前 4 后 2 + 长度），
//! 完整值绝不进结果。零第三方 crate：全部匹配器手写（含 Python 正则的
//! **贪婪 + 回溯**语义），词边界按 Unicode `\w` 口径（`char::is_alphanumeric`
//! ∨ '_'；中文相邻即无边界——与 Python str 正则一致）。
//!
//! 保真要点（对照实验关注项）：
//! - 掩码 `_mask`：<8 全遮 "***"；否则 `前4…后2(len=N)`（U+2026 省略号）；
//! - 占位符过滤（27 词，小写包含即滤，仅作用 secret_assignment）；
//! - 锁文件/构建产物的**熵层**整文件跳过（模式层照扫）；
//! - 二进制（前 8192 字节含 NUL）与超尺寸文件计 files_skipped；
//! - 行切分按 Python `str.splitlines()` 全字符集（\n\r\v\f\x1c-\x1e\x85\u2028\u2029）；
//! - Shannon 按**首现序**累加（与 Python dict 插入序一致 → f64 逐位一致）；
//! - hits 按 (severity, file, line) 稳定排序（同级同文件同行保持规则序）；
//! - 熵层 token 类 `[A-Za-z0-9_/+=-]{20,300}` 按 300 截断分块（finditer 非重叠语义）。
//!
//! 已知边界（如实）：非 UTF-8 文件的解码按 lossy（snippet 文本可能在替换字符
//! 边界上与 Python 略有差异；**命中/掩码不受影响**——模式与 token 均为 ASCII 类）；
//! 文件遍历序与 Python os.walk 不同（仅影响 max_files 截断边界的取件集合，
//! 结果排序后一致）。

use std::fs;
use std::path::{Path, PathBuf};

use crate::json::{self, Value};

const MAX_LINE_SNIPPET: usize = 200;
const ENTROPY_CHUNK: usize = 300;

pub const DEFAULT_INCLUDE: &str = "py,rs,js,ts,jsx,tsx,json,yaml,yml,toml,md,go,java,c,h,cpp,cs,php,rb,sh,ps1,bat,sql,env,cfg,ini,txt,xml,html,swift,kt";

/// ide_common._SKIP_DIRS ∪ {venv,.venv,site-packages,.tox,dist,out,.idea,.vscode}（逐字）
const SKIP_DIRS: [&str; 20] = [
    ".git", "node_modules", "target", "__pycache__", "dist", "build",
    ".unified-rx-index", ".codegraph", "backups", "assets", "data", "models",
    "docs", "venv", ".venv", "site-packages", ".tox", "out", ".idea", ".vscode",
];

/// 占位符过滤（27 词，逐字；小写包含即滤，仅 secret_assignment 层）
const PLACEHOLDERS: [&str; 27] = [
    "changeme", "change-me", "example", "placeholder", "your-", "your_",
    "<your", "${", "$(", "{%", "{{", "xxx", "todo", "dummy", "n/a", "insert",
    "replace", "sample", "dummy_", "123456", "qwerty", "letmein", "password",
    "username", "test123", "abcd", "default",
];

// ---------------------------------------------------------------- 字符谓词

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

fn start_boundary(ch: &[char], i: usize) -> bool {
    i == 0 || !is_word(ch[i - 1])
}

/// `\b` at position e（e 可为串尾：要求前一字符是 word）。
fn end_boundary(ch: &[char], e: usize) -> bool {
    if e >= ch.len() {
        e > 0 && is_word(ch[e - 1])
    } else {
        is_word(ch[e - 1]) != is_word(ch[e])
    }
}

/// 贪婪 {min,max} + 词边界回溯：返回最长满足边界的末端（None=无匹配）。
fn greedy_boundary_end(
    ch: &[char], start: usize, min: usize, max: usize, class: impl Fn(char) -> bool,
) -> Option<usize> {
    let run_end = (start..ch.len()).take_while(|&i| class(ch[i])).count() + start;
    let hi = run_end.min(start + max);
    let mut e = hi;
    while e >= start + min && e > start {
        if end_boundary(ch, e) {
            return Some(e);
        }
        e -= 1;
    }
    None
}

fn chars_of(s: &str) -> Vec<char> {
    s.chars().collect()
}

// ---------------------------------------------------------------- 8 条模式规则

fn find_aws(ch: &[char]) -> Vec<(usize, usize)> {
    let mut out = vec![];
    let mut i = 0;
    while i + 20 <= ch.len() {
        if start_boundary(ch, i)
            && ch[i] == 'A' && ch[i + 1] == 'K' && ch[i + 2] == 'I' && ch[i + 3] == 'A'
            && (4..20).all(|k| ch[i + k].is_ascii_digit() || ch[i + k].is_ascii_uppercase())
        {
            let e = i + 20;
            if end_boundary(ch, e) {
                out.push((i, e));
                i = e;
                continue;
            }
        }
        i += 1;
    }
    out
}

fn find_github(ch: &[char]) -> Vec<(usize, usize)> {
    let mut out = vec![];
    let mut i = 0;
    while i + 4 < ch.len() {
        if start_boundary(ch, i) && ch[i] == 'g' && ch[i + 1] == 'h'
            && matches!(ch[i + 2], 'p' | 'o' | 'u' | 's' | 'r') && ch[i + 3] == '_'
            && let Some(e) = greedy_boundary_end(ch, i + 4, 36, 250, |c| c.is_ascii_alphanumeric()) {
                out.push((i, e));
                i = e;
                continue;
            }
        i += 1;
    }
    out
}

fn find_slack(ch: &[char]) -> Vec<(usize, usize)> {
    let mut out = vec![];
    let mut i = 0;
    while i + 5 < ch.len() {
        if start_boundary(ch, i) && ch[i] == 'x' && ch[i + 1] == 'o' && ch[i + 2] == 'x'
            && matches!(ch[i + 3], 'b' | 'a' | 'p' | 'r' | 's') && ch[i + 4] == '-'
            && let Some(e) = greedy_boundary_end(ch, i + 5, 10, 250, |c| {
                c.is_ascii_alphanumeric() || c == '-'
            }) {
                out.push((i, e));
                i = e;
                continue;
            }
        i += 1;
    }
    out
}

fn find_google(ch: &[char]) -> Vec<(usize, usize)> {
    let mut out = vec![];
    let mut i = 0;
    while i + 39 <= ch.len() {
        if start_boundary(ch, i)
            && ch[i] == 'A' && ch[i + 1] == 'I' && ch[i + 2] == 'z' && ch[i + 3] == 'a'
            && (4..39).all(|k| {
                ch[i + k].is_ascii_alphanumeric() || ch[i + k] == '_' || ch[i + k] == '-'
            })
        {
            let e = i + 39; // {35} 定量：无回溯
            if end_boundary(ch, e) {
                out.push((i, e));
                i = e;
                continue;
            }
        }
        i += 1;
    }
    out
}

fn find_stripe(ch: &[char]) -> Vec<(usize, usize)> {
    const P: &[char] = &['s', 'k', '_', 'l', 'i', 'v', 'e', '_'];
    let mut out = vec![];
    let mut i = 0;
    while i + 8 < ch.len() {
        if start_boundary(ch, i) && ch[i..(i + 8).min(ch.len())] == *P
            && let Some(e) = greedy_boundary_end(ch, i + 8, 24, 250, |c| c.is_ascii_alphanumeric()) {
                out.push((i, e));
                i = e;
                continue;
            }
        i += 1;
    }
    out
}

fn find_pem(ch: &[char]) -> Vec<(usize, usize)> {
    const HEAD: &str = "-----BEGIN ";
    const TAIL: &str = "PRIVATE KEY-----";
    let head: Vec<char> = HEAD.chars().collect();
    let tail: Vec<char> = TAIL.chars().collect();
    let mut out = vec![];
    let mut i = 0;
    while i + head.len() <= ch.len() {
        if ch[i..i + head.len()] == head[..] {
            // [A-Z ]* 贪婪 + 回溯尝试字面尾
            let run_end = (i + head.len()..ch.len())
                .take_while(|&k| ch[k].is_ascii_uppercase() || ch[k] == ' ')
                .count() + i + head.len();
            let mut e = run_end;
            loop {
                if e + tail.len() <= ch.len() && ch[e..e + tail.len()] == tail[..] {
                    out.push((i, e + tail.len()));
                    i = e + tail.len();
                    break;
                }
                if e <= i + head.len() {
                    i += 1;
                    break;
                }
                e -= 1;
            }
        } else {
            i += 1;
        }
    }
    out
}

const JWT_CLASS: fn(char) -> bool =
    |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '-';

fn seg_until_dot(ch: &[char], start: usize, min: usize, max: usize) -> Option<usize> {
    let run_end = (start..ch.len()).take_while(|&i| JWT_CLASS(ch[i])).count() + start;
    let n = run_end - start;
    if n >= min && n <= max && run_end < ch.len() && ch[run_end] == '.' {
        Some(run_end)
    } else {
        None
    }
}

fn find_jwt(ch: &[char]) -> Vec<(usize, usize)> {
    let mut out = vec![];
    let mut i = 0;
    while i + 3 <= ch.len() {
        if start_boundary(ch, i) && ch[i] == 'e' && ch[i + 1] == 'y' && ch[i + 2] == 'J'
            && let Some(e1) = seg_until_dot(ch, i + 3, 10, 250)
                && let Some(e2) = seg_until_dot(ch, e1 + 1, 10, 250)
                    && let Some(e3) = greedy_boundary_end(ch, e2 + 1, 5, 250, |c| {
                        JWT_CLASS(c)
                    }) {
                        out.push((i, e3));
                        i = e3;
                        continue;
                    }
        i += 1;
    }
    out
}

/// secret_assignment：(?i)\b(kw)\b\s*[:=]\s*["']?([^\s"']{12,200})
/// 交替按源序（最左最先、逐个整体试）：
/// password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|
/// auth[_-]?token|client[_-]?secret|secret[_-]?key
/// （S134 性能注记：kw 匹配走无分配的字面比较；首字符过滤把 97% 位置 O(1) 掉——
/// 首版按"每位置每形态 format! 建字符串"，整仓 16.4s vs Python 1.94s，重构后见
/// ROUNDLOG 实测。）
const ALT: [(&str, &str); 11] = [
    ("password", ""), ("passwd", ""), ("pwd", ""), ("secret", ""), ("token", ""),
    ("api", "key"), ("access", "key"), ("private", "key"), ("auth", "token"),
    ("client", "secret"), ("secret", "key"),
];

/// 在 i 处匹配字面 words（ASCII 大小写不敏感，逐字符）；命中返回末端。
#[inline]
fn match_lit(ch: &[char], i: usize, w: &str) -> Option<usize> {
    let n = w.len(); // 全 ASCII 字面
    if i + n > ch.len() {
        return None;
    }
    for (k, b) in w.bytes().enumerate() {
        if ch[i + k].to_ascii_lowercase() as u8 != b {
            return None;
        }
    }
    Some(i + n)
}

fn find_secret_assignment(ch: &[char]) -> Vec<(usize, usize, usize)> {
    // 返回 (kw_start, value_start, value_end)。value 为掩码/替换区间（group(2) 语义）。
    let mut out = vec![];
    let mut i = 0;
    while i < ch.len() {
        // 首字符过滤：kw 首字母集合 {p,a,s,t,c}
        let c0 = ch[i].to_ascii_lowercase();
        if !matches!(c0, 'p' | 'a' | 's' | 't' | 'c') {
            i += 1;
            continue;
        }
        let mut matched_end: Option<usize> = None;
        for (a, b) in ALT {
            // 三形态：[_-]? 的"优先吃分隔符"贪婪序（有分隔符先试带分隔符的）
            let mut cands: [Option<usize>; 3] = [None, None, None];
            let mut n = 0;
            if !b.is_empty() {
                let ae = match match_lit(ch, i, a) {
                    Some(e) => e,
                    None => continue,
                };
                if ae < ch.len() && matches!(ch[ae], '_' | '-') {
                    let sep = ch[ae];
                    let full_sep: String = format!("{}{}", a, sep);
                    if let Some(e) = match_lit(ch, i, &full_sep)
                        && let Some(be) = match_lit(ch, e, b) {
                            cands[n] = Some(be);
                            n += 1;
                        }
                }
                let full: String = format!("{}{}", a, b);
                if let Some(be) = match_lit(ch, i, &full) {
                    cands[n] = Some(be);
                    n += 1;
                }
            } else if let Some(e) = match_lit(ch, i, a) {
                cands[0] = Some(e);
                n = 1;
            }
            for cand in cands.into_iter().take(n).flatten() {
                let kw_end = cand;
                if !end_boundary(ch, kw_end) {
                    continue;
                }
                let mut j = kw_end;
                while j < ch.len() && ch[j].is_whitespace() {
                    j += 1;
                }
                if j >= ch.len() || (ch[j] != ':' && ch[j] != '=') {
                    continue;
                }
                j += 1;
                while j < ch.len() && ch[j].is_whitespace() {
                    j += 1;
                }
                if j < ch.len() && (ch[j] == '"' || ch[j] == '\'') {
                    j += 1;
                }
                let vstart = j;
                let run_end = (vstart..ch.len())
                    .take_while(|&k| {
                        !ch[k].is_whitespace() && ch[k] != '"' && ch[k] != '\''
                    })
                    .count() + vstart;
                let vcap = run_end.min(vstart + 200);
                if vcap - vstart >= 12 {
                    out.push((i, vstart, vcap));
                    matched_end = Some(vcap);
                    break;
                }
            }
            if matched_end.is_some() {
                break;
            }
        }
        i = matched_end.unwrap_or(i + 1);
    }
    out
}

// ---------------------------------------------------------------- 熵层

fn shannon(s: &[char]) -> f64 {
    if s.is_empty() {
        return 0.0;
    }
    // 首现序计数（与 Python dict 插入序一致 → f64 累加序一致）；
    // token 类保证 ASCII：字节直索引 O(n)（首版线性查找 O(k²) 是整仓热点之一）
    let mut counts = [0usize; 256];
    let mut order: Vec<u8> = Vec::with_capacity(16);
    for &c in s {
        let b = c as u8;
        if counts[b as usize] == 0 {
            order.push(b);
        }
        counts[b as usize] += 1;
    }
    let n = s.len() as f64;
    let mut acc = 0.0;
    for &b in &order {
        let p = counts[b as usize] as f64 / n;
        acc += p * p.log2();
    }
    -acc
}

fn char_classes(tok: &[char]) -> usize {
    let mut lower = false;
    let mut upper = false;
    let mut digit = false;
    let mut sym = false;
    for &c in tok {
        if c.is_ascii_lowercase() {
            lower = true;
        } else if c.is_ascii_uppercase() {
            upper = true;
        } else if c.is_ascii_digit() {
            digit = true;
        } else if matches!(c, '_' | '/' | '+' | '=' | '-') {
            sym = true;
        }
    }
    lower as usize + upper as usize + digit as usize + sym as usize
}

fn entropy_token_class(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '_' | '/' | '+' | '=' | '-')
}

/// 行长上的 token 分块（finditer 非重叠、{20,300} 截断分块语义）
fn entropy_tokens(ch: &[char]) -> Vec<(usize, usize)> {
    let mut out = vec![];
    let mut i = 0;
    while i < ch.len() {
        if !entropy_token_class(ch[i]) {
            i += 1;
            continue;
        }
        let run_end = (i..ch.len()).take_while(|&k| entropy_token_class(ch[k])).count() + i;
        let mut s = i;
        while s < run_end {
            let e = (s + ENTROPY_CHUNK).min(run_end);
            if e - s >= 20 {
                out.push((s, e));
            }
            s = e;
        }
        i = run_end;
    }
    out
}

// ---------------------------------------------------------------- 掩码/占位符/工具

fn mask(tok: &[char]) -> String {
    if tok.len() < 8 {
        return "***".to_string();
    }
    let head: String = tok[..4].iter().collect();
    let tail: String = tok[tok.len() - 2..].iter().collect();
    format!("{}…{}(len={})", head, tail, tok.len())
}

fn is_placeholder(value: &[char]) -> bool {
    let low: String = value.iter().flat_map(|c| c.to_lowercase()).collect();
    PLACEHOLDERS.iter().any(|p| low.contains(p))
}

fn ext_of(fname: &str) -> String {
    if fname == ".env" {
        return "env".to_string();
    }
    match fname.rsplit_once('.') {
        Some((_, e)) => e.to_lowercase(),
        None => String::new(),
    }
}

fn entropy_skip_name(fname: &str) -> bool {
    let base = fname.to_lowercase();
    if base.ends_with(".min.js") || base.ends_with(".sum") || base.ends_with(".lock") {
        return true;
    }
    // (^|[._-])lock：单词 lock 前是串首或 . _ -
    let b: Vec<char> = base.chars().collect();
    let mut i = 0;
    while i + 4 <= b.len() {
        if b[i] == 'l' && b[i + 1] == 'o' && b[i + 2] == 'c' && b[i + 3] == 'k'
            && (i == 0 || matches!(b[i - 1], '.' | '_' | '-')) {
                return true;
            }
        i += 1;
    }
    false
}

/// Python str.splitlines 的切分集合（\n \r \r\n \v \f \x1c \x1d \x1e \x85 \u2028 \u2029）
fn splitlines(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let bytes = text.as_bytes();
    let mut start = 0usize;
    let mut i = 0usize;
    while i < bytes.len() {
        // 多字节分隔符先看 ASCII 单字节集
        let b = bytes[i];
        let sep_len = if b == b'\n' || b == b'\r' || b == 0x0b || b == 0x0c
            || b == 0x1c || b == 0x1d || b == 0x1e
        {
            if b == b'\r' && i + 1 < bytes.len() && bytes[i + 1] == b'\n' { 2 } else { 1 }
        } else if b == 0xc2 && i + 1 < bytes.len() && bytes[i + 1] == 0x85 {
            2 // U+0085 NEL
        } else if b == 0xe2 && i + 2 < bytes.len() && bytes[i + 1] == 0x80
            && (bytes[i + 2] == 0xa8 || bytes[i + 2] == 0xa9)
        {
            3 // U+2028 / U+2029
        } else {
            0
        };
        if sep_len > 0 {
            out.push(&text[start..i]);
            i += sep_len;
            start = i;
        } else {
            i += 1;
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    } else if text.is_empty() {
        return Vec::new();
    }
    out
}

fn walk_ext_files(root: &Path, exts: &[String], max_files: usize) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        let rd = match fs::read_dir(&d) {
            Ok(r) => r,
            Err(_) => continue,
        };
        let mut subdirs = vec![];
        for e in rd.flatten() {
            let p = e.path();
            let name = e.file_name().to_string_lossy().to_string();
            if p.is_dir() {
                if !SKIP_DIRS.contains(&name.as_str()) {
                    subdirs.push(p);
                }
            } else if exts.iter().any(|x| *x == ext_of(&name)) {
                if out.len() >= max_files {
                    return out;
                }
                out.push(p);
            }
        }
        subdirs.sort();          // 确定性遍历序（同目录内按名）+ LIFO 入栈
        for d in subdirs.into_iter().rev() {
            stack.push(d);
        }
    }
    out
}

// ---------------------------------------------------------------- 主入口

/// 扫描目录 → 结果 json（键序与 Python 版一致；root 由薄壳补）。
#[allow(clippy::too_many_arguments)]
pub fn secrets_scan(
    root: &Path, max_files: usize, max_file_kb: usize, min_entropy: f64,
    max_results: usize, include: &str,
) -> Value {
    if !root.is_dir() {
        return json::Value::Obj(vec![(
            "error".into(),
            Value::Str(format!("目录不存在: {}", root.display())),
        )]);
    }
    let exts: Vec<String> = if include.trim().is_empty() {
        DEFAULT_INCLUDE.split(',').map(|s| s.trim().to_lowercase()).collect()
    } else {
        include
            .split(',')
            .map(|s| s.trim().trim_start_matches('.').to_lowercase())
            .filter(|s| !s.is_empty())
            .collect()
    };
    let size_cap = max_file_kb * 1024;

    let mut files = walk_ext_files(root, &exts, max_files);
    files.sort(); // 结果与遍历序解耦（命中排序后等价；截断边界的取件集合见模块注记）

    let mut hits: Vec<(u8, String, usize, String, String, String, String)> = vec![];
    // 元组：(sev_rank, file, line, rule, severity, masked, snippet)
    let mut files_scanned = 0usize;
    let mut files_skipped = 0usize;

    for fp in &files {
        let base = fp.file_name().map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default();
        let bytes = match fs::read(fp) {
            Ok(b) => b,
            Err(_) => {
                files_skipped += 1;
                continue;
            }
        };
        if bytes.len() > size_cap {
            files_skipped += 1;
            continue;
        }
        let head = &bytes[..bytes.len().min(8192)];
        if head.contains(&0u8) {
            files_skipped += 1;
            continue;
        }
        files_scanned += 1;
        let text = String::from_utf8_lossy(&bytes).to_string();
        let rel = fp
            .strip_prefix(root)
            .unwrap_or(fp)
            .to_string_lossy()
            .replace('\\', "/");
        let entropy_layer = !entropy_skip_name(&base);

        for (lineno, line) in splitlines(&text).into_iter().enumerate() {
            let ch = chars_of(line);
            let trimmed: String = line.trim().to_string();

            // 模式层：规则源序扁平收集（Python 版逐规则逐命中追加的等价序）
            let mut found: Vec<(usize, usize, String)> = vec![];
            {
                let mut push_rule = |fname: &str, spans: Vec<(usize, usize)>| {
                    for (s, e) in spans {
                        found.push((s, e, fname.to_string()));
                    }
                };
                push_rule("aws_access_key", find_aws(&ch));
                push_rule("github_token", find_github(&ch));
                push_rule("slack_token", find_slack(&ch));
                push_rule("google_api_key", find_google(&ch));
                push_rule("stripe_live_key", find_stripe(&ch));
                push_rule("private_key_block", find_pem(&ch));
                push_rule("jwt", find_jwt(&ch));
                for (_s, vs, ve) in find_secret_assignment(&ch) {
                    // 掩码/片段替换的是**值区间**（Python group(2) 语义），非整段
                    found.push((vs, ve, "secret_assignment".to_string()));
                }
            }
            for (s, e, rule) in found {
                let severity = match rule.as_str() {
                    "aws_access_key" | "github_token" | "slack_token"
                    | "google_api_key" | "stripe_live_key" => "high",
                    "private_key_block" => "critical",
                    _ => "medium",
                };
                let value: Vec<char> = ch[s..e].to_vec();
                if rule == "secret_assignment" && is_placeholder(&value) {
                    continue;
                }
                let m = mask(&value);
                let snippet = trimmed.replace(&value.iter().collect::<String>(), &m);
                let snippet: String = snippet.chars().take(MAX_LINE_SNIPPET).collect();
                let rank = match severity {
                    "critical" => 0u8, "high" => 1, "medium" => 2, _ => 3,
                };
                hits.push((rank, rel.clone(), lineno + 1, rule, severity.to_string(),
                           m, snippet));
            }

            // 熵层
            if entropy_layer {
                for (s, e) in entropy_tokens(&ch) {
                    let tok = &ch[s..e];
                    if char_classes(tok) >= 3 && shannon(tok) >= min_entropy {
                        let value: String = tok.iter().collect();
                        let m = mask(tok);
                        let snippet = trimmed.replace(&value, &m);
                        let snippet: String = snippet.chars().take(MAX_LINE_SNIPPET).collect();
                        hits.push((3u8, rel.clone(), lineno + 1, "high_entropy".to_string(),
                                   "suspect".to_string(), m, snippet));
                    }
                }
            }
        }
    }

    // 稳定排序 (severity_rank, file, line)
    hits.sort_by_key(|a| (a.0, a.1.clone(), a.2));

    let mut by_sev = [0usize; 4];
    for h in &hits {
        let idx = match h.4.as_str() {
            "critical" => 0, "high" => 1, "medium" => 2, _ => 3,
        };
        by_sev[idx] += 1;
    }
    let total_hits = hits.len();
    let truncated = total_hits > max_results;
    let hits_json: Vec<Value> = hits
        .into_iter()
        .take(max_results)
        .map(|(_, file, line, rule, sev, masked, snippet)| {
            Value::Obj(vec![
                ("file".into(), Value::Str(file)),
                ("line".into(), Value::Int(line as i128)),
                ("rule".into(), Value::Str(rule)),
                ("severity".into(), Value::Str(sev)),
                ("masked".into(), Value::Str(masked)),
                ("snippet".into(), Value::Str(snippet)),
            ])
        })
        .collect();

    json::Value::Obj(vec![
        ("files_scanned".into(), Value::Int(files_scanned as i128)),
        ("files_skipped".into(), Value::Int(files_skipped as i128)),
        ("total_hits".into(), Value::Int(total_hits as i128)),
        (
            "by_severity".into(),
            Value::Obj(vec![
                ("critical".into(), Value::Int(by_sev[0] as i128)),
                ("high".into(), Value::Int(by_sev[1] as i128)),
                ("medium".into(), Value::Int(by_sev[2] as i128)),
                ("suspect".into(), Value::Int(by_sev[3] as i128)),
            ]),
        ),
        ("hits".into(), Value::Arr(hits_json)),
        ("truncated".into(), Value::Bool(truncated)),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mask_and_shannon_contract() {
        assert_eq!(mask(&chars_of("short")), "***");
        assert_eq!(mask(&chars_of("Kx9mQ2vB7wZ3sP6dL1cH5jG0")), "Kx9m…G0(len=24)");
        assert_eq!(shannon(&chars_of("")), 0.0);
        assert_eq!(shannon(&chars_of("aaaa")), 0.0);
        assert_eq!(shannon(&chars_of("abcd")), 2.0);
    }

    #[test]
    fn unicode_boundary_like_python() {
        // 中文相邻 → \w 相邻 → 无边界 → 不命中（与 Python str 正则一致）
        let ch = chars_of("中AKIAIOSFODNN7EXAMPLE");
        assert!(find_aws(&ch).is_empty());
        let ch2 = chars_of("x AKIAIOSFODNN7EXAMPLE y");
        assert_eq!(find_aws(&ch2).len(), 1);
    }

    #[test]
    fn splitlines_python_semantics() {
        let parts = splitlines("a\r\nb\rc\u{85}d\u{2028}e\u{b}f");
        assert_eq!(parts, vec!["a", "b", "c", "d", "e", "f"]);
    }

    #[test]
    fn entropy_skip_names() {
        assert!(entropy_skip_name("Cargo.lock"));
        assert!(entropy_skip_name("package-lock.json"));
        assert!(entropy_skip_name("poetry.lock"));
        assert!(entropy_skip_name("app.min.js"));
        assert!(entropy_skip_name("go.sum"));
        assert!(!entropy_skip_name("blockfile.py"));
        assert!(!entropy_skip_name("creds.py"));
    }
}
