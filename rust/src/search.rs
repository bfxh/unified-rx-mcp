//! search —— code_search 的 Rust 原生实现（S80）：BM25 文件级代码检索。
//!
//! 等价复刻 tools/search.py 迁移前语义（S12 进程内索引缓存随 Python 实现退役：
//! 短命进程无从缓存，Rust 版单次含建索引整体耗时见 ROUNDLOG S80 实测）。
//! 关键对齐点：
//! - 分词器：camelCase/PascalCase 拆词 + snake 下划线分隔 + 中文 bigram + 停用词，
//!   无 regex crate，手写字符状态机（等价原正则
//!   `[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+`）；
//! - BM25：idf = ln(1 + (N-df+0.5)/(df+0.5))，k=1.5, b=0.75，score>0 过滤；
//! - 行重排：查询 token 交集计数 + 原始词形（≥4 字符标识符/≥2 连续中文）行内
//!   精确出现 +6（精确符号置顶，S13 语义）；
//! - 收录面：18 种扩展名、跳过 .git/node_modules/target 等 8 目录、文件数上限 200
//!   （截断顺序 = 遍历结构：每层先本目录文件再下钻，目录内按 NTFS upcase 排序——
//!   与 os.walk/scandir 对齐，S80 对照实验实锤后定契约）；
//! - 无沙盒门：与 Python 版一致（S75 审计定性：纯读分析=本职）。

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::json::Value;

pub const MAX_FILES: usize = 200;
const K1: f64 = 1.5;
const B: f64 = 0.75;
const RAW_TERM_BOOST: i64 = 6;

const SKIP_DIRS: [&str; 8] = [".git", "node_modules", "target", "__pycache__",
    "dist", "build", ".unified-rx-index", "backups"];

const INDEX_EXTS: [&str; 18] = [".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx",
    ".gd", ".cs", ".dart", ".lua", ".java", ".kt", ".md", ".toml", ".json",
    ".yaml", ".yml"];

const STOPWORDS: [&str; 39] = ["the", "a", "an", "is", "are", "was", "were", "of",
    "in", "on", "at", "to", "from", "and", "or", "for", "with", "this", "that",
    "it", "as", "by", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "但", "是", "的", "了", "在", "与", "和", "或"];

/// 主入口：root 下 BM25 检索。错误（root 非目录）与旧实现同文案、走正常返回
/// （registry 对 {"error":...} 统一转 ok:false）。
pub fn code_search(root: &Path, query: &str, k: usize) -> Value {
    if !root.is_dir() {
        return err_obj(&format!("不是目录: {}", root.display()));
    }
    let mut docs: Vec<PathBuf> = Vec::new();
    walk(root, &mut docs);
    // 等价 Python _index：读取失败 continue（上限名额照烧、文档不入库）
    docs.retain(|p| std::fs::read(p).is_ok());
    let n = docs.len() as f64;

    // 倒排索引：token -> [(doc_id, tf)]；doc_len 同步记录
    let mut idx: HashMap<String, Vec<(u32, i64)>> = HashMap::new();
    let mut doc_len: Vec<f64> = Vec::with_capacity(docs.len());
    for (id, path) in docs.iter().enumerate() {
        let toks = tokenize(&read_text(path));
        doc_len.push(toks.len() as f64);
        let mut tf: HashMap<&str, i64> = HashMap::new();
        for t in &toks {
            *tf.entry(t.as_str()).or_insert(0) += 1;
        }
        for (t, c) in tf {
            idx.entry(t.to_string()).or_default().push((id as u32, c));
        }
    }
    let total_len: f64 = doc_len.iter().sum();
    let avgdl = total_len / (doc_len.len().max(1) as f64);

    // BM25 打分（按查询 token 首现顺序累加，确定性输出）
    let q_toks = tokenize(query);
    if q_toks.is_empty() {
        return hits_obj(query, Vec::new());
    }
    let mut uniq: Vec<&str> = Vec::new();
    for t in &q_toks {
        if !uniq.iter().any(|u| *u == t.as_str()) {
            uniq.push(t.as_str());
        }
    }
    let mut scores: Vec<(u32, f64)> = Vec::new();
    for t in &uniq {
        let posts = match idx.get(*t) {
            Some(p) => p,
            None => continue,
        };
        let df = posts.len() as f64;
        let idf = (1.0 + (n - df + 0.5) / (df + 0.5)).ln();
        for (id, tf) in posts {
            let dl = doc_len[*id as usize];
            let denom = *tf as f64 + K1 * (1.0 - B + B * dl / f64::max(1.0, avgdl));
            bump(&mut scores, *id, idf * *tf as f64 / denom);
        }
    }
    scores.retain(|(_, s)| *s > 0.0);
    scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    // 候选文件行重排（ranked[:k*2]），精确符号置顶（S13）
    let raw_terms = raw_terms(query);
    let q_set: Vec<&str> = uniq;
    let mut hits: Vec<Value> = Vec::new();
    for (id, score) in scores.iter().take(k * 2) {
        let lines = read_lines(&docs[*id as usize]);
        let mut best_line = 1usize;
        let mut best_score = 0i64;
        for (i, line) in lines.iter().enumerate() {
            let lt = tokenize(line);
            let mut hit = 0i64;
            for t in &q_set {
                if lt.iter().any(|x| x == t) {
                    hit += 1;
                }
            }
            let low = line.to_lowercase();
            if raw_terms.iter().any(|rt| low.contains(rt.as_str())) {
                hit += RAW_TERM_BOOST;
            }
            if hit > best_score {
                best_score = hit;
                best_line = i + 1;
            }
        }
        if best_score == 0 && hits.len() >= k {
            continue;
        }
        let snippet = lines
            .get(best_line - 1)
            .map(|l| l.trim().chars().take(120).collect::<String>())
            .unwrap_or_default();
        hits.push(Value::Obj(vec![
            ("file".into(), Value::Str(docs[*id as usize].to_string_lossy().into_owned())),
            ("line".into(), Value::Int(best_line as i128)),
            ("score".into(), Value::Float((score * 1000.0).round() / 1000.0)),
            ("snippet".into(), Value::Str(snippet)),
        ]));
        if hits.len() >= k {
            break;
        }
    }
    hits_obj(query, hits)
}

fn hits_obj(query: &str, hits: Vec<Value>) -> Value {
    Value::Obj(vec![
        ("query".into(), Value::Str(query.into())),
        ("total".into(), Value::Int(hits.len() as i128)),
        ("hits".into(), Value::Arr(hits)),
    ])
}

fn err_obj(msg: &str) -> Value {
    Value::Obj(vec![("error".into(), Value::Str(msg.into()))])
}

/// 累加某文档的 BM25 分（不存在则落一 Newly）。
fn bump(scores: &mut Vec<(u32, f64)>, id: u32, add: f64) {
    if let Some(p) = scores.iter_mut().find(|(i, _)| *i == id) {
        p.1 += add;
    } else {
        scores.push((id, add));
    }
}

/// 查询的原始词形：≥4 字符标识符（小写）+ ≥2 连续中文——S13 行重排加分用。
pub fn raw_terms(query: &str) -> Vec<String> {
    let cs: Vec<char> = query.chars().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < cs.len() {
        let c = cs[i];
        if c.is_ascii_alphabetic() || c == '_' {
            let start = i;
            i += 1;
            while i < cs.len() && (cs[i].is_ascii_alphanumeric() || cs[i] == '_') {
                i += 1;
            }
            if i - start >= 4 {
                out.push(cs[start..i].iter().collect::<String>().to_lowercase());
            }
        } else if is_cjk(c) {
            let start = i;
            while i < cs.len() && is_cjk(cs[i]) {
                i += 1;
            }
            if i - start >= 2 {
                out.push(cs[start..i].iter().collect());
            }
        } else {
            i += 1;
        }
    }
    out
}

/// 分词：标识符拆词 + 中文 bigram；过滤停用词与单字符 token。
pub fn tokenize(text: &str) -> Vec<String> {
    let cs: Vec<char> = text.chars().collect();
    let mut out: Vec<String> = Vec::new();
    let mut i = 0;
    while i < cs.len() {
        let c = cs[i];
        if c.is_ascii_alphabetic() || c == '_' {
            let start = i;
            i += 1;
            while i < cs.len() && (cs[i].is_ascii_alphanumeric() || cs[i] == '_') {
                i += 1;
            }
            let w: String = cs[start..i].iter().collect();
            for p in camel_split(&w) {
                if p.chars().count() > 1 {
                    out.push(p.to_lowercase());
                }
            }
            out.push(w.to_lowercase());
        } else if is_cjk(c) {
            let start = i;
            while i < cs.len() && is_cjk(cs[i]) {
                i += 1;
            }
            let seg: String = cs[start..i].iter().collect();
            let sc: Vec<char> = seg.chars().collect();
            if sc.len() == 1 {
                out.push(seg);
            } else {
                for j in 0..sc.len() - 1 {
                    out.push(sc[j..j + 2].iter().collect());
                }
                out.push(seg);
            }
        } else {
            i += 1;
        }
    }
    out.retain(|t| !STOPWORDS.contains(&t.as_str()) && t.chars().count() > 1);
    out
}

/// 等价正则 `[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+` 的拆词：
/// 大写串后接小写 → 前缀/尾串分离（HTTPServer → HTTP+Server）；
/// '_' 与其他符号为分隔符；数字串独立。
fn camel_split(w: &str) -> Vec<String> {
    let cs: Vec<char> = w.chars().collect();
    let n = cs.len();
    let mut out = Vec::new();
    let mut i = 0;
    while i < n {
        let c = cs[i];
        if c.is_ascii_uppercase() {
            let start = i;
            while i < n && cs[i].is_ascii_uppercase() {
                i += 1;
            }
            if i < n && cs[i].is_ascii_lowercase() {
                if i - start > 1 {
                    out.push(cs[start..i - 1].iter().collect());
                }
                let lo_start = i - 1;
                while i < n && cs[i].is_ascii_lowercase() {
                    i += 1;
                }
                out.push(cs[lo_start..i].iter().collect());
            } else {
                out.push(cs[start..i].iter().collect());
            }
        } else if c.is_ascii_lowercase() {
            let start = i;
            while i < n && cs[i].is_ascii_lowercase() {
                i += 1;
            }
            out.push(cs[start..i].iter().collect());
        } else if c.is_ascii_digit() {
            let start = i;
            while i < n && cs[i].is_ascii_digit() {
                i += 1;
            }
            out.push(cs[start..i].iter().collect());
        } else {
            i += 1;
        }
    }
    out
}

fn is_cjk(c: char) -> bool {
    ('\u{4e00}'..='\u{9fff}').contains(&c)
}

/// 收录文件遍历：8 个跳过目录 + 18 种扩展名 + 200 文件上限。
/// 结构等价 os.walk：每层先收本目录文件再下钻子目录（根目录源码优先入库，
/// 200 上限截断顺序由遍历结构决定，不是任意序）；目录符号链接不下钻
/// （followlinks=False）。目录内顺序按 NTFS upcase 排序——os.scandir 在 NTFS
/// 上返回 $UpCase 排序的目录项，字节序会把大写文件排到小写目录之前，语料
/// 选取就全变了（S80 对照实验实锤：bench/ 先于 conftest.py 烧光名额）。
fn walk(root: &Path, out: &mut Vec<PathBuf>) {
    let rd = match std::fs::read_dir(root) {
        Ok(r) => r,
        Err(_) => return,
    };
    let mut files: Vec<PathBuf> = Vec::new();
    let mut dirs: Vec<PathBuf> = Vec::new();
    for e in rd.filter_map(|e| e.ok()) {
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_symlink() {
            continue;
        }
        if ft.is_dir() {
            dirs.push(e.path());
        } else {
            files.push(e.path());
        }
    }
    let by_upcase = |a: &PathBuf, b: &PathBuf| {
        let an = a.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default();
        let bn = b.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default();
        an.to_uppercase().cmp(&bn.to_uppercase()).then_with(|| an.cmp(&bn))
    };
    files.sort_by(by_upcase);
    dirs.sort_by(by_upcase);
    for p in files {
        if out.len() >= MAX_FILES {
            return;
        }
        let ext = p.extension().map(|e| format!(".{}", e.to_string_lossy().to_lowercase()));
        if let Some(ext) = ext {
            if INDEX_EXTS.contains(&ext.as_str()) {
                out.push(p);
            }
        }
    }
    for p in dirs {
        if out.len() >= MAX_FILES {
            return;
        }
        let name = p.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default();
        if SKIP_DIRS.contains(&name.as_str()) {
            continue;
        }
        walk(&p, out);
    }
}

/// 读文本（utf-8 errors=replace 等价）。
fn read_text(p: &Path) -> String {
    match std::fs::read(p) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(_) => String::new(),
    }
}

/// 按行拆分（universal newlines 后按 \n 切，行尾不带换行符——
/// 行号定位与 strip 截断都只依赖行内容）。
fn read_lines(p: &Path) -> Vec<String> {
    let text = read_text(p);
    let text = if text.contains('\r') {
        text.replace("\r\n", "\n").replace('\r', "\n")
    } else {
        text
    };
    text.split('\n').map(|s| s.to_string()).collect()
}
