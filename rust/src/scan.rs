//! scan 域轻正则三工具的 Rust 原生实现（S82）：std_check / ui_check / bug_locate。
//!
//! 语义与 tools/scan.py 旧实现逐字段对齐（S82 双实现对照实验定案）：
//! - 遍历（_iter_files 契约）：单文件直接收录；目录每层先收本目录代码文件再
//!   下钻子目录，目录内按 NTFS upcase 排序（os.scandir 语义，S80 实锤同款）；
//!   符号链接解引用定类（目录符号链接不下钻，断链跳过）；跳过 12 个目录；
//!   名额只计代码文件（非代码文件不烧额度），满额即整体停走。
//! - 无沙盒门：与 Python 版一致（纯读分析，S75 审计定性=本职）。
//! - 正则全部手写移植（红线：无 regex crate）：备选顺序/贪婪回溯/懒惰扩张
//!   逐条复刻，含已知怪癖——文件名兜底把 foo.tsx 捕获成 foo.ts（备选里
//!   ts 先于 tsx 且命中即止）。
//! - 输出键序与 Python dict 一致；不做任何排序（顺序即遍历序）。

use crate::json::Value;
use std::collections::HashMap;
use std::collections::HashSet;
use std::path::Path;

pub const MAX_FILES: usize = 100;

const SKIP_DIRS: [&str; 12] = [
    ".git", "node_modules", "target", "__pycache__", "dist", "build",
    ".codegraph", "backups", "assets", "screenshots", "images", "fonts",
];

/// 21 种扩展名 → 语言（scan._LANG_BY_EXT）。
fn lang_of_ext(ext: &str) -> &'static str {
    match ext {
        ".py" => "python",
        ".rs" => "rust",
        ".go" => "go",
        ".ts" => "typescript",
        ".tsx" => "typescript",
        ".js" => "javascript",
        ".jsx" => "javascript",
        ".gd" => "gdscript",
        ".c" => "c",
        ".cpp" => "cpp",
        ".h" => "c",
        ".hpp" => "cpp",
        ".cs" => "csharp",
        ".dart" => "dart",
        ".lua" => "lua",
        ".sh" => "bash",
        ".java" => "java",
        ".kt" => "kotlin",
        ".php" => "php",
        ".rb" => "ruby",
        ".swift" => "swift",
        _ => "",
    }
}

/// os.path.splitext 等价：取最后一段路径分隔符之后的最后一个 '.'（点开头的
/// 文件名视为无扩展名）。Python 版对完整路径调用——分隔符后的点才算数。
fn splitext(name: &str) -> &str {
    let start = name.rfind(['/', '\\']).map(|i| i + 1).unwrap_or(0);
    let b = name.as_bytes();
    let mut dot = None;
    for (i, &c) in b.iter().enumerate().skip(start) {
        if c == b'.' {
            dot = Some(i);
        }
    }
    match dot {
        Some(i) if i > start => &name[i..],
        _ => "",
    }
}

pub(crate) fn lang_of(path: &str) -> &'static str {
    lang_of_ext(&splitext(path).to_lowercase())
}

// ---------- 遍历 ----------

struct Walk {
    out: Vec<String>,
    count: usize,
    max: usize,
}

fn walk_dir(dir: &Path, st: &mut Walk) {
    let rd = match std::fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => return,
    };
    let mut files: Vec<String> = Vec::new();
    let mut dirs: Vec<String> = Vec::new();
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
        if st.count >= st.max {
            return;
        }
        // 只对代码文件计数（非代码文件直接跳过，不占额度）
        if lang_of(&name).is_empty() {
            continue;
        }
        st.out.push(join_name(dir, &name));
        st.count += 1;
    }
    for name in dirs {
        if SKIP_DIRS.contains(&name.as_str()) {
            continue;
        }
        if st.count >= st.max {
            return;
        }
        walk_dir(&dir.join(&name), st);
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

/// _iter_files 等价：目录或单文件；max_files 只计代码文件。
pub(crate) fn iter_files(path: &str, max_files: usize) -> Vec<String> {
    let p = Path::new(path);
    if p.is_file() {
        return vec![path.to_string()];
    }
    if !p.is_dir() {
        return vec![];
    }
    let mut st = Walk { out: Vec::new(), count: 0, max: max_files };
    walk_dir(p, &mut st);
    st.out
}

/// utf-8 errors=replace 等价读全文。
pub(crate) fn read_text(p: &Path) -> Option<String> {
    let bytes = std::fs::read(p).ok()?;
    Some(String::from_utf8_lossy(&bytes).into_owned())
}

/// readlines() 等价：universal newlines 归一后的行列表（行尾不带换行）。
/// 与 read_to_lines 的差别：空文件没有行；末尾换行不产生幻影空行
/// （find_in_file 的空 needle 会命中所有行，幻影行会凭空多报）。
fn read_lines(p: &Path) -> Option<Vec<String>> {
    let text = read_text(p)?;
    if text.is_empty() {
        return Some(Vec::new());
    }
    let text = if text.contains('\r') {
        text.replace("\r\n", "\n").replace('\r', "\n")
    } else {
        text
    };
    let mut lines: Vec<String> = text.split('\n').map(|s| s.to_string()).collect();
    if text.ends_with('\n') {
        lines.pop();
    }
    Some(lines)
}

fn err_obj(msg: &str) -> Value {
    Value::Obj(vec![("error".into(), Value::Str(msg.into()))])
}

fn line_no(src: &str, pos: usize) -> i128 {
    src[..pos].bytes().filter(|&b| b == b'\n').count() as i128 + 1
}

fn skip_ws(s: &str, mut i: usize) -> usize {
    while let Some(c) = s[i..].chars().next() {
        if c.is_whitespace() {
            i += c.len_utf8();
        } else {
            break;
        }
    }
    i
}

/// Python \w 的近似（本仓既定约定）：字母/数字/下划线（Unicode 口径）。
fn is_word_char_at(s: &str, i: usize) -> bool {
    s[i..].chars().next().map_or(false, |c| c.is_alphanumeric() || c == '_')
}

// ---------- std_check ----------

const PLACEHOLDER_WORDS: [&str; 12] = [
    "TODO", "FIXME", "placeholder", "占位", "待实现", "未实现",
    "lorem", "example.com", "your_name", "xxx", "foo", "bar",
];

const MAGIC_LANGS: [&str; 6] = ["rust", "python", "go", "typescript", "javascript", "gdscript"];

pub fn std_check(path: &str, max_files: usize) -> Value {
    if !Path::new(path).exists() {
        return err_obj(&format!("路径不存在: {}", path));
    }
    let mut findings: Vec<Value> = Vec::new();
    let mut files_scanned = 0i128;
    for fp in iter_files(path, max_files) {
        let lang = lang_of(&fp);
        if lang.is_empty() {
            continue;
        }
        files_scanned += 1;
        let Some(src) = read_text(Path::new(&fp)) else { continue };
        std_check_file(&src, &fp, lang, &mut findings);
    }
    Value::Obj(vec![
        ("files".into(), Value::Int(files_scanned)),
        ("total".into(), Value::Int(findings.len() as i128)),
        ("findings".into(), Value::Arr(findings)),
    ])
}

fn is_comment_prefix(line: &str) -> bool {
    let t = line.trim();
    t.starts_with('#') || t.starts_with("//") || t.starts_with("/*") || t.starts_with('*')
}

fn strip80(line: &str) -> String {
    line.trim().chars().take(80).collect()
}

fn std_check_file(src: &str, fp: &str, lang: &str, out: &mut Vec<Value>) {
    for (idx0, line) in src.split('\n').enumerate() {
        let idx = idx0 as i128 + 1;
        let low = line.to_lowercase();
        for w in PLACEHOLDER_WORDS {
            if low.contains(&w.to_lowercase()) && !is_comment_prefix(line) {
                out.push(Value::Obj(vec![
                    ("file".into(), Value::Str(fp.into())),
                    ("line".into(), Value::Int(idx)),
                    ("rule".into(), Value::Str("placeholder".into())),
                    ("msg".into(), Value::Str(format!("占位/假数据文字: {}", w))),
                    ("text".into(), Value::Str(strip80(line))),
                ]));
                break;
            }
        }
        if MAGIC_LANGS.contains(&lang) {
            if let Some(num) = magic_number(line) {
                out.push(Value::Obj(vec![
                    ("file".into(), Value::Str(fp.into())),
                    ("line".into(), Value::Int(idx)),
                    ("rule".into(), Value::Str("magic_number".into())),
                    ("msg".into(), Value::Str(format!("魔法数字: {}", num))),
                    ("text".into(), Value::Str(strip80(line))),
                ]));
            }
        }
    }
}

/// `=\s*(-?\d{3,}|[2-9]\d{2,})\b` 手写移植：逐个 '=' 位置（左移优先，前一个
/// '=' 匹配失败引擎才右移）；\s* 后先试 -?\d{3,}（贪婪 + \b 回溯收缩），整支
/// 落空才试 [2-9]\d{2,}。\b 按 Python Unicode \w 口径判定。
fn magic_number(line: &str) -> Option<String> {
    let cs: Vec<char> = line.chars().collect();
    for i in 0..cs.len() {
        if cs[i] != '=' {
            continue;
        }
        let j = skip_ws_char(&cs, i + 1);
        if let Some(g) = digit_run(&cs, j, true) {
            return Some(g);
        }
        if let Some(g) = digit_run(&cs, j, false) {
            return Some(g);
        }
    }
    None
}

fn skip_ws_char(cs: &[char], mut i: usize) -> usize {
    while i < cs.len() && cs[i].is_whitespace() {
        i += 1;
    }
    i
}

/// 从 start 起匹配 `-?\d{3,}`（allow_minus）或 `[2-9]\d{2,}`，\b 边界处
/// 贪婪回溯收缩（最少 3 位）。返回捕获组文本。
fn digit_run(cs: &[char], start: usize, allow_minus: bool) -> Option<String> {
    let mut k = start;
    if allow_minus && k < cs.len() && cs[k] == '-' {
        k += 1;
    }
    let ds = k;
    while k < cs.len() && cs[k].is_ascii_digit() {
        k += 1;
    }
    let nd = k - ds;
    if nd < 3 {
        return None;
    }
    if !allow_minus && !('2'..='9').contains(&cs[ds]) {
        return None;
    }
    let mut cut = nd;
    loop {
        let boundary = match cs.get(ds + cut) {
            None => true,
            Some(&c) => !(c.is_alphanumeric() || c == '_'),
        };
        if boundary {
            return Some(cs[start..ds + cut].iter().collect());
        }
        if cut <= 3 {
            return None;
        }
        cut -= 1;
    }
}

// ---------- ui_check ----------

pub fn ui_check(path: &str, max_files: usize) -> Value {
    if !Path::new(path).exists() {
        return err_obj(&format!("路径不存在: {}", path));
    }
    let mut issues: Vec<Value> = Vec::new();
    let mut files_scanned = 0i128;
    for fp in iter_files(path, max_files) {
        let ext = splitext(&fp).to_lowercase();
        let engine = match ext.as_str() {
            ".rs" => "bevy",
            ".gd" => "godot",
            ".cs" => "unity",
            _ => continue,
        };
        files_scanned += 1;
        let Some(src) = read_text(Path::new(&fp)) else { continue };
        match engine {
            "bevy" => {
                // BEVY_UI_PATTERNS 三连（顺序：with_children / TextBundle / TextStyle）
                for ln in find_with_children(&src) {
                    push_issue(&mut issues, &fp, ln, "空 with_children()——无子节点（UI 无效）", engine);
                }
                for ln in find_ident_brace(&src, "TextBundle") {
                    push_issue(&mut issues, &fp, ln, "旧式 TextBundle——Bevy 0.15+ 推荐 Text::new（API 迁移）", engine);
                }
                for ln in find_ident_brace(&src, "TextStyle") {
                    push_issue(&mut issues, &fp, ln, "TextStyle 手动构建——Bevy 0.15+ 推荐 TextFont/TextColor 组件", engine);
                }
                // S6：死按钮用结构化检测（Marker-Query 跨 system 验证，非同域正则）
                dead_buttons(&src, &fp, &mut issues);
            }
            "godot" => {
                for ln in find_godot_button(&src) {
                    push_issue(&mut issues, &fp, ln, "Button 信号未连接（疑似死按钮）", engine);
                }
            }
            "unity" => {
                for ln in find_unity_new_button(&src) {
                    push_issue(&mut issues, &fp, ln, "运行时 new Button（应引用场景中的实例）", engine);
                }
            }
            _ => {}
        }
    }
    Value::Obj(vec![
        ("files".into(), Value::Int(files_scanned)),
        ("total".into(), Value::Int(issues.len() as i128)),
        ("issues".into(), Value::Arr(issues)),
    ])
}

fn push_issue(out: &mut Vec<Value>, fp: &str, line: i128, msg: &str, engine: &str) {
    out.push(Value::Obj(vec![
        ("file".into(), Value::Str(fp.into())),
        ("line".into(), Value::Int(line)),
        ("rule".into(), Value::Str("ui_pattern".into())),
        ("msg".into(), Value::Str(msg.into())),
        ("engine".into(), Value::Str(engine.into())),
    ]));
}

/// `\.with_children\(\s*\)` —— 括号间只允许空白。
fn find_with_children(src: &str) -> Vec<i128> {
    const HEAD: &str = ".with_children(";
    let mut out = Vec::new();
    let mut i = 0usize;
    while let Some(rel) = src[i..].find(HEAD) {
        let occ = i + rel;
        let j = skip_ws(src, occ + HEAD.len());
        if src[j..].starts_with(')') {
            out.push(line_no(src, occ));
            i = j + 1;
        } else {
            i = occ + 1;
        }
    }
    out
}

/// `TextBundle\s*\{` / `TextStyle\s*\{`（\s* 跨行）。
fn find_ident_brace(src: &str, ident: &str) -> Vec<i128> {
    let mut out = Vec::new();
    let mut i = 0usize;
    while let Some(rel) = src[i..].find(ident) {
        let occ = i + rel;
        let j = skip_ws(src, occ + ident.len());
        if src[j..].starts_with('{') {
            out.push(line_no(src, occ));
            i = j + 1;
        } else {
            i = occ + 1;
        }
    }
    out
}

/// `Button\b[^:]*:\s*$`（re.MULTILINE）手写移植：无左边界（MyButton foo: 也
/// 命中）；[^:]* 跨行吞到首个 ':'；`$` 语义 = ':' 后的空白串里含 '\n'
/// （$ 落在换行前）或空白串直达文尾。
fn find_godot_button(src: &str) -> Vec<i128> {
    let b = src.as_bytes();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i + 6 <= b.len() {
        if &b[i..i + 6] == b"Button" {
            let boundary = i + 6 == b.len() || !is_word_char_at(src, i + 6);
            if boundary {
                if let Some(cr) = src[i + 6..].find(':') {
                    let colon = i + 6 + cr;
                    let k = skip_ws(src, colon + 1);
                    let reaches_eof = k == b.len();
                    let run_has_nl = src[colon + 1..k].contains('\n');
                    if reaches_eof || run_has_nl {
                        out.push(line_no(src, i));
                        // 匹配结束于 $ 位置（换行处或文尾），从那里续扫
                        i = if reaches_eof {
                            b.len()
                        } else {
                            colon + 1 + src[colon + 1..k].find('\n').unwrap()
                        };
                        continue;
                    }
                }
            }
        }
        i += 1;
    }
    out
}

/// `new\s+Button\s*\([^)]*\)` 手写移植：无任何边界（renew Button( 也命中）；
/// \s+ 跨行；[^)]* 可跨行但止于首个 ')'。
fn find_unity_new_button(src: &str) -> Vec<i128> {
    let b = src.as_bytes();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i + 3 <= b.len() {
        if &b[i..i + 3] == b"new" {
            let mut j = skip_ws(src, i + 3);
            if j > i + 3 && src[j..].starts_with("Button") {
                j = skip_ws(src, j + 6);
                if let Some(or_) = src[j..].find('(') {
                    let open = j + or_;
                    if let Some(cr) = src[open + 1..].find(')') {
                        out.push(line_no(src, i));
                        i = open + 1 + cr + 1;
                        continue;
                    }
                }
            }
        }
        i += 1;
    }
    out
}

/// bevy.find_dead_buttons 手写移植（S6 结构化语义）：spawn((Button, Marker, ...))
/// 后全文找不到 With<Marker> 或 &Marker…Interaction 同现才算死。
fn dead_buttons(src: &str, fp: &str, out: &mut Vec<Value>) {
    let lines: Vec<&str> = src.split('\n').collect();
    for (i, line) in lines.iter().enumerate() {
        // 门：正则 \(?(Button,\s*(?:[A-Z]...[\{,])?) 的最小匹配 ≡ 含 "Button,"
        if !line.contains("Button,") {
            continue;
        }
        let stripped = line.trim();
        let is_lone = stripped == "Button," || stripped.ends_with("(Button,");
        let mut marker: Option<String> = None;
        if is_lone {
            // 只向下看最多 2 行；撞到 ")" 或 "//" 注释即止
            let hi = (i + 3).min(lines.len());
            for j in (i + 1)..hi {
                if let Some(m) = match_marker_line(lines[j]) {
                    if m != "Button" {
                        marker = Some(m);
                        break;
                    }
                }
                let st = lines[j].trim_start();
                if st.starts_with(')') || st.starts_with("//") {
                    break;
                }
            }
        } else if let Some(m) = button_comma_marker(line) {
            if m != "Node" {
                marker = Some(m);
            }
        }
        let Some(marker) = marker else { continue };
        if src.contains(&format!("With<{}>", marker)) || marker_interaction_ref(src, &marker) {
            continue;
        }
        out.push(Value::Obj(vec![
            ("file".into(), Value::Str(fp.into())),
            ("line".into(), Value::Int(i as i128 + 1)),
            ("rule".into(), Value::Str("ui_pattern".into())),
            ("msg".into(), Value::Str(format!("死按钮：{} spawn 后无任何 Query 交互处理", marker))),
            ("engine".into(), Value::Str("bevy".into())),
        ]));
    }
}

/// re.match(r"\s*([A-Z][A-Za-z0-9_]+)\s*[\{,]", s) —— 锚定行首。
fn match_marker_line(s: &str) -> Option<String> {
    let cs: Vec<char> = s.chars().collect();
    let mut i = 0usize;
    while i < cs.len() && cs[i].is_whitespace() {
        i += 1;
    }
    if i >= cs.len() || !cs[i].is_ascii_uppercase() {
        return None;
    }
    let start = i;
    i += 1;
    while i < cs.len() && (cs[i].is_ascii_alphanumeric() || cs[i] == '_') {
        i += 1;
    }
    let end = i;
    while i < cs.len() && cs[i].is_whitespace() {
        i += 1;
    }
    if i < cs.len() && (cs[i] == '{' || cs[i] == ',') {
        Some(cs[start..end].iter().collect())
    } else {
        None
    }
}

/// re.search(r"Button,\s*([A-Z][A-Za-z0-9_]*)", line) —— 逐个 "Button," 出现处
/// 尝试（前一处后跟非大写字母时引擎右移继续找）。
fn button_comma_marker(line: &str) -> Option<String> {
    let mut from = 0usize;
    while let Some(rel) = line[from..].find("Button,") {
        let j = skip_ws(line, from + rel + 7);
        let cs: Vec<char> = line[j..].chars().collect();
        if let Some(&c0) = cs.first() {
            if c0.is_ascii_uppercase() {
                let mut n = 1usize;
                while n < cs.len() && (cs[n].is_ascii_alphanumeric() || cs[n] == '_') {
                    n += 1;
                }
                return Some(cs[..n].iter().collect());
            }
        }
        from += rel + 7;
    }
    None
}

/// `&Marker[^\n]{0,80}Interaction|Interaction[^\n]{0,80}&Marker` 存在性判定。
/// re.search 的存在性 = 两支各自存在即可，与先后无关；缺口不得跨行且 ≤80 字符。
fn marker_interaction_ref(src: &str, marker: &str) -> bool {
    let pat = format!("&{}", marker);
    let mut from = 0usize;
    while let Some(rel) = src[from..].find(&pat) {
        let end = from + rel + pat.len();
        let line_end = src[end..].find('\n').map_or(src.len(), |r| end + r);
        let lim = line_end.min(end + 80);
        if src[end..lim].contains("Interaction") {
            return true;
        }
        from = end;
    }
    let mut from = 0usize;
    while let Some(rel) = src[from..].find("Interaction") {
        let end = from + rel + "Interaction".len();
        let line_end = src[end..].find('\n').map_or(src.len(), |r| end + r);
        let lim = line_end.min(end + 80);
        if src[end..lim].contains(&pat) {
            return true;
        }
        from = end;
    }
    false
}

// ---------- bug_locate ----------

/// 文件名/traceback 两处共用的扩展名备选，顺序即 Python 正则备选顺序
/// （ts 先于 tsx、js 先于 jsx——命中即止，foo.tsx 捕获成 foo.ts 的怪癖源头）。
const FILE_EXTS: [&str; 13] = [
    "py", "rs", "go", "ts", "js", "tsx", "jsx", "gd", "cs", "java", "kt", "rb", "php",
];

const SYMBOL_KWS: [&str; 5] = ["NameError", "AttributeError", "KeyError", "ImportError", "Error"];

pub fn bug_locate(root: &str, error_text: &str) -> Value {
    if !Path::new(root).is_dir() {
        return err_obj(&format!("root 不是目录: {}", root));
    }
    let mut candidates: Vec<(String, Option<i64>, &'static str)> = Vec::new();
    for (f, l) in extract_traceback(error_text) {
        candidates.push((f, Some(l), "traceback 精确"));
    }
    if candidates.is_empty() {
        for f in extract_filenames(error_text) {
            candidates.push((f, None, "文件名"));
        }
    }
    if candidates.is_empty() {
        for s in extract_symbols(error_text) {
            candidates.push((s, None, "符号"));
        }
    }
    let files = iter_files(root, MAX_FILES);
    // 同一文件可能被多个候选反复读取——读一次缓存（Python 每次重读，结果同）
    let mut cache: HashMap<String, Option<Vec<String>>> = HashMap::new();
    let mut direct: Vec<Value> = Vec::new();
    for (c, lineno, how) in candidates {
        let is_code = [".py", ".rs", ".go", ".ts", ".js", ".gd", ".cs", ".java", ".kt", ".rb", ".php"]
            .iter().any(|e| c.ends_with(e));
        if is_code {
            // 候选名由标识符字符组成（无分隔符）→ 恒非绝对路径，直接 endswith 搜
            let mut fpath: Option<String> = None;
            for fp in &files {
                if fp.ends_with(&c) {
                    fpath = Some(fp.clone());
                    break;
                }
            }
            if let Some(fpath) = fpath {
                if Path::new(&fpath).exists() {
                    match lineno {
                        Some(ln) => {
                            let snippet = file_ctx_line(&fpath, ln, &mut cache);
                            direct.push(Value::Obj(vec![
                                ("file".into(), Value::Str(fpath)),
                                ("line".into(), Value::Int(ln as i128)),
                                ("how".into(), Value::Str(how.into())),
                                ("snippet".into(), Value::Str(snippet)),
                            ]));
                        }
                        None => {
                            let hits = file_find_empty(&fpath, &mut cache);
                            direct.extend(hits);
                            // Python 怪癖：extend 空列表也会执行 direct[-1]["how"] = how
                            if let Some(Value::Obj(fields)) = direct.last_mut() {
                                fields.push(("how".into(), Value::Str(how.into())));
                            }
                        }
                    }
                }
            }
        } else {
            for fp in &files {
                let lines = load_lines(fp, &mut cache);
                let Some(ls) = lines else { continue };
                let mut hits = find_in_file(fp, ls, &c, 2);
                for h in hits.iter_mut() {
                    if let Value::Obj(fields) = h {
                        fields.push(("how".into(), Value::Str(format!("符号 '{}'", c))));
                    }
                }
                direct.extend(hits);
            }
        }
    }
    let mut seen: HashSet<(String, i64)> = HashSet::new();
    let mut out: Vec<Value> = Vec::new();
    for h in direct {
        let f = h.get("file").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let l = match h.get("line") {
            Some(Value::Int(n)) => *n as i64,
            _ => -1,
        };
        let key = (f, l);
        if seen.contains(&key) {
            continue;
        }
        seen.insert(key);
        out.push(h);
        if out.len() >= 10 {
            break;
        }
    }
    Value::Obj(vec![
        ("candidates".into(), Value::Int(out.len() as i128)),
        ("hits".into(), Value::Arr(out)),
    ])
}

fn load_lines<'a>(fp: &str, cache: &'a mut HashMap<String, Option<Vec<String>>>) -> Option<&'a Vec<String>> {
    cache.entry(fp.to_string()).or_insert_with(|| read_lines(Path::new(fp))).as_ref()
}

/// _line_ctx（radius=2）：start = max(0, lineno-3)、end = min(len, lineno+2)。
fn file_ctx_line(fp: &str, ln: i64, cache: &mut HashMap<String, Option<Vec<String>>>) -> String {
    match load_lines(fp, cache) {
        Some(lines) => {
            let n = lines.len() as i64;
            ctx_snippet(lines, (ln - 3).max(0), n.min(ln + 2))
        }
        None => String::new(),
    }
}

/// _find_in_file(fpath, "")：空 needle 命中所有行，最多 3 条。
fn file_find_empty(fp: &str, cache: &mut HashMap<String, Option<Vec<String>>>) -> Vec<Value> {
    match load_lines(fp, cache) {
        Some(lines) => find_in_file(fp, lines, "", 3),
        None => Vec::new(),
    }
}

/// 上下文窗切片（Python 行切片语义：start 闭 / end 开），join 后 strip。
fn ctx_snippet(lines: &[String], start: i64, end: i64) -> String {
    if start >= end {
        return String::new();
    }
    let s = start.max(0) as usize;
    let e = (end as usize).min(lines.len());
    if s >= e {
        return String::new();
    }
    lines[s..e].join("\n").trim().to_string()
}

/// _find_in_file：命中窗口 = [max(0, idx-2), min(len, idx+3))（idx 为 1 基行号）。
fn find_in_file(fp: &str, lines: &[String], needle: &str, max_hits: usize) -> Vec<Value> {
    let mut hits = Vec::new();
    let n = lines.len() as i64;
    for (i0, line) in lines.iter().enumerate() {
        let idx = i0 as i64 + 1;
        if line.contains(needle) {
            let snippet = ctx_snippet(lines, (idx - 2).max(0), n.min(idx + 3));
            hits.push(Value::Obj(vec![
                ("file".into(), Value::Str(fp.into())),
                ("line".into(), Value::Int(idx as i128)),
                ("snippet".into(), Value::Str(snippet)),
            ]));
            if hits.len() >= max_hits {
                break;
            }
        }
    }
    hits
}

/// `File\s+"([^"]+\.(?:py|rs|...))",\s*line\s+(\d+)` 手写移植。
/// ext 结尾必须紧贴闭引号 → 后缀完全相等判定（ends_with 等价，不受备选顺序影响）。
fn extract_traceback(text: &str) -> Vec<(String, i64)> {
    let b = text.as_bytes();
    let mut out = Vec::new();
    let mut from = 0usize;
    while let Some(rel) = text[from..].find("File") {
        let i = from + rel;
        from = i + 4;
        let mut j = skip_ws(text, i + 4);
        if j == i + 4 {
            continue; // \s+ 至少一个
        }
        if !text[j..].starts_with('"') {
            continue;
        }
        j += 1;
        let Some(q) = text[j..].find('"') else { continue };
        let inner = &text[j..j + q];
        if inner.is_empty() {
            continue; // [^"]+ 至少一个
        }
        if !FILE_EXTS.iter().any(|e| inner.ends_with(&format!(".{}", e))) {
            continue;
        }
        let mut k = j + q + 1;
        if !text[k..].starts_with(',') {
            continue;
        }
        k = skip_ws(text, k + 1);
        if !text[k..].starts_with("line") {
            continue;
        }
        k += 4;
        let k2 = skip_ws(text, k);
        if k2 == k {
            continue; // \s+
        }
        k = k2;
        let d0 = k;
        while k < b.len() && b[k].is_ascii_digit() {
            k += 1;
        }
        if k == d0 {
            continue;
        }
        let n: i64 = text[d0..k].parse().unwrap_or(i64::MAX);
        out.push((inner.to_string(), n));
        from = k;
    }
    out
}

/// `([A-Za-z_][A-Za-z0-9_.]*\.(?:py|rs|...))` 手写移植：贪婪回溯从右往左找 '.'
/// 拆分点；拆分点确定后扩展名备选按原序首个前缀命中即收（ts 先于 tsx →
/// foo.tsx 捕获成 foo.ts，怪癖保真）。运行段内所有拆分点都试败时，段内
/// 更晚起点只是同批拆分点的子集，不可能命中 → 整段跳过。
fn extract_filenames(text: &str) -> Vec<String> {
    let b = text.as_bytes();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < b.len() {
        let c = b[i];
        if !(c.is_ascii_alphabetic() || c == b'_') {
            i += 1;
            continue;
        }
        let mut e = i;
        while e < b.len() && (b[e].is_ascii_alphanumeric() || b[e] == b'_' || b[e] == b'.') {
            e += 1;
        }
        let run = &text[i..e];
        let mut hit: Option<usize> = None;
        for d in (0..run.len()).rev() {
            if run.as_bytes()[d] != b'.' {
                continue;
            }
            let suffix = &run[d + 1..];
            for ext in FILE_EXTS {
                if suffix.starts_with(ext) {
                    hit = Some(d + 1 + ext.len());
                    break;
                }
            }
            if hit.is_some() {
                break;
            }
        }
        match hit {
            Some(me) => {
                out.push(run[..me].to_string());
                i += me; // finditer 从匹配结束续扫
            }
            None => i = e,
        }
    }
    out
}

/// `(?:NameError|AttributeError|KeyError|ImportError|Error).*?['\"]([^'\"]+)['\"]`
/// 手写移植：.*? 不跨行（开引号必须与本关键词同行）；[^'\"]+ 可跨行（闭引号
/// 可在后续行）；开引号逐个回退——前一个开引号找不到闭引号时，懒惰扩张把
/// 下一个引号当开引号再试。
fn extract_symbols(text: &str) -> Vec<String> {
    let b = text.as_bytes();
    let mut out = Vec::new();
    let mut pos = 0usize;
    'scan: while pos < b.len() {
        let mut kw_end = None;
        for kw in SYMBOL_KWS {
            if text[pos..].starts_with(kw) {
                kw_end = Some(pos + kw.len());
                break;
            }
        }
        let Some(ke) = kw_end else {
            pos += text[pos..].chars().next().map_or(1, |c| c.len_utf8());
            continue;
        };
        let line_end = text[ke..].find('\n').map_or(b.len(), |r| ke + r);
        let mut q = ke;
        while q < line_end {
            let c = b[q];
            if c == b'\'' || c == b'"' {
                // 闭引号 = ≥1 个非引号字符后的首个引号（[^'\"]+ 可跨行）
                if q + 1 < b.len() && b[q + 1] != b'\'' && b[q + 1] != b'"' {
                    if let Some(cr) = text[q + 1..].find(['\'', '"']) {
                        out.push(text[q + 1..q + 1 + cr].to_string());
                        pos = q + 1 + cr + 1;
                        continue 'scan;
                    }
                }
            }
            q += 1;
        }
        // 本关键词位置无符号命中 → 引擎前移一位（NameError 内部的 Error 还有机会）
        pos += text[pos..].chars().next().map_or(1, |c| c.len_utf8());
    }
    out
}
