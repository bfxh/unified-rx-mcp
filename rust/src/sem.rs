//! sem —— code_semantic 的 Rust 原生实现（S81）：符号定义级 tf-idf 余弦语义检索。
//!
//! 等价复刻 tools/search.py S31 迁移前语义（S12 进程内 _SEM_CACHE 随 Python 实现
//! 退役）。关键对齐点：
//! - 定义提取：py def/async def/class、rs fn/struct/enum/trait/impl（含泛型与
//!   `for`）、go func（方法接收者）、js function/class，全部手写匹配器逐例对齐
//!   原正则的锚定与回溯语义（含 `impl Trait for` 的 A/B 分支回溯、`.ts` 不算 js
//!   的 lang 怪癖）；定义上方紧邻注释行（// # """ '''）折入定义体；
//! - 向量：名称 token ×3 + 名称 char-trigram ×2 + 定义体 token ×1，权重 =
//!   (1+ln(tf)) * idf，idf = 0.4 + 0.6*ln(1 + N/(1+df))（df 只含名称/定义体
//!   token——trigram 恒取默认 1.0，与 Python 一致）；
//! - tf 累加与点积按 Python dict 插入序（名称→trigram→定义体）保序，浮点行为
//!   与原实现逐位同序；
//! - 阈值：search 模式首个 ≤0.02 即 break；related 模式取前 k 后再过滤 >0.05
//!   （先截断后过滤，不回填——原实现语义）；
//! - 遍历：与 code_search 同款"每层先文件后目录 + NTFS upcase 排序"，200 文件
//!   上限只计扩展名命中文件，4000 定义上限全局截断；空语料返回无 mode 键的
//!   空结果（原实现如此）。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::json::Value;
use crate::search::{tokenize, INDEX_EXTS, MAX_FILES, SKIP_DIRS};

const SEM_BODY_CAP: usize = 40;
const SEM_MAX_DEFS: usize = 4000;
const SEARCH_FLOOR: f64 = 0.02;
const RELATED_FLOOR: f64 = 0.05;

/// 主入口：mode = "search" | "related"（bin 层已校验）。
pub fn code_semantic(root: &Path, query: &str, mode: &str, k: usize) -> Value {
    if !root.is_dir() {
        return err_obj(&format!("不是目录: {}", root.display()));
    }
    let mut st = SemState { count: 0, defs: Vec::new() };
    walk_sem(root, &mut st);
    if st.defs.is_empty() {
        // 原实现：空语料返回 {"query","total":0,"hits":[]}——无 mode 键
        return Value::Obj(vec![
            ("query".into(), Value::Str(query.into())),
            ("total".into(), Value::Int(0)),
            ("hits".into(), Value::Arr(Vec::new())),
        ]);
    }
    // idf：df 只统计名称 token ∪ 定义体 token（每定义去重计 1）
    let mut df: HashMap<String, i64> = HashMap::new();
    for d in &st.defs {
        let mut seen: HashSet<String> = HashSet::new();
        for t in tokenize(&d.name) {
            seen.insert(t);
        }
        for t in tokenize(&d.body) {
            seen.insert(t);
        }
        for t in seen {
            *df.entry(t.to_string()).or_insert(0) += 1;
        }
    }
    let n = st.defs.len().max(1) as f64;
    let idf: HashMap<String, f64> = df
        .into_iter()
        .map(|(t, c)| (t, 0.4 + 0.6 * (1.0 + n / (1.0 + c as f64)).ln()))
        .collect();
    for d in &mut st.defs {
        d.vec = sem_vec(&d.body, &d.name, &idf);
    }

    if mode == "related" {
        let target_idx = match st.defs.iter().position(|d| d.name == query) {
            Some(i) => i,
            None => {
                // 模糊：最高余弦的定义当锚点（首个最大值，与 Python max 一致）
                let qv = sem_vec(query, query, &idf);
                let mut best = 0usize;
                let mut bs = f64::NEG_INFINITY;
                for (i, d) in st.defs.iter().enumerate() {
                    let s = cosine(&qv, &d.vec);
                    if s > bs {
                        bs = s;
                        best = i;
                    }
                }
                best
            }
        };
        let mut scored: Vec<(usize, f64)> = st
            .defs
            .iter()
            .enumerate()
            .filter(|(i, _)| *i != target_idx)
            .map(|(i, d)| (i, cosine(&st.defs[target_idx].vec, &d.vec)))
            .collect();
        scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        let hits: Vec<Value> = scored
            .iter()
            .take(k)
            .filter(|(_, s)| *s > RELATED_FLOOR)
            .map(|(i, s)| hit_obj(&st.defs[*i], round3(*s), None))
            .collect();
        return Value::Obj(vec![
            ("query".into(), Value::Str(query.into())),
            ("mode".into(), Value::Str("related".into())),
            ("anchor".into(), Value::Str(st.defs[target_idx].name.clone())),
            ("total".into(), Value::Int(hits.len() as i128)),
            ("hits".into(), Value::Arr(hits)),
        ]);
    }

    let qv = sem_vec(query, query, &idf);
    let mut scored: Vec<(usize, f64)> = st
        .defs
        .iter()
        .enumerate()
        .map(|(i, d)| (i, cosine(&qv, &d.vec)))
        .collect();
    scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    let mut hits: Vec<Value> = Vec::new();
    for (i, s) in scored.iter().take(k) {
        if *s <= SEARCH_FLOOR {
            break; // 原实现是 break：首个低于阈值即停
        }
        let snippet = read_snippet(&st.defs[*i].file, st.defs[*i].line);
        hits.push(hit_obj(&st.defs[*i], round3(*s), Some(snippet)));
    }
    Value::Obj(vec![
        ("query".into(), Value::Str(query.into())),
        ("mode".into(), Value::Str("search".into())),
        ("total".into(), Value::Int(hits.len() as i128)),
        ("hits".into(), Value::Arr(hits)),
    ])
}

// ================= 定义提取（手写匹配器，等价原 7 条锚定正则） =================

struct Def {
    file: PathBuf,
    line: usize,
    kind: &'static str,
    name: String,
    body: String,
    vec: SemVec,
}

struct SemState {
    count: usize,
    defs: Vec<Def>,
}

fn skip_ws(cs: &[char], mut i: usize) -> usize {
    while i < cs.len() && cs[i].is_whitespace() {
        i += 1;
    }
    i
}

/// \s+（至少一个空白）
fn scan_ws1(cs: &[char], i: usize) -> Option<usize> {
    let j = skip_ws(cs, i);
    if j > i {
        Some(j)
    } else {
        None
    }
}

/// 吃掉字面关键字（ASCII 精确匹配）
fn eat_kw(cs: &[char], i: usize, kw: &str) -> Option<usize> {
    let kc: Vec<char> = kw.chars().collect();
    if i + kc.len() <= cs.len() && cs[i..i + kc.len()] == kc[..] {
        Some(i + kc.len())
    } else {
        None
    }
}

/// \w+（Unicode 字母数字 + 下划线，对齐 Python str 正则的 \w）
fn scan_ident(cs: &[char], i: usize) -> Option<(usize, usize)> {
    let s = i;
    let mut e = i;
    while e < cs.len() && (cs[e].is_alphanumeric() || cs[e] == '_') {
        e += 1;
    }
    if e > s {
        Some((s, e))
    } else {
        None
    }
}

fn ident_at(cs: &[char], i: usize) -> Option<String> {
    scan_ident(cs, i).map(|(s, e)| cs[s..e].iter().collect())
}

/// `^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)`
fn match_py_def(cs: &[char]) -> Option<(&'static str, String)> {
    let start = skip_ws(cs, 0);
    // 分支1：（async）? def
    let after_async = eat_kw(cs, start, "async").and_then(|k| scan_ws1(cs, k));
    for pos in [after_async, Some(start)] {
        if let Some(p) = pos {
            if let Some(k) = eat_kw(cs, p, "def") {
                if let Some(k) = scan_ws1(cs, k) {
                    if let Some(name) = ident_at(cs, k) {
                        return Some(("def", name));
                    }
                }
            }
        }
    }
    // 分支2：class
    if let Some(k) = eat_kw(cs, start, "class") {
        if let Some(k) = scan_ws1(cs, k) {
            if let Some(name) = ident_at(cs, k) {
                return Some(("def", name));
            }
        }
    }
    None
}

/// `^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)`——pub/async 两个独立可选组的回溯序
fn match_rs_fn(cs: &[char]) -> Option<String> {
    let start = skip_ws(cs, 0);
    let after_pub = eat_kw(cs, start, "pub").and_then(|k| scan_ws1(cs, k));
    for pub_pos in [after_pub, Some(start)] {
        let p = match pub_pos {
            Some(p) => p,
            None => continue,
        };
        let after_async = eat_kw(cs, p, "async").and_then(|k| scan_ws1(cs, k));
        for a_pos in [after_async, Some(p)] {
            if let Some(a) = a_pos {
                if let Some(k) = eat_kw(cs, a, "fn") {
                    if let Some(k) = scan_ws1(cs, k) {
                        if let Some(name) = ident_at(cs, k) {
                            return Some(name);
                        }
                    }
                }
            }
        }
    }
    None
}

/// `^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)`
fn match_rs_type(cs: &[char]) -> Option<(&'static str, String)> {
    let start = skip_ws(cs, 0);
    let after_pub = eat_kw(cs, start, "pub").and_then(|k| scan_ws1(cs, k));
    for pub_pos in [after_pub, Some(start)] {
        let p = match pub_pos {
            Some(p) => p,
            None => continue,
        };
        for kw in ["struct", "enum", "trait"] {
            if let Some(k) = eat_kw(cs, p, kw) {
                if let Some(k) = scan_ws1(cs, k) {
                    if let Some(name) = ident_at(cs, k) {
                        return Some(("type", name));
                    }
                }
            }
        }
    }
    None
}

/// `^\s*impl(?:<[^>]*>)?\s+(?:\w+\s+for\s+)?(\w+)`——A 分支（含 for）失败回退 B
fn match_rs_impl(cs: &[char]) -> Option<String> {
    let mut i = skip_ws(cs, 0);
    i = eat_kw(cs, i, "impl")?;
    if cs.get(i) == Some(&'<') {
        // (?:<[^>]*>)?：'<' 出现则必须找到 '>'，否则整组失败且 \s+ 也过不去
        let mut j = i + 1;
        while j < cs.len() && cs[j] != '>' {
            j += 1;
        }
        if j >= cs.len() {
            return None;
        }
        i = j + 1;
    }
    i = scan_ws1(cs, i)?;
    // A 分支：\w+ \s+ for \s+ \w+（\w+ 取最大值后无需内部回溯——词内无空白）
    if let Some((s, e)) = scan_ident(cs, i) {
        let _ = s;
        if let Some(j) = scan_ws1(cs, e) {
            if let Some(j) = eat_kw(cs, j, "for") {
                if let Some(j) = scan_ws1(cs, j) {
                    if let Some(name) = ident_at(cs, j) {
                        return Some(name);
                    }
                }
            }
        }
    }
    // B 分支：可选组缺席
    ident_at(cs, i)
}

/// `^func\s+(?:\([^)]*\)\s*)?(\w+)`——注意无前导 \s*（go 正则锚定行首无缩进）
fn match_go_func(cs: &[char]) -> Option<(&'static str, String)> {
    let i = eat_kw(cs, 0, "func")?;
    let i = scan_ws1(cs, i)?;
    let save = i;
    // 可选接收者 (...)
    if cs.get(i) == Some(&'(') {
        let mut j = i + 1;
        while j < cs.len() && cs[j] != ')' {
            j += 1;
        }
        if j < cs.len() {
            if let Some(j) = scan_ws1(cs, j + 1) {
                if let Some(name) = ident_at(cs, j) {
                    return Some(("fn", name));
                }
            }
        }
    }
    match ident_at(cs, save) {
        Some(name) => Some(("fn", name)),
        None => None,
    }
}

/// `^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)`
fn match_js_fn(cs: &[char]) -> Option<(&'static str, String)> {
    let start = skip_ws(cs, 0);
    let after_export = eat_kw(cs, start, "export").and_then(|k| scan_ws1(cs, k));
    for export_pos in [after_export, Some(start)] {
        let p = match export_pos {
            Some(p) => p,
            None => continue,
        };
        let after_async = eat_kw(cs, p, "async").and_then(|k| scan_ws1(cs, k));
        for a_pos in [after_async, Some(p)] {
            if let Some(a) = a_pos {
                if let Some(k) = eat_kw(cs, a, "function") {
                    if let Some(k) = scan_ws1(cs, k) {
                        if let Some(name) = ident_at(cs, k) {
                            return Some(("fn", name));
                        }
                    }
                }
            }
        }
    }
    None
}

/// `^\s*(?:export\s+)?class\s+(\w+)`
fn match_js_class(cs: &[char]) -> Option<(&'static str, String)> {
    let start = skip_ws(cs, 0);
    let after_export = eat_kw(cs, start, "export").and_then(|k| scan_ws1(cs, k));
    for export_pos in [after_export, Some(start)] {
        if let Some(p) = export_pos {
            if let Some(k) = eat_kw(cs, p, "class") {
                if let Some(k) = scan_ws1(cs, k) {
                    if let Some(name) = ident_at(cs, k) {
                        return Some(("class", name));
                    }
                }
            }
        }
    }
    None
}

/// 按 _SEM_DEF_RE 的语言与顺序取首个命中（每行最多一个定义——原实现 break）
fn extract_def(lang: &str, line: &str) -> Option<(&'static str, String)> {
    let cs: Vec<char> = line.chars().collect();
    match lang {
        "py" => match_py_def(&cs),
        "rs" => match_rs_fn(&cs)
            .map(|n| ("fn", n))
            .or_else(|| match_rs_type(&cs))
            .or_else(|| match_rs_impl(&cs).map(|n| ("impl", n))),
        "go" => match_go_func(&cs),
        "js" => match_js_fn(&cs).or_else(|| match_js_class(&cs)),
        _ => None,
    }
}

/// 定义上方紧邻注释行：`^\s*(//|#|\"\"\"|''')`
fn is_comment_line(line: &str) -> bool {
    let cs: Vec<char> = line.chars().collect();
    let i = skip_ws(&cs, 0);
    cs[i..].starts_with(&['/', '/']) || cs.get(i) == Some(&'#')
        || cs[i..].starts_with(&['"', '"', '"']) || cs[i..].starts_with(&['\'', '\'', '\''])
}

// ================= 向量与余弦 =================

#[derive(Default)]
struct SemVec {
    items: Vec<(String, f64)>, // Python dict 插入序：名称 token → trigram → 定义体 token
    map: HashMap<String, f64>,
    norm: f64,
}

fn tf_add(tf: &mut Vec<(String, i64)>, t: String, add: i64) {
    if let Some(p) = tf.iter_mut().find(|(x, _)| *x == t) {
        p.1 += add;
    } else {
        tf.push((t, add));
    }
}

fn sem_vec(text: &str, name: &str, idf: &HashMap<String, f64>) -> SemVec {
    let mut tf: Vec<(String, i64)> = Vec::new();
    for t in tokenize(name) {
        tf_add(&mut tf, t, 3);
    }
    let nc: Vec<char> = name.chars().collect();
    if nc.len() >= 3 {
        for j in 0..=(nc.len() - 3) {
            let g: String = nc[j..j + 3].iter().collect::<String>().to_lowercase();
            tf_add(&mut tf, g, 2);
        }
    }
    for t in tokenize(text) {
        tf_add(&mut tf, t, 1);
    }
    let items: Vec<(String, f64)> = tf
        .into_iter()
        .map(|(t, c)| {
            let w = (1.0 + (c as f64).ln()) * idf.get(&t).copied().unwrap_or(1.0);
            (t, w)
        })
        .collect();
    let norm = (items.iter().map(|(_, v)| v * v).sum::<f64>()).sqrt();
    let map: HashMap<String, f64> = items.iter().cloned().collect();
    SemVec { items, map, norm }
}

fn cosine(a: &SemVec, b: &SemVec) -> f64 {
    // 原实现：小 dict 作 a 做点积（浮点累加序对齐）
    let (a, b) = if b.items.len() < a.items.len() { (b, a) } else { (a, b) };
    let dot: f64 = a
        .items
        .iter()
        .map(|(t, v)| v * b.map.get(t).copied().unwrap_or(0.0))
        .sum();
    if a.norm != 0.0 && b.norm != 0.0 {
        dot / (a.norm * b.norm)
    } else {
        0.0
    }
}

fn round3(s: f64) -> f64 {
    (s * 1000.0).round() / 1000.0
}

// ================= 遍历与落地 =================

/// 遍历结构同 code_search（先文件后目录 + upcase 序）；上限：200 文件（只计
/// 扩展名命中）或 4000 定义，命中即全停（原实现 return defs）。
fn walk_sem(dir: &Path, st: &mut SemState) -> bool {
    let rd = match std::fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => return false,
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
        if st.count >= MAX_FILES || st.defs.len() >= SEM_MAX_DEFS {
            return true;
        }
        let ext = p.extension().map(|e| format!(".{}", e.to_string_lossy().to_lowercase()));
        let Some(ext) = ext else { continue };
        if !INDEX_EXTS.contains(&ext.as_str()) {
            continue;
        }
        st.count += 1;
        let lang = &ext[1..];
        let lines = match read_to_lines(&p) {
            Ok(l) => l,
            Err(_) => continue, // 读取失败：名额照烧、无定义（等价 OSError continue）
        };
        for (i, line) in lines.iter().enumerate() {
            if let Some((kind, name)) = extract_def(lang, line) {
                let mut start = i;
                while start > 0 && is_comment_line(&lines[start - 1]) {
                    start -= 1;
                }
                let body = lines[start..(i + SEM_BODY_CAP).min(lines.len())].join("\n");
                st.defs.push(Def {
                    file: p.clone(),
                    line: i + 1,
                    kind,
                    name,
                    body,
                    vec: SemVec::default(),
                });
            }
        }
    }
    for p in dirs {
        if st.count >= MAX_FILES || st.defs.len() >= SEM_MAX_DEFS {
            return true;
        }
        let name = p.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default();
        if SKIP_DIRS.contains(&name.as_str()) {
            continue;
        }
        if walk_sem(&p, st) {
            return true;
        }
    }
    false
}

/// universal newlines 归一后按 \n 切（行尾不带换行——匹配与分词都不依赖）
fn read_to_lines(p: &Path) -> Result<Vec<String>, ()> {
    let bytes = std::fs::read(p).map_err(|_| ())?;
    let text = String::from_utf8_lossy(&bytes).into_owned();
    let text = if text.contains('\r') {
        text.replace("\r\n", "\n").replace('\r', "\n")
    } else {
        text
    };
    Ok(text.split('\n').map(|s| s.to_string()).collect())
}

/// 命中行快照：读盘取定义行 strip 后前 120 字符（原实现命中时重读文件）
fn read_snippet(p: &Path, line: usize) -> String {
    match read_to_lines(p) {
        Ok(lines) => lines
            .get(line - 1)
            .map(|l| l.trim().chars().take(120).collect::<String>())
            .unwrap_or_default(),
        Err(_) => String::new(),
    }
}

fn hit_obj(d: &Def, score: f64, snippet: Option<String>) -> Value {
    let mut fields = vec![
        ("file".into(), Value::Str(d.file.to_string_lossy().into_owned())),
        ("line".into(), Value::Int(d.line as i128)),
        ("symbol".into(), Value::Str(d.name.clone())),
        ("kind".into(), Value::Str(d.kind.into())),
        ("score".into(), Value::Float(score)),
    ];
    if let Some(s) = snippet {
        fields.push(("snippet".into(), Value::Str(s)));
    }
    Value::Obj(fields)
}

fn err_obj(msg: &str) -> Value {
    Value::Obj(vec![("error".into(), Value::Str(msg.into()))])
}
