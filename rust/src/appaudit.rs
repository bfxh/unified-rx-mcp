//! appaudit —— app_audit 原生实现（S85），与 tools/appaudit.py::app_audit 逐条对齐。
//!
//! 范围：仅纯读的 app_audit。app_clone/app_clean 是写面+授权门，按"纯读先迁"
//! 纪律留在 Python 侧（_strictly_under 因此在两语言各有实现，oracle 对照钉死）。
//!
//! 语义对齐要点（全部有 oracle 场景钉着）：
//! - 沙盒门 `_strictly_under`：宽限 realpath（复用 sandbox::lenient_realpath）+
//!   normcase（小写 + / → \）+ 严格子判定（等于根本身也拒）；
//!   根 = env `UNIFIED_RX_AUDIT_SANDBOX`（空串视为未设）或 %TEMP%\unified-rx-appaudit，
//!   先 create_dir_all 再解析（与 Python _sandbox_root 一致）；
//! - 遍历：os.walk 语义——每层先文件后子目录、目录内 NTFS upcase 排序
//!   （scan.rs 同款，S80 实锤）；无 node_modules 剪枝（appaudit 全走）；
//! - 行扫描：单文件头部 3MB 字节 → utf-8 lossy（与 errors="replace" 同为
//!   maximal-subpart 逐段 U+FFFD）→ Python str.splitlines 全集（\r\n/\r/\v/\f/
//!   \x1c-\x1e/\x85/\u2028/\u2029）→ 空行与剥离后 >800 字符的行整行跳过
//!   （URL 也不计）；
//! - 计数怪癖：surface 规则全库共享 ≤51 次上限（seen_js_labels，先判 ≤50 再自增，
//!   实际每标签最多 51 条）；findings 总量 400 封顶（emit 门槛，definite/clues
//!   计数来自封顶后的 findings）；hit_lines 不封顶；asar 复扫不受 51 上限、
//!   不计入 hit_lines、不做 URL 盘点，但受 400 封顶；
//! - 排序稳定性：findings (kind!=definite, label, file, line) 稳定排序、
//!   binaries 按大小降序稳定排序（同大保持遍历序）、url_host_top 计数降序稳定
//!   （Counter.most_common 的 heapq.nlargest 同为稳定语义）；
//! - asar：头发现（8MB 窗 ×3 轮、候选长度升序试、截断 JSON 快败）→ 三层
//!   pickle 前导 u32 组合出候选内容基址、用前 8 个带 integrity 的中小文本叶
//!   SHA256 自标定 → 提取（文本扩展名/单条 4MB/总量 48MB/600 条上限/路径含
//!   ".." 或绝对路径跳过/哈希不符跳过）。
//!   与 Python 的已声明差异：意外 io 错误的类名（Python PermissionError 等 →
//!   统一 OSError 前缀）——设计内失败（_AsarError 两类）文本逐字一致。

use crate::json::Value;
use crate::sandbox::lenient_realpath;
use crate::sha256;
use std::collections::{BTreeSet, HashMap};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

const MAX_FINDINGS: usize = 400;
const MAX_ASARS: usize = 6;
const MAX_ASAR_EXTRACT_BYTES: i128 = 48 * 1024 * 1024;
const MAX_ASAR_ENTRY_BYTES: i64 = 4 * 1024 * 1024;
const HEAD_BYTES: usize = 3 * 1024 * 1024;

const TEXT_EXTS: &[&str] = &[
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".html", ".htm", ".css", ".md",
    ".txt", ".yml", ".yaml", ".env", ".ini", ".cfg", ".toml", ".xml", ".ron",
    // 凭据/密钥载体也是文本——漏掉它们 = 秘密规则对真实文件全盲
    ".pem", ".key", ".crt", ".pub",
];
const BINARY_INVENTORY_EXTS: &[&str] = &[".exe", ".dll", ".node", ".asar"];
const AI_HOST_HINTS: &[&str] = &[
    "anthropic", "openai", "deepseek", "baichuan", "hunyuan",
    "moonshot", "zhipuai", "mistral", "dashscope", "volces",
];

// ---------- 沙盒门（app_clean 仍用 Python 版；两版由 oracle 钉死等价） ----------

fn sandbox_root() -> PathBuf {
    let base = match std::env::var("UNIFIED_RX_AUDIT_SANDBOX") {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => PathBuf::from(std::env::var("TEMP").unwrap_or_else(|_| r"C:\Temp".into()))
            .join("unified-rx-appaudit"),
    };
    let _ = std::fs::create_dir_all(&base);
    lenient_realpath(&base)
}

/// normcase 等价：小写 + / → \。严格子判定：拒绝空白/等于根本身/大小写欺骗。
fn strictly_under(p: &str, root: &Path) -> bool {
    if p.trim().is_empty() {
        return false;
    }
    let pc = lenient_realpath(Path::new(p)).to_string_lossy().to_lowercase().replace('/', "\\");
    let rc = root.to_string_lossy().to_lowercase().replace('/', "\\");
    let rc = rc.trim_end_matches('\\');
    pc != rc && pc.starts_with(&format!("{}\\", rc))
}

// ---------- 遍历（os.walk 语义，scan.rs 同款） ----------

struct WalkFile {
    /// 相对 root 的路径，分隔符已归一为 /
    rel: String,
    path: PathBuf,
}

fn walk_files(root: &Path, out: &mut Vec<WalkFile>) {
    walk_dir_rec(root, root, out);
}

fn walk_dir_rec(root: &Path, dir: &Path, out: &mut Vec<WalkFile>) {
    let rd = match std::fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => return,
    };
    let mut files: Vec<(String, PathBuf)> = Vec::new();
    let mut dirs: Vec<PathBuf> = Vec::new();
    for e in rd.filter_map(|e| e.ok()) {
        let name = e.file_name().to_string_lossy().into_owned();
        let Ok(ft) = e.file_type() else { continue };
        // os.walk 语义：符号链接解引用定类（文件符号链接照收，目录符号链接
        // 进 dirs 但不下钻），断链两边都不是 → 跳过
        let is_dir = if ft.is_symlink() {
            match std::fs::metadata(e.path()) {
                Ok(m) => m.is_dir(),
                Err(_) => continue,
            }
        } else {
            ft.is_dir()
        };
        if is_dir {
            dirs.push(e.path());
        } else {
            files.push((name, e.path()));
        }
    }
    let by_upcase = |a: &String, b: &String| {
        a.to_uppercase().cmp(&b.to_uppercase()).then_with(|| a.cmp(b))
    };
    files.sort_by(|a, b| by_upcase(&a.0, &b.0));
    dirs.sort_by(|a, b| {
        let an = a.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let bn = b.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        by_upcase(&an, &bn)
    });
    for (name, p) in files {
        let _ = name;
        let rel = rel_of(root, &p);
        out.push(WalkFile { rel, path: p });
    }
    for d in dirs {
        walk_dir_rec(root, &d, out);
    }
}

fn rel_of(root: &Path, p: &Path) -> String {
    p.strip_prefix(root)
        .unwrap_or(p)
        .to_string_lossy()
        .replace('\\', "/")
}

fn splitext_lower(name: &str) -> String {
    let base = name.rsplit(['/', '\\']).next().unwrap_or(name);
    match base.rfind('.') {
        Some(i) if i > 0 => base[i..].to_lowercase(),
        _ => String::new(),
    }
}

fn is_text_ext(ext: &str) -> bool {
    TEXT_EXTS.contains(&ext)
}

// ---------- Python splitlines 全集 ----------

fn py_splitlines(s: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut cur = String::new();
    let mut broke = false;
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        match c {
            '\n' => {
                lines.push(std::mem::take(&mut cur));
                broke = true;
            }
            '\r' => {
                lines.push(std::mem::take(&mut cur));
                broke = true;
                if it.peek() == Some(&'\n') {
                    it.next();
                }
            }
            '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}'
            | '\u{2028}' | '\u{2029}' => {
                lines.push(std::mem::take(&mut cur));
                broke = true;
            }
            _ => cur.push(c),
        }
    }
    if !cur.is_empty() || !broke {
        lines.push(cur);
    }
    lines
}

// ---------- 手写规则匹配器（零第三方 crate；each 返回匹配到的全文切片） ----------
//
// Python \w ≈ 字母数字下划线（unicode）；Rust char::is_alphanumeric 同级近似，
// 边界差异只在非 ASCII 标识符相邻处，oracle 场景已覆盖典型形态。

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

fn is_re_ws(c: char) -> bool {
    // Python re \s（unicode 模式）≈ White_Space 全集
    c.is_whitespace()
}

type Chars = Vec<char>;

fn to_chars(s: &str) -> Chars {
    s.chars().collect()
}

fn slice(c: &[char], a: usize, b: usize) -> String {
    c[a..b].iter().collect()
}

fn starts_with_at(c: &[char], lit: &str, at: usize) -> bool {
    let l: Vec<char> = lit.chars().collect();
    c.len() >= at + l.len() && c[at..at + l.len()] == l[..]
}

/// r"\beval\s*\(" —— \b 在可选 \s* 之前
fn m_eval_call(line: &Chars) -> Option<(usize, usize)> {
    let n = line.len();
    for i in 0..n {
        if i > 0 && is_word(line[i - 1]) {
            continue;
        }
        if !starts_with_at(line, "eval", i) {
            continue;
        }
        let mut j = i + 4;
        while j < n && is_re_ws(line[j]) {
            j += 1;
        }
        if j < n && line[j] == '(' {
            return Some((i, j + 1));
        }
    }
    None
}

/// r"new\s+Function\s*\(" —— new 前有 \b（词首），Function 大小写敏感
fn m_new_function(line: &Chars) -> Option<(usize, usize)> {
    let n = line.len();
    for i in 0..n {
        if i > 0 && is_word(line[i - 1]) {
            continue;
        }
        if !starts_with_at(line, "new", i) {
            continue;
        }
        let mut j = i + 3;
        let ws0 = j;
        while j < n && is_re_ws(line[j]) {
            j += 1;
        }
        if j == ws0 || !starts_with_at(line, "Function", j) {
            continue;
        }
        j += 8;
        while j < n && is_re_ws(line[j]) {
            j += 1;
        }
        if j < n && line[j] == '(' {
            return Some((i, j + 1));
        }
    }
    None
}

/// r"""require\s*\(\s*["'](child_process|node:child_process)["']\s*\)"""
/// 注意：Python 原式无 \b（require 前不加边界），照抄。
fn m_child_process(line: &Chars) -> Option<(usize, usize)> {
    let n = line.len();
    for i in 0..n {
        if !starts_with_at(line, "require", i) {
            continue;
        }
        let mut j = i + 7;
        while j < n && is_re_ws(line[j]) {
            j += 1;
        }
        if j >= n || line[j] != '(' {
            continue;
        }
        j += 1;
        while j < n && is_re_ws(line[j]) {
            j += 1;
        }
        if j >= n || (line[j] != '"' && line[j] != '\'') {
            continue;
        }
        let q = line[j];
        j += 1;
        let alt = if starts_with_at(line, "child_process", j) {
            "child_process"
        } else if starts_with_at(line, "node:child_process", j) {
            "node:child_process"
        } else {
            continue;
        };
        j += alt.chars().count();
        if j >= n || line[j] != q {
            continue;
        }
        j += 1;
        while j < n && is_re_ws(line[j]) {
            j += 1;
        }
        if j < n && line[j] == ')' {
            return Some((i, j + 1));
        }
    }
    None
}

/// r"openExternal\s*\(" —— 无 \b，照抄
fn m_open_external(line: &Chars) -> Option<(usize, usize)> {
    let n = line.len();
    for i in 0..n {
        if !starts_with_at(line, "openExternal", i) {
            continue;
        }
        let mut j = i + 12;
        while j < n && is_re_ws(line[j]) {
            j += 1;
        }
        if j < n && line[j] == '(' {
            return Some((i, j + 1));
        }
    }
    None
}

/// r"autoUpdater|electron-updater"
fn m_auto_updater(line: &Chars) -> Option<(usize, usize)> {
    let n = line.len();
    for i in 0..n {
        if starts_with_at(line, "autoUpdater", i) {
            return Some((i, i + 11));
        }
        if starts_with_at(line, "electron-updater", i) {
            return Some((i, i + 16));
        }
    }
    None
}

/// r"setAsDefaultProtocolClient|registerFileProtocol|registerSchemesAsPrivileged"
fn m_protocol_register(line: &Chars) -> Option<(usize, usize)> {
    let alts = [
        "setAsDefaultProtocolClient",
        "registerFileProtocol",
        "registerSchemesAsPrivileged",
    ];
    let n = line.len();
    for i in 0..n {
        for a in alts {
            if starts_with_at(line, a, i) {
                return Some((i, i + a.chars().count()));
            }
        }
    }
    None
}

/// r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
fn m_private_key_block(line: &Chars) -> Option<(usize, usize)> {
    let n = line.len();
    for i in 0..n {
        if !starts_with_at(line, "-----BEGIN ", i) {
            continue;
        }
        let mut j = i + 11;
        while j < n && (line[j] == ' ' || line[j].is_ascii_uppercase()) {
            j += 1;
        }
        // 贪婪回溯：字面量首字符 'P' 在类内，必须从长到短试起点
        for k in (i + 11..=j).rev() {
            if starts_with_at(line, "PRIVATE KEY-----", k) {
                return Some((i, k + 16));
            }
        }
    }
    None
}

/// r"\bsk-[A-Za-z0-9_\-]{20,}\b" —— 贪婪吃满类运行后 \b 恒成立（词字符 ⊆ 类），
/// 无需回溯语义。
fn m_api_key_sk(line: &Chars) -> Option<(usize, usize)> {
    let n = line.len();
    for i in 0..n {
        if i > 0 && is_word(line[i - 1]) {
            continue;
        }
        if !starts_with_at(line, "sk-", i) {
            continue;
        }
        let mut j = i + 3;
        while j < n && (line[j].is_ascii_alphanumeric() || line[j] == '_' || line[j] == '-') {
            j += 1;
        }
        if j - (i + 3) >= 20 {
            return Some((i, j));
        }
    }
    None
}

/// r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"
fn m_github_pat(line: &Chars) -> Option<(usize, usize)> {
    let n = line.len();
    for i in 0..n {
        if i > 0 && is_word(line[i - 1]) {
            continue;
        }
        if !starts_with_at(line, "gh", i) {
            continue;
        }
        match line.get(i + 2) {
            Some(c) if "pousr".contains(*c) => {}
            _ => continue,
        }
        if line.get(i + 3) != Some(&'_') {
            continue;
        }
        let mut j = i + 4;
        while j < n && line[j].is_ascii_alphanumeric() {
            j += 1;
        }
        if j - (i + 4) >= 30 {
            return Some((i, j));
        }
    }
    None
}

/// r"\bAKIA[0-9A-Z]{16}\b" —— 恰 16 位；第 17 位若是词字符则整体不匹配（\b 挡住）。
fn m_aws_access_key(line: &Chars) -> Option<(usize, usize)> {
    let n = line.len();
    for i in 0..n {
        if i > 0 && is_word(line[i - 1]) {
            continue;
        }
        if !starts_with_at(line, "AKIA", i) {
            continue;
        }
        let mut j = i + 4;
        let mut cnt = 0;
        while j < n && cnt < 16 && line[j].is_ascii_uppercase() | line[j].is_ascii_digit() {
            j += 1;
            cnt += 1;
        }
        if cnt == 16 && (j >= n || !is_word(line[j])) {
            return Some((i, j));
        }
    }
    None
}

const SECRET_KEY_ALTS: &[&str] = &[
    "api_key", "apikey", "secret", "access_token", "refresh_token", "password", "private_key",
];

fn ci_starts_with(c: &[char], lit: &str, at: usize) -> bool {
    let l: Vec<char> = lit.chars().collect();
    c.len() >= at + l.len()
        && (0..l.len()).all(|k| c[at + k].to_ascii_lowercase() == l[k].to_ascii_lowercase())
}

/// 在 at 处试一个键名候选（re.I）；api_key/access_token/refresh_token/private_key
/// 四个是 X_?Y 形（可选下划线）。命中返回键名结束位置。
fn try_key_alt(line: &[char], at: usize, alt: &str) -> Option<usize> {
    let (head, tail) = match alt {
        "api_key" => ("api", "key"),
        "access_token" => ("access", "token"),
        "refresh_token" => ("refresh", "token"),
        "private_key" => ("private", "key"),
        _ => return if ci_starts_with(line, alt, at) { Some(at + alt.chars().count()) } else { None },
    };
    if !ci_starts_with(line, head, at) {
        return None;
    }
    let mut k = at + head.chars().count();
    if line.get(k) == Some(&'_') {
        k += 1;
    }
    if ci_starts_with(line, tail, k) {
        Some(k + tail.chars().count())
    } else {
        None
    }
}

/// 键名指认型（re.I）：
/// ["'](api_?key|apikey|secret|access_?token|refresh_?token|password|private_?key)["']
/// \s*[:=]\s*["']([^"']{8,200})["']
/// 值运行 >200 时贪婪回溯无 quote 可落 → 整体不匹配（照抄）。
fn m_secret_by_key(line: &Chars) -> Option<(usize, usize)> {
    let n = line.len();
    for i in 0..n {
        if line[i] != '"' && line[i] != '\'' {
            continue;
        }
        let mut j = i + 1;
        let mut key_end: Option<usize> = None;
        for alt in SECRET_KEY_ALTS {
            if let Some(end) = try_key_alt(line, j, alt) {
                key_end = Some(end);
                break;
            }
        }
        let Some(end) = key_end else { continue };
        j = end;
        if j >= n || (line[j] != '"' && line[j] != '\'') {
            continue;
        }
        j += 1;
        while j < n && is_re_ws(line[j]) {
            j += 1;
        }
        if j >= n || (line[j] != ':' && line[j] != '=') {
            continue;
        }
        j += 1;
        while j < n && is_re_ws(line[j]) {
            j += 1;
        }
        if j >= n || (line[j] != '"' && line[j] != '\'') {
            continue;
        }
        j += 1;
        let v0 = j;
        while j < n && line[j] != '"' && line[j] != '\'' {
            j += 1;
        }
        let run = j - v0;
        if run < 8 || run > 200 {
            continue;
        }
        if j >= n || (line[j] != '"' && line[j] != '\'') {
            continue;
        }
        return Some((i, j + 1));
    }
    None
}

fn mask(m: &str) -> String {
    let chars: Vec<char> = m.chars().collect();
    let head: String = chars.iter().take(6).collect();
    format!("{}***len={}", head, chars.len())
}

// ---------- URL 盘点 ----------
// https?://[a-zA-Z0-9.\-]+(?::\d+)?(?:/[^\s"'<>)\\\]]*)?

fn is_url_host_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '.' || c == '-'
}

fn url_matches(line: &Chars) -> Vec<(usize, usize)> {
    let n = line.len();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < n {
        // https?://  —— '?' 只作用于 's'
        let scheme_len = if starts_with_at(line, "https://", i) {
            8
        } else if starts_with_at(line, "http://", i) {
            7
        } else {
            i += 1;
            continue;
        };
        let mut j = i + scheme_len;
        let h0 = j;
        while j < n && is_url_host_char(line[j]) {
            j += 1;
        }
        if j == h0 {
            i += 1;
            continue;
        }
        if line.get(j) == Some(&':') {
            let mut k = j + 1;
            while k < n && line[k].is_ascii_digit() {
                k += 1;
            }
            if k > j + 1 {
                j = k;
            }
        }
        if line.get(j) == Some(&'/') {
            j += 1;
            while j < n
                && !is_re_ws(line[j])
                && line[j] != '"'
                && line[j] != '\''
                && line[j] != '<'
                && line[j] != '>'
                && line[j] != ')'
                && line[j] != '\\'
                && line[j] != ']'
            {
                j += 1;
            }
        }
        out.push((i, j));
        i = j;
    }
    out
}

// ---------- asar 提取 ----------

struct AsarLeaf {
    rel: String,
    off: i64,
    size: i64,
    hash: Option<String>,
}

struct AsarError(String);

fn find_bytes(hay: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() {
        return None;
    }
    (from..=hay.len() - needle.len()).find(|&i| &hay[i..i + needle.len()] == needle)
}

fn read_up_to(fh: &mut std::fs::File, n: usize) -> Vec<u8> {
    let mut buf = Vec::with_capacity(n);
    let mut chunk = [0u8; 65536];
    while buf.len() < n {
        let want = (n - buf.len()).min(chunk.len());
        match fh.read(&mut chunk[..want]) {
            Ok(0) | Err(_) => break,
            Ok(k) => buf.extend_from_slice(&chunk[..k]),
        }
    }
    buf
}

/// best-effort asar 提取，移植 tools/appaudit.py::_extract_asar 的两轮内存修复版：
/// 头窗口流式读、按前导 u32 候选长度逐个试解析、基址用叶节点 SHA256 自标定。
fn extract_asar(asar_path: &Path, out_dir: &Path) -> Result<Value, AsarError> {
    let mut fh = std::fs::File::open(asar_path)
        .map_err(|e| AsarError(format!("OSError: {e}")))?;
    let mut buf = read_up_to(&mut fh, 8 * 1024 * 1024);
    let mut jstart = find_bytes(&buf, b"{\"files\"", 0).map(|i| i as i64).unwrap_or(-1);
    let mut obj: Option<Value> = None;

    fn preamble_of(buf: &[u8], js: i64) -> [u32; 4] {
        if js >= 16 {
            let s = (js - 16) as usize;
            let mut out = [0u32; 4];
            for (k, o) in out.iter_mut().enumerate() {
                *o = u32::from_le_bytes([buf[s + 4 * k], buf[s + 4 * k + 1], buf[s + 4 * k + 2], buf[s + 4 * k + 3]]);
            }
            out
        } else {
            [0, 0, 0, 0]
        }
    }

    for _round in 0..3 {
        if jstart >= 0 {
            let preamble = preamble_of(&buf, jstart);
            let mut lens: BTreeSet<i64> = BTreeSet::new();
            for &v in preamble[1..4].iter().chain(std::iter::once(&4u32)) {
                for dv in [0i64, -4, -8] {
                    let c = v as i64 + dv;
                    if c > 0 {
                        lens.insert(c);
                    }
                }
            }
            lens.insert(buf.len() as i64 - jstart);
            for &l in &lens {
                if jstart + l > buf.len() as i64 {
                    continue;
                }
                let a = jstart as usize;
                let b = (jstart + l) as usize;
                if let Ok(s) = std::str::from_utf8(&buf[a..b]) {
                    if let Ok(o) = crate::json::parse(s) {
                        obj = Some(o);
                        break;
                    }
                }
            }
        }
        if obj.is_some() {
            break;
        }
        let grow = read_up_to(&mut fh, 8 * 1024 * 1024);
        if grow.is_empty() {
            break;
        }
        let jstart_new = find_bytes(&grow, b"{\"files\"", 0);
        if jstart >= 0 || jstart_new.is_none() {
            buf.extend_from_slice(&grow); // 同窗续扫（跨窗边界截断的头）
        } else {
            jstart = buf.len() as i64 + jstart_new.unwrap() as i64;
            buf.extend_from_slice(&grow);
        }
    }
    if jstart < 0 || obj.is_none() {
        return Err(AsarError("_AsarError: 未找到或未解析出 files 头".into()));
    }
    let obj = obj.unwrap();
    let preamble = preamble_of(&buf, jstart);

    // 候选基址：JSON 起点前三个 u32 与常量 4 的组合偏移——真值交由 SHA256 裁决
    let mut cands: BTreeSet<i64> = BTreeSet::new();
    for &v in preamble[1..4].iter().chain(std::iter::once(&4u32)) {
        for dv in [0i64, 4, 8, -4] {
            let c = jstart + v as i64 + dv;
            if c > jstart {
                cands.insert(c);
            }
        }
    }

    let mut leaves: Vec<AsarLeaf> = Vec::new();
    fn walk_header(node: &Value, prefix: &str, leaves: &mut Vec<AsarLeaf>) -> Result<(), AsarError> {
        let Some(Value::Obj(files)) = node.get("files") else {
            return Ok(());
        };
        for (name, ent) in files {
            let rel = if prefix.is_empty() { name.clone() } else { format!("{prefix}/{name}") };
            if ent.get("files").is_some() {
                walk_header(ent, &rel, leaves)?;
            } else if let Some(Value::Int(size)) = ent.get("size") {
                let off = match ent.get("offset") {
                    None | Some(Value::Null) => 0,
                    Some(Value::Int(i)) => *i as i64,
                    Some(Value::Str(s)) if s.is_empty() => 0,
                    Some(Value::Str(s)) => match s.parse::<i64>() {
                        Ok(v) => v,
                        Err(_) => {
                            // Python int(s) 抛 ValueError → app_audit 外层 except 接住
                            // 转 error 条目；文本逐字复刻
                            return Err(AsarError(format!(
                                "ValueError: invalid literal for int() with base 10: '{s}'"
                            )));
                        }
                    },
                    _ => 0,
                };
                let hash = ent
                    .get("integrity")
                    .and_then(|v| v.get("hash"))
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
                leaves.push(AsarLeaf { rel, off, size: *size as i64, hash });
            }
        }
        Ok(())
    }
    // obj.get("root", obj.get("top", obj))：键存在即用（哪怕为 null）
    let tree = obj
        .get("root")
        .cloned()
        .or_else(|| obj.get("top").cloned())
        .unwrap_or_else(|| obj.clone());
    walk_header(&tree, "", &mut leaves)?;

    // 基址自标定：用前几个带 integrity 的中小文本叶试候选基址，SHA256 命中即锁定
    let mut base: Option<i64> = None;
    let hashed_probes: Vec<&AsarLeaf> = leaves
        .iter()
        .filter(|l| {
            l.hash.is_some()
                && (32..=1024 * 1024).contains(&l.size)
                && is_text_ext(&splitext_lower(&l.rel))
        })
        .take(8)
        .collect();
    'outer: for p in &hashed_probes {
        let want = p.hash.clone().unwrap();
        for &cand in &cands {
            if let Err(e) = fh.seek(SeekFrom::Start((cand + p.off) as u64)) {
                return Err(AsarError(format!("OSError: {e}")));
            }
            let data = read_up_to(&mut fh, p.size.max(0).min(MAX_ASAR_ENTRY_BYTES) as usize);
            if sha256::hex(&data) == want {
                base = Some(cand);
                break 'outer;
            }
        }
    }
    let Some(base) = base else {
        return Err(AsarError("_AsarError: 基址标定失败（integrity 全不匹配）".into()));
    };

    let _ = std::fs::create_dir_all(out_dir);
    let mut n_ext = 0i128;
    let mut n_bytes = 0i128;
    let mut n_skip = 0i128;
    for l in &leaves {
        if l.rel.is_empty() || l.rel.split('/').any(|seg| seg == "..") || is_abs_rel(&l.rel) {
            n_skip += 1;
            continue;
        }
        if !is_text_ext(&splitext_lower(&l.rel))
            || l.size > MAX_ASAR_ENTRY_BYTES
            || n_bytes > MAX_ASAR_EXTRACT_BYTES
            || n_ext >= 600
        {
            n_skip += 1;
            continue;
        }
        if l.off < 0 {
            // Python seek(负) 抛 OSError → 外层转 error 条目
            return Err(AsarError("OSError: [Errno 22] Invalid argument".into()));
        }
        if let Err(e) = fh.seek(SeekFrom::Start((base + l.off) as u64)) {
            return Err(AsarError(format!("OSError: {e}")));
        }
        let data = read_up_to(&mut fh, l.size.max(0) as usize);
        if let Some(ih) = &l.hash {
            if sha256::hex(&data) != *ih {
                n_skip += 1;
                continue;
            }
        }
        let dst = out_dir.join(l.rel.replace('/', "\\"));
        if let Some(parent) = dst.parent() {
            if let Err(e) = std::fs::create_dir_all(parent) {
                return Err(AsarError(format!("OSError: {e}")));
            }
        }
        if let Err(e) = std::fs::write(&dst, &data) {
            return Err(AsarError(format!("OSError: {e}")));
        }
        n_ext += 1;
        n_bytes += l.size as i128;
    }
    Ok(Value::Obj(vec![
        ("extracted".into(), Value::Int(n_ext)),
        ("bytes".into(), Value::Int(n_bytes)),
        ("skipped".into(), Value::Int(n_skip)),
        ("entries".into(), Value::Int(leaves.len() as i128)),
    ]))
}

/// os.path.isabs(rel) 的 Windows 语义：盘符或以分隔符开头
fn is_abs_rel(rel: &str) -> bool {
    rel.starts_with('/') || rel.starts_with('\\')
        || (rel.len() >= 2 && rel.as_bytes()[1] == b':' && rel.as_bytes()[0].is_ascii_alphabetic())
}

// ---------- 主流程 ----------

fn s(v: &str) -> Value {
    Value::Str(v.to_string())
}

fn err(msg: String) -> Value {
    Value::Obj(vec![("error".into(), Value::Str(msg))])
}

struct Finding {
    kind: &'static str,
    label: String,
    file: String,
    line: i128,
    detail: String,
}

struct ScanState {
    findings: Vec<Finding>,
    hosts: Vec<(String, i128)>,
    seen_labels: HashMap<String, i64>,
    hit_lines: i128,
}

impl ScanState {
    fn emit(&mut self, kind: &'static str, label: &str, file: &str, line: i128, detail: String) {
        if self.findings.len() < MAX_FINDINGS {
            self.findings.push(Finding {
                kind,
                label: label.to_string(),
                file: file.to_string(),
                line,
                detail,
            });
        }
    }

    fn host_add(&mut self, h: String) {
        if h.is_empty() {
            return;
        }
        match self.hosts.iter_mut().find(|(k, _)| *k == h) {
            Some((_, c)) => *c += 1,
            None => self.hosts.push((h, 1)),
        }
    }
}

const SURFACE_FNS: &[(&str, fn(&Chars) -> Option<(usize, usize)>)] = &[
    ("eval_call", m_eval_call),
    ("new_function", m_new_function),
    ("child_process", m_child_process),
    ("open_external", m_open_external),
    ("auto_updater", m_auto_updater),
    ("protocol_register", m_protocol_register),
];

const SECRET_FNS: &[(&str, &'static str, fn(&Chars) -> Option<(usize, usize)>)] = &[
    ("private_key_block", "definite", m_private_key_block),
    ("api_key_sk", "clue", m_api_key_sk),
    ("github_pat", "clue", m_github_pat),
    ("aws_access_key", "clue", m_aws_access_key),
    ("secret_by_key", "clue", m_secret_by_key),
];

/// _iter_text_rows 等价：文本扩展名 → 3MB 头 → lossy 解码 → splitlines 全集。
/// audit-ext 子目录跳过用与 Python 相同的子串判定（".audit-ext/" in rel 或
/// "/.audit-ext/" in "/"+rel）。
fn iter_text_rows(root: &Path, cb: &mut impl FnMut(&str, i128, &str)) {
    let mut files: Vec<WalkFile> = Vec::new();
    walk_files(root, &mut files);
    for f in files {
        if !is_text_ext(&splitext_lower(&f.rel)) {
            continue;
        }
        let rel = &f.rel;
        if rel.contains(".audit-ext/") || format!("/{rel}").contains("/.audit-ext/") {
            continue; // asar 提取件只走带 asar! 前缀的专用扫描，防双份计数
        }
        let mut file = match std::fs::File::open(&f.path) {
            Ok(x) => x,
            Err(_) => continue,
        };
        let head = read_up_to(&mut file, HEAD_BYTES);
        let text = String::from_utf8_lossy(&head);
        for (i, line) in py_splitlines(&text).into_iter().enumerate() {
            cb(rel, (i + 1) as i128, &line);
        }
    }
}

fn scan_line(st: &mut ScanState, file_label: &str, ln: i128, line: &str, cap_surface: bool) {
    let stripped = line.trim();
    if stripped.is_empty() || stripped.chars().count() > 800 {
        return;
    }
    let chars = to_chars(line);
    let mut hit = false;
    for (label, f) in SURFACE_FNS {
        if let Some((a, b)) = f(&chars) {
            let capped = cap_surface
                && st.seen_labels.get(*label).copied().unwrap_or(0) > 50;
            if !capped {
                if cap_surface {
                    *st.seen_labels.entry(label.to_string()).or_insert(0) += 1;
                }
                st.emit("clue", label, file_label, ln, mask(&slice(&chars, a, b)));
                hit = true;
            }
        }
    }
    for (label, kind, f) in SECRET_FNS {
        if let Some((a, b)) = f(&chars) {
            st.emit(kind, label, file_label, ln, mask(&slice(&chars, a, b)));
            hit = true;
        }
    }
    for (a, b) in url_matches(&chars) {
        let m = slice(&chars, a, b);
        let after = m.splitn(2, "//").nth(1).unwrap_or("");
        let host_part = after.split('/').next().unwrap_or("");
        let host = host_part.split(':').next().unwrap_or("").to_lowercase();
        st.host_add(host);
    }
    if hit {
        st.hit_lines += 1;
    }
}

pub fn app_audit(snapshot_dir: &str, with_asar: bool) -> Value {
    let trimmed = snapshot_dir.trim();
    if trimmed.is_empty() {
        return err("snapshot_dir 必须是非空字符串".into());
    }
    let root = PathBuf::from(trimmed);
    let sb = sandbox_root();
    if !strictly_under(trimmed, &sb) {
        return err("只允许审计隔离沙箱内的克隆；先 app_clone 再把它的 snapshot 路径传进来".into());
    }
    if !root.is_dir() {
        return err(format!("克隆不存在: {trimmed}"));
    }

    let mut st = ScanState {
        findings: Vec::new(),
        hosts: Vec::new(),
        seen_labels: HashMap::new(),
        hit_lines: 0,
    };
    iter_text_rows(&root, &mut |rel, ln, line| {
        scan_line(&mut st, rel, ln, line, true);
    });

    // ---------- asar ----------
    let mut asar_report: Vec<Value> = Vec::new();
    if with_asar {
        let mut all: Vec<WalkFile> = Vec::new();
        walk_files(&root, &mut all);
        let asars: Vec<&WalkFile> = all
            .iter()
            .filter(|f| {
                f.path
                    .file_name()
                    .map(|n| n.to_string_lossy().to_lowercase().ends_with(".asar"))
                    .unwrap_or(false)
            })
            .take(MAX_ASARS)
            .collect();
        for ap in asars {
            let ext_dir = PathBuf::from(format!("{}.audit-ext", ap.path.to_string_lossy()));
            let mut stat: Vec<(String, Value)> =
                vec![("asar".into(), s(&rel_of(&root, &ap.path)))];
            match extract_asar(&ap.path, &ext_dir) {
                Ok(v) => {
                    if let Value::Obj(pairs) = v {
                        stat.extend(pairs);
                    }
                }
                Err(AsarError(e)) => stat.push(("error".into(), s(&e))),
            }
            let extracted = stat
                .iter()
                .find(|(k, _)| k == "extracted")
                .and_then(|(_, v)| match v {
                    Value::Int(i) => Some(*i),
                    _ => None,
                })
                .unwrap_or(0);
            if extracted > 0 {
                // 提取件在克隆内 → 复扫同套规则；file 前缀 sub（保留反斜杠，
                // 与 Python os.path.relpath 行为一致）+ "/" + 提取内相对路径
                let sub = relpath_native(&ext_dir, &root);
                let mut sub_files: Vec<WalkFile> = Vec::new();
                walk_files(&ext_dir, &mut sub_files);
                for f in sub_files {
                    if !is_text_ext(&splitext_lower(&f.rel)) {
                        continue;
                    }
                    let mut file = match std::fs::File::open(&f.path) {
                        Ok(x) => x,
                        Err(_) => continue,
                    };
                    let head = read_up_to(&mut file, HEAD_BYTES);
                    let text = String::from_utf8_lossy(&head);
                    let label = format!("{sub}/{}", f.rel);
                    for (i, line) in py_splitlines(&text).into_iter().enumerate() {
                        scan_rescan_line(&mut st, &label, (i + 1) as i128, &line);
                    }
                }
            }
            asar_report.push(Value::Obj(stat));
        }
    }

    // ---------- 二进制盘点 ----------
    let mut all: Vec<WalkFile> = Vec::new();
    walk_files(&root, &mut all);
    let mut binaries: Vec<(String, i128)> = Vec::new();
    for f in &all {
        if !BINARY_INVENTORY_EXTS.contains(&splitext_lower(&f.rel).as_str()) {
            continue;
        }
        match std::fs::metadata(&f.path) {
            Ok(m) => binaries.push((f.rel.clone(), m.len() as i128)),
            Err(_) => continue,
        }
    }
    let binaries_total = binaries.len() as i128;
    binaries.sort_by(|a, b| b.1.cmp(&a.1)); // 稳定排序：同大保持遍历序

    // ---------- 汇总 ----------
    st.findings.sort_by(|a, b| {
        (a.kind != "definite")
            .cmp(&(b.kind != "definite"))
            .then_with(|| a.label.cmp(&b.label))
            .then_with(|| a.file.cmp(&b.file))
            .then_with(|| a.line.cmp(&b.line))
    });
    let definite = st.findings.iter().filter(|f| f.kind == "definite").count() as i128;
    let clues = st.findings.iter().filter(|f| f.kind == "clue").count() as i128;

    let mut hosts = st.hosts.clone();
    hosts.sort_by(|a, b| b.1.cmp(&a.1)); // 稳定：同计数保持插入序（most_common 语义）
    let url_host_top: Vec<Value> = hosts
        .iter()
        .take(25)
        .map(|(h, c)| Value::Obj(vec![("host".into(), s(h)), ("count".into(), Value::Int(*c))]))
        .collect();
    let ai_endpoint_hosts: Vec<(String, Value)> = st
        .hosts
        .iter()
        .filter(|(h, _)| AI_HOST_HINTS.iter().any(|k| h.contains(k)))
        .map(|(h, c)| (h.clone(), Value::Int(*c)))
        .collect();

    let findings: Vec<Value> = st
        .findings
        .iter()
        .map(|f| {
            Value::Obj(vec![
                ("kind".into(), s(f.kind)),
                ("label".into(), s(&f.label)),
                ("file".into(), s(&f.file)),
                ("line".into(), Value::Int(f.line)),
                ("detail".into(), s(&f.detail)),
            ])
        })
        .collect();

    Value::Obj(vec![
        ("snapshot".into(), s(trimmed)),
        ("hit_lines".into(), Value::Int(st.hit_lines)),
        ("definite".into(), Value::Int(definite)),
        ("clues".into(), Value::Int(clues)),
        ("findings".into(), Value::Arr(findings)),
        ("url_host_top".into(), Value::Arr(url_host_top)),
        ("ai_endpoint_hosts".into(), Value::Obj(ai_endpoint_hosts)),
        (
            "native_binaries_top".into(),
            Value::Arr(
                binaries
                    .iter()
                    .take(15)
                    .map(|(f, sz)| {
                        Value::Obj(vec![("file".into(), s(f)), ("size".into(), Value::Int(*sz))])
                    })
                    .collect(),
            ),
        ),
        ("binaries_total".into(), Value::Int(binaries_total)),
        ("asar".into(), Value::Arr(asar_report)),
    ])
}

/// asar 复扫行：同 scan_line 但无 surface 51 上限、不计 hit_lines、不做 URL 盘点。
fn scan_rescan_line(st: &mut ScanState, file_label: &str, ln: i128, line: &str) {
    let stripped = line.trim();
    if stripped.is_empty() || stripped.chars().count() > 800 {
        return;
    }
    let chars = to_chars(line);
    for (label, f) in SURFACE_FNS {
        if let Some((a, b)) = f(&chars) {
            st.emit("clue", &format!("asar:{label}"), file_label, ln, mask(&slice(&chars, a, b)));
        }
    }
    for (label, kind, f) in SECRET_FNS {
        if let Some((a, b)) = f(&chars) {
            st.emit(kind, &format!("asar:{label}"), file_label, ln, mask(&slice(&chars, a, b)));
        }
    }
}

/// os.path.relpath(target, start) 的 Windows 常规语义（同盘、target 在 start 下）
fn relpath_native(target: &Path, start: &Path) -> String {
    target
        .strip_prefix(start)
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| target.to_string_lossy().to_string())
}
