// SPDX-FileCopyrightText: 2026 bfxh
// SPDX-License-Identifier: MIT
//! rx-search — 本地语义代码检索（Rust）。
//!
//! 目标（用户："定位问题很麻烦很弱"——CocoIndex/codesearch/Canopy 式）：
//!   - **零依赖**：纯标准库 + serde_json（对齐 rx-core 承诺）
//!   - **token 化**：标识符拆词（camelCase/snake_case）+ 中文连续
//!     CJK 按 bigram + 英文单词（去停用词）
//!   - **符号表**：函数/类/结构体定义名 + 行号（fn/def/struct/class…）
//!   - **BM25 混合检索**：符号名精确/前缀命中加权 + 词频相关性
//!   - **常驻 serve 行协议**（对齐 rx-core/rx-telemetry）：
//!     {"cmd":"index","root":...} / {"cmd":"search","q":...,"k":...}
//!
//! 集成：server.py `semantic_search` 工具经 search_core.py 桥接调用。

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

// ─────────────────────────────────────────────────────────────
// token 化
// ─────────────────────────────────────────────────────────────

const STOP_WORDS: &[&str] = &[
    "the", "a", "an", "and", "or", "for", "if", "in", "of", "is", "to",
    "with", "on", "at", "by", "as", "it", "this", "that", "from", "are",
    "was", "be", "not", "do", "does", "but", "we", "you", "they", "he",
    "she", "them", "our", "your", "my", "its", "into", "out", "up", "down",
];

/// 判断是否 CJK 统一表意文字。
fn is_cjk(c: char) -> bool {
    matches!(c as u32,
        0x3400..=0x4DBF | 0x4E00..=0x9FFF | 0xF900..=0xFAFF
        | 0x20000..=0x2A6DF | 0x2A700..=0x2B73F)
}

fn is_ident_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

/// 标识符拆词：camelCase / snake_case / 大写缩写 → 小写词。
fn split_identifier(ident: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let chars: Vec<char> = ident.chars().collect();
    let flush = |cur: &mut String, out: &mut Vec<String>| {
        if !cur.is_empty() {
            out.push(cur.clone());
            cur.clear();
        }
    };
    for (i, &c) in chars.iter().enumerate() {
        if c == '_' {
            flush(&mut cur, &mut out);
            continue;
        }
        if c.is_ascii_uppercase() && !cur.is_empty() {
            let prev_c = chars[i - 1];
            if prev_c.is_ascii_lowercase() {
                // 小写→大写：词边界（placement|Target）
                flush(&mut cur, &mut out);
            } else if prev_c.is_ascii_uppercase()
                && i + 1 < chars.len()
                && chars[i + 1].is_ascii_lowercase()
                && cur.len() > 1 {
                // 大写串→小写：缩写尾边界（HTTP|Request——在 R 处切）
                flush(&mut cur, &mut out);
            }
        }
        cur.push(c.to_ascii_lowercase());
    }
    flush(&mut cur, &mut out);
    out
}

/// 中文连续串 → bigram。
fn chinese_bigrams(s: &str) -> Vec<String> {
    let cjk: Vec<char> = s.chars().filter(|&c| is_cjk(c)).collect();
    if cjk.len() == 1 {
        return vec![cjk[0].to_string()];
    }
    cjk.windows(2).map(|w| w.iter().collect()).collect()
}

/// 一行源码 → token 列表（标识符拆词 + 注释/字符串中文 bigram + 英文词）。
fn line_tokens(line: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut ident = String::new();
    let mut ascii_word = String::new();
    let mut cjk_run = String::new();
    let mut in_string = false;
    let mut quote = ' ';
    let mut in_comment = false;
    let mut prev = ' ';

    let flush_ident = |ident: &mut String, out: &mut Vec<String>| {
        if !ident.is_empty() {
            out.extend(split_identifier(ident));
            ident.clear();
        }
    };

    for c in line.chars() {
        if in_comment {
            // 注释里：英文词 + 中文 bigram
            if c.is_ascii_alphabetic() {
                ascii_word.push(c.to_ascii_lowercase());
                continue;
            }
            if !ascii_word.is_empty() {
                if !STOP_WORDS.contains(&ascii_word.as_str()) && ascii_word.len() > 1 {
                    out.push(ascii_word.clone());
                }
                ascii_word.clear();
            }
            if is_cjk(c) {
                cjk_run.push(c);
            } else if !cjk_run.is_empty() {
                out.extend(chinese_bigrams(&cjk_run));
                cjk_run.clear();
            }
            continue;
        }
        if in_string {
            if c == quote && prev != '\\' {
                in_string = false;
            }
            // 字符串内：中文 bigram + 英文词（可检索语义线索）
            if is_cjk(c) {
                cjk_run.push(c);
            } else if !cjk_run.is_empty() {
                out.extend(chinese_bigrams(&cjk_run));
                cjk_run.clear();
            }
            if c.is_ascii_alphabetic() {
                ascii_word.push(c.to_ascii_lowercase());
            } else if !ascii_word.is_empty() {
                if !STOP_WORDS.contains(&ascii_word.as_str()) && ascii_word.len() > 1 {
                    out.push(ascii_word.clone());
                }
                ascii_word.clear();
            }
            prev = c;
            continue;
        }
        // 代码区
        if c == '#' || (c == '/' && prev == '/') {
            in_comment = true;
            prev = c;
            continue;
        }
        if c == '"' || c == '\'' || c == '`' {
            flush_ident(&mut ident, &mut out);
            if !ascii_word.is_empty() {
                ascii_word.clear();
            }
            in_string = true;
            quote = c;
            prev = c;
            continue;
        }
        if is_ident_char(c) {
            ident.push(c);
            prev = c;
            continue;
        }
        flush_ident(&mut ident, &mut out);
        if !ascii_word.is_empty() {
            if !STOP_WORDS.contains(&ascii_word.as_str()) && ascii_word.len() > 1 {
                out.push(ascii_word.clone());
            }
            ascii_word.clear();
        }
        if is_cjk(c) {
            cjk_run.push(c);
        } else if !cjk_run.is_empty() {
            out.extend(chinese_bigrams(&cjk_run));
            cjk_run.clear();
        }
        prev = c;
    }
    flush_ident(&mut ident, &mut out);
    if !ascii_word.is_empty() && !STOP_WORDS.contains(&ascii_word.as_str())
        && ascii_word.len() > 1 {
        out.push(ascii_word);
    }
    if !cjk_run.is_empty() {
        out.extend(chinese_bigrams(&cjk_run));
    }
    out
}

/// 从源码行提取符号名（fn/def/struct/class/impl/const/let/type/pub…）。
fn symbol_from_line(line: &str) -> Option<String> {
    let t = line.trim();
    for kw in ["fn ", "def ", "struct ", "class ", "impl ", "enum ",
               "type ", "const ", "function ", "func ", "let ", "pub fn ",
               "pub struct ", "pub enum ", "pub fn ", "mod ", "trait "] {
        if let Some(idx) = t.find(kw) {
            let rest = &t[idx + kw.len()..];
            let name: String = rest.chars().take_while(|c| {
                c.is_ascii_alphanumeric() || *c == '_' || *c == '<'
            }).collect();
            let name = name.trim_end_matches('<').trim();
            if !name.is_empty() && name.chars().next().unwrap().is_alphabetic() {
                return Some(name.to_string());
            }
        }
    }
    None
}

// ─────────────────────────────────────────────────────────────
// 索引
// ─────────────────────────────────────────────────────────────

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Doc {
    pub path: String,
    /// term → 词频
    pub tokens: HashMap<String, u32>,
    /// (符号名, 行号)
    pub symbols: Vec<(String, u32)>,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Index {
    pub docs: Vec<Doc>,
    /// term → 文档数（df）
    pub df: HashMap<String, u32>,
    pub n_docs: u32,
}

const INDEXABLE_EXT: &[&str] = &[
    "rs", "py", "js", "ts", "jsx", "tsx", "go", "c", "h", "cpp", "hpp",
    "java", "kt", "rb", "php", "swift", "md", "toml", "json", "yaml",
    "yml", "sh", "bat", "ps1", "vue", "html", "css", "scss",
];

fn is_indexable(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| INDEXABLE_EXT.contains(&e.to_ascii_lowercase().as_str()))
        .unwrap_or(false)
}

/// 遍历目录收集可索引文件。
fn collect_files(root: &Path, limit: usize) -> Vec<PathBuf> {
    let skip: HashSet<&str> = [
        ".git", "node_modules", "__pycache__", ".venv", "venv", "target",
        "vendor", ".pytest_cache", "build", "dist", ".unified-rx-index",
        ".rx-target",
    ].iter().copied().collect();
    let mut out = Vec::new();
    if !root.is_dir() {
        return out;
    }
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        if out.len() >= limit {
            break;
        }
        let Ok(rd) = fs::read_dir(&dir) else { continue };
        for entry in rd.flatten() {
            let p = entry.path();
            if p.is_dir() {
                if !skip.contains(p.file_name().and_then(|n| n.to_str()).unwrap_or("")) {
                    stack.push(p);
                }
            } else if is_indexable(&p) && out.len() < limit {
                out.push(p);
            }
        }
    }
    out.sort();
    out
}

/// 索引一个文件 → Doc。
pub fn index_file(path: &Path) -> Option<Doc> {
    let src = fs::read_to_string(path).ok()?;
    let mut tokens: HashMap<String, u32> = HashMap::new();
    let mut symbols: Vec<(String, u32)> = Vec::new();
    for (i, line) in src.lines().enumerate() {
        for t in line_tokens(line) {
            *tokens.entry(t).or_insert(0) += 1;
        }
        if let Some(name) = symbol_from_line(line) {
            symbols.push((name, (i + 1) as u32));
        }
    }
    // 符号名也计入 token（前缀匹配检索）
    for (name, _) in &symbols {
        for t in split_identifier(name) {
            *tokens.entry(t).or_insert(0) += 1;
        }
    }
    Some(Doc {
        path: path.to_string_lossy().into_owned(),
        tokens,
        symbols,
    })
}

/// 构建索引（root 下全部文件）。
pub fn build_index(root: &Path, limit: usize) -> Index {
    let mut idx = Index::default();
    let mut df: HashMap<String, HashSet<usize>> = HashMap::new();
    for path in collect_files(root, limit) {
        if let Some(doc) = index_file(&path) {
            let di = idx.docs.len();
            for t in doc.tokens.keys() {
                df.entry(t.clone()).or_default().insert(di);
            }
            idx.docs.push(doc);
        }
    }
    idx.n_docs = idx.docs.len() as u32;
    idx.df = df.into_iter()
        .map(|(t, s)| (t, s.len() as u32))
        .collect();
    idx
}

// ─────────────────────────────────────────────────────────────
// BM25 检索
// ─────────────────────────────────────────────────────────────

const K1: f64 = 1.5;
const B: f64 = 0.75;

#[derive(Serialize, Clone, Debug)]
pub struct Hit {
    pub path: String,
    pub line: u32,
    pub score: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub symbol: Option<String>,
}

/// 查询 token 化（复用行 token 逻辑，取词）。
pub fn query_tokens(q: &str) -> Vec<String> {
    line_tokens(q)
        .into_iter()
        .filter(|t| t.len() > 1)
        .collect()
}

/// BM25 检索：符号名命中加权 + 词频相关性。
pub fn search(idx: &Index, q: &str, k: usize) -> Vec<Hit> {
    let qt = query_tokens(q);
    if qt.is_empty() || idx.n_docs == 0 {
        return Vec::new();
    }
    let n = idx.n_docs as f64;
    let avgdl = if idx.docs.is_empty() {
        1.0
    } else {
        idx.docs.iter().map(|d| d.tokens.values().sum::<u32>() as f64)
            .sum::<f64>() / idx.docs.len() as f64
    };
    let mut scores: Vec<(usize, f64)> = Vec::new();
    for (di, doc) in idx.docs.iter().enumerate() {
        let dl = doc.tokens.values().sum::<u32>() as f64;
        let mut score = 0.0;
        for t in &qt {
            let df = *idx.df.get(t).unwrap_or(&0) as f64;
            if df == 0.0 {
                continue;
            }
            let tf = *doc.tokens.get(t).unwrap_or(&0) as f64;
            if tf == 0.0 {
                continue;
            }
            let idf = ((n - df + 0.5) / (df + 0.5) + 1.0).ln();
            score += idf * (tf * (K1 + 1.0)) / (tf + K1 * (1.0 - B + B * dl / avgdl));
        }
        if score > 0.0 {
            scores.push((di, score));
        }
    }
    // 符号名加权：查询含符号前缀/子串 → +2.0
    for (di, doc) in idx.docs.iter().enumerate() {
        if let Some((_, sc)) = scores.iter_mut().find(|(i, _)| *i == di) {
            for (name, _) in &doc.symbols {
                for t in &qt {
                    let lname = name.to_ascii_lowercase();
                    if lname == *t || lname.starts_with(t) {
                        *sc += 2.0;
                        break;
                    }
                }
            }
        }
    }
    scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    let mut out = Vec::new();
    for (di, score) in scores.into_iter().take(k) {
        let doc = &idx.docs[di];
        // 命中符号行（取第一个与查询相关的符号；否则 0 行）
        let mut line = 0u32;
        let mut symbol: Option<String> = None;
        for (name, ln) in &doc.symbols {
            let lname = name.to_ascii_lowercase();
            if qt.iter().any(|t| lname == *t || lname.starts_with(t)) {
                line = *ln;
                symbol = Some(name.clone());
                break;
            }
        }
        out.push(Hit {
            path: doc.path.clone(),
            line,
            score: (score * 100.0).round() / 100.0,
            symbol,
        });
    }
    out
}

// ─────────────────────────────────────────────────────────────
// 测试
// ─────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_camel_snake() {
        assert_eq!(split_identifier("placementTarget"),
                   vec!["placement", "target"]);
        assert_eq!(split_identifier("hit_module"), vec!["hit", "module"]);
        assert_eq!(split_identifier("HTTPRequest"), vec!["http", "request"]);
    }

    #[test]
    fn chinese_bigram_ok() {
        assert_eq!(chinese_bigrams("命中盒"), vec!["命中", "中盒"]);
    }

    #[test]
    fn line_tokens_mixed() {
        let toks = line_tokens("fn hit_module() { // 命中盒计算 模块");
        assert!(toks.contains(&"hit".to_string()));
        assert!(toks.contains(&"module".to_string()));
        assert!(toks.contains(&"命中".to_string()));
        assert!(toks.contains(&"中盒".to_string()));
        assert!(toks.contains(&"计算".to_string()));
    }

    #[test]
    fn symbol_extract() {
        assert_eq!(symbol_from_line("fn hit_module() -> Option<Vec3i> {"),
                   Some("hit_module".into()));
        assert_eq!(symbol_from_line("pub struct Assembly {"),
                   Some("Assembly".into()));
        assert_eq!(symbol_from_line("def placement_target(ray):"),
                   Some("placement_target".into()));
    }

    #[test]
    fn index_and_search() {
        let dir = std::env::temp_dir().join("rx-search-test");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("a.rs"),
                  "pub fn placement_target() {}\n// 放置 命中\n").unwrap();
        fs::write(dir.join("b.rs"),
                  "pub fn render_frame() {}\n// 渲染 帧率\n").unwrap();
        let idx = build_index(&dir, 1000);
        assert_eq!(idx.n_docs, 2);
        // 符号名查询 → 命中 a.rs 且符号行定位
        let hits = search(&idx, "placement", 5);
        assert!(!hits.is_empty());
        assert!(hits[0].path.ends_with("a.rs"));
        assert_eq!(hits[0].line, 1);
        assert_eq!(hits[0].symbol.as_deref(), Some("placement_target"));
        // 中文查询
        let hits2 = search(&idx, "渲染", 5);
        assert!(!hits2.is_empty());
        assert!(hits2[0].path.ends_with("b.rs"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn empty_index_search() {
        let idx = Index::default();
        assert!(search(&idx, "anything", 5).is_empty());
    }
}
