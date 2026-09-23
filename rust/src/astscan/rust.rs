//! astscan 子模块（S168 从 astscan.rs 拆出；纯搬移，未改语义）。
use super::*;

pub(crate) fn mask_rust(src: &str) -> (Vec<char>, usize, usize) {
    let cs: Vec<char> = src.chars().collect();
    let n = cs.len();
    let mut out = cs.clone();
    let (mut strings, mut comments) = (0usize, 0usize);
    let mut i = 0usize;
    while i < n {
        let c = cs[i];
        let nxt = if i + 1 < n { cs[i + 1] } else { '\0' };
        if c == '/' && nxt == '/' {
            let mut j = i;
            while j < n && cs[j] != '\n' {
                j += 1;
            }
            for k in out.iter_mut().take(j).skip(i) {
                *k = ' ';
            }
            comments += 1;
            i = j;
        } else if c == '/' && nxt == '*' {
            let mut j = i + 2;
            let mut e = n;
            while j + 1 < n {
                if cs[j] == '*' && cs[j + 1] == '/' {
                    e = j + 2;
                    break;
                }
                j += 1;
            }
            for k in out.iter_mut().take(e).skip(i) {
                *k = ' ';
            }
            comments += 1;
            i = e;
        } else if c == 'r' && (nxt == '"' || nxt == '#') {
            // 原始字符串 r"..." / r#"..."#（简易：# 计数）。旧 Python 在 r# 后
            // 非引号（原始标识符 r#type）会死循环——这里 i += 1 继续扫（S84 修复）
            let mut j = i + 1;
            let mut hashes = 0usize;
            while j < n && cs[j] == '#' {
                hashes += 1;
                j += 1;
            }
            if j < n && cs[j] == '"' {
                j += 1;
                while j < n {
                    if cs[j] == '"' {
                        let k = j + 1;
                        if (0..hashes).all(|h| k + h < n && cs[k + h] == '#') {
                            break;
                        }
                    }
                    j += 1;
                }
                let e = (j + 1).min(n);
                for k in out.iter_mut().take(e).skip(i) {
                    *k = ' ';
                }
                strings += 1;
                i = e;
                continue;
            }
            i += 1;
        } else if c == '"' {
            let mut j = i + 1;
            while j < n {
                if cs[j] == '\\' {
                    j += 2;
                    continue;
                }
                if cs[j] == c || cs[j] == '\n' {
                    break;
                }
                j += 1;
            }
            let e = (j + 1).min(n);
            for k in out.iter_mut().take(e).skip(i) {
                *k = ' ';
            }
            strings += 1;
            i = e;
        } else if c == '\'' {
            // char 字面量（短闭合）——生命周期 'a 无紧跟闭合引号则放过
            let mut j = i + 1;
            let mut closed = false;
            while j < n && j < i + 5 {
                if cs[j] == '\'' {
                    closed = true;
                    break;
                }
                if cs[j] == '\\' {
                    j += 2;
                    continue;
                }
                j += 1;
            }
            if closed {
                let e = j + 1;
                for k in out.iter_mut().take(e).skip(i) {
                    *k = ' ';
                }
                strings += 1;
                i = e;
            } else {
                i += 1;
            }
        } else {
            i += 1;
        }
    }
    (out, strings, comments)
}

pub(crate) fn is_rust_word(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

pub(crate) const PANIC_NAMES: [&str; 6] =
    ["unwrap", "expect", "panic!", "unreachable!", "todo!", "unimplemented!"];

pub(crate) fn mod_test_attr_search(s: &str) -> bool {
    // #\[cfg\s*\(\s*test\s*\)\s*\]
    let cs: Vec<char> = s.chars().collect();
    let n = cs.len();
    let mut i = 0usize;
    while i + 5 <= n {
        if cs[i] == '#' && cs[i + 1] == '[' && cs[i + 2] == 'c' && cs[i + 3] == 'f'
            && cs[i + 4] == 'g'
        {
            let mut j = i + 5;
            while j < n && is_js_space(cs[j]) {
                j += 1;
            }
            if j < n && cs[j] == '(' {
                j += 1;
                while j < n && is_js_space(cs[j]) {
                    j += 1;
                }
                if cs[j..].starts_with(&['t', 'e', 's', 't']) {
                    j += 4;
                    while j < n && is_js_space(cs[j]) {
                        j += 1;
                    }
                    if j < n && cs[j] == ')' {
                        j += 1;
                        while j < n && is_js_space(cs[j]) {
                            j += 1;
                        }
                        if j < n && cs[j] == ']' {
                            return true;
                        }
                    }
                }
            }
        }
        i += 1;
    }
    false
}

/// \bfn\s+(ident) 的首个匹配（Python \s 口径）。
pub(crate) fn fn_re_search(ln: &str) -> Option<String> {
    let cs: Vec<char> = ln.chars().collect();
    let n = cs.len();
    let mut i = 0usize;
    while i + 2 <= n {
        if cs[i] == 'f' && cs[i + 1] == 'n' && (i == 0 || !is_py_word(cs[i - 1])) {
            let mut j = i + 2;
            if j >= n || !is_js_space(cs[j]) {
                i += 1;
                continue;
            }
            while j < n && is_js_space(cs[j]) {
                j += 1;
            }
            if j < n && (cs[j].is_ascii_alphabetic() || cs[j] == '_') {
                let mut e = j + 1;
                while e < n && is_rust_word(cs[e]) {
                    e += 1;
                }
                return Some(cs[j..e].iter().collect());
            }
        }
        i += 1;
    }
    None
}

/// \bunsafe\b 的全部命中数（\b 用 Python \w 口径）。
pub(crate) fn unsafe_count_line(ln: &str) -> usize {
    let cs: Vec<char> = ln.chars().collect();
    let n = cs.len();
    let mut cnt = 0usize;
    let mut i = 0usize;
    while i + 6 <= n {
        if cs[i] == 'u'
            && cs[i + 1] == 'n'
            && cs[i + 2] == 's'
            && cs[i + 3] == 'a'
            && cs[i + 4] == 'f'
            && cs[i + 5] == 'e'
            && (i == 0 || !is_py_word(cs[i - 1]))
            && (i + 6 >= n || !is_py_word(cs[i + 6]))
        {
            cnt += 1;
            i += 6;
        } else {
            i += 1;
        }
    }
    cnt
}

/// _PANIC_CALL_RE：\b(?:\.\s*)?(名字表)\s*[(!] —— 点形优先（greedy ?），
/// 名字表按 alternation 序回溯。返回 (名字, group0) 列表。
/// \b 的两种成立语境（实测 CPython 语义）：
/// - 名字形：位置 i 是名字首字符（ASCII 字母）且前一字符非词字符；
///   `x .unwrap()` 的 'u'（前是 '.'）也成立，group0 不含点
/// - 点形：位置 i 是 '.' 且前一字符恰是词字符（\b 在词↔点间成立），
///   group0 含点（`v.unwrap(` → ".unwrap("）；行首 '.' 不成立
pub(crate) fn panic_call_finditer(ln: &str) -> Vec<(String, String)> {
    let cs: Vec<char> = ln.chars().collect();
    let n = cs.len();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < n {
        let c = cs[i];
        let name_gate = c.is_ascii_alphabetic() && (i == 0 || !is_py_word(cs[i - 1]));
        let dot_gate = c == '.' && i > 0 && is_py_word(cs[i - 1]);
        if !name_gate && !dot_gate {
            i += 1;
            continue;
        }
        let j = if c == '.' {
            let mut jj = i + 1;
            while jj < n && is_js_space(cs[jj]) {
                jj += 1;
            }
            jj
        } else {
            i
        };
        let mut matched: Option<(usize, &'static str)> = None;
        for name in PANIC_NAMES {
            let nc: Vec<char> = name.chars().collect();
            if j + nc.len() <= n && cs[j..j + nc.len()] == nc[..] {
                let mut k = j + nc.len();
                while k < n && is_js_space(cs[k]) {
                    k += 1;
                }
                if k < n && (cs[k] == '(' || cs[k] == '!') {
                    matched = Some((k + 1, name));
                    break;
                }
            }
        }
        if let Some((end, name)) = matched {
            out.push((name.to_string(), cs[i..end].iter().collect::<String>()));
            i = end;
        } else {
            i += 1;
        }
    }
    out
}

pub(crate) struct RustMeta {

    pub(crate) fn_count: usize,
    pub(crate) unsafe_count: usize,
    pub(crate) risky: Vec<Value>,
    pub(crate) fns: Vec<String>,}

pub(crate) fn scan_rust_struct(masked: &[char], fp: &str) -> (Vec<Value>, RustMeta) {
    let text: String = masked.iter().collect();
    let lines: Vec<&str> = text.split('\n').collect();
    let mut issues = Vec::new();
    let mut fn_names: Vec<String> = Vec::new();
    let mut unsafe_blocks: Vec<usize> = Vec::new();
    let mut in_test_mod = false;
    let mut test_mod_depth: i64 = -1;
    let mut brace_depth: i64 = 0;
    let mut fn_stack: Vec<(i64, String)> = Vec::new();
    let mut fn_risk: Vec<(String, i128, i128)> = Vec::new(); // (name, unwrap, unsafe)

    for (idx, ln) in lines.iter().enumerate() {
        let idx = idx + 1;
        let stripped = ln.trim();
        if !in_test_mod && mod_test_attr_search(stripped) {
            in_test_mod = true;
            test_mod_depth = brace_depth;
        } else if in_test_mod && stripped.starts_with('}') && brace_depth <= test_mod_depth {
            in_test_mod = false;
        }
        // 先压栈：本行命中才能归属到本 fn
        if ln.contains('{')
            && let Some(name) = fn_re_search(ln) {
                fn_stack.push((brace_depth, name.clone()));
                fn_names.push(name);
            }
        if !in_test_mod {
            let uc = unsafe_count_line(ln);
            for _ in 0..uc {
                unsafe_blocks.push(idx);
                let owner =
                    fn_stack.last().map(|f| f.1.clone()).unwrap_or_else(|| "<toplevel>".into());
                let e = match fn_risk.iter_mut().find(|f| f.0 == owner) {
                    Some(e) => e,
                    None => {
                        fn_risk.push((owner.clone(), 0, 0));
                        fn_risk.last_mut().unwrap()
                    }
                };
                e.2 += 1;
                issues.push(o(vec![
                    ("file", s(fp)),
                    ("line", i(idx)),
                    ("col", i(0)),
                    ("rule", s("rust_unsafe")),
                    ("detail", s("unsafe 块（设计信号，需人工评估不变量）")),
                    ("unit", s("call")),
                    ("fn", s(&owner)),
                ]));
            }
            for (name, g0) in panic_call_finditer(ln) {
                let rule = if matches!(
                    name.as_str(),
                    "panic!" | "unreachable!" | "todo!" | "unimplemented!"
                ) {
                    "rust_panic_macro"
                } else {
                    "rust_unwrap_expect"
                };
                let owner =
                    fn_stack.last().map(|f| f.1.clone()).unwrap_or_else(|| "<toplevel>".into());
                let e = match fn_risk.iter_mut().find(|f| f.0 == owner) {
                    Some(e) => e,
                    None => {
                        fn_risk.push((owner.clone(), 0, 0));
                        fn_risk.last_mut().unwrap()
                    }
                };
                if rule == "rust_unsafe" {
                    e.2 += 1;
                } else if rule == "rust_unwrap_expect" {
                    e.1 += 1;
                }
                let detail: String = g0.chars().take(40).collect();
                issues.push(o(vec![
                    ("file", s(fp)),
                    ("line", i(idx)),
                    ("col", i(0)),
                    ("rule", s(rule)),
                    ("detail", s(&detail)),
                    ("unit", s("call")),
                    ("fn", s(&owner)),
                ]));
            }
        }
        let new_depth =
            brace_depth + ln.matches('{').count() as i64 - ln.matches('}').count() as i64;
        while let Some(top) = fn_stack.last() {
            if top.0 >= new_depth {
                fn_stack.pop();
            } else {
                break;
            }
        }
        brace_depth = new_depth;
    }

    let mut risky: Vec<(String, i128, i128)> =
        fn_risk.iter().filter(|f| f.1 > 0 || f.2 > 0).cloned().collect();
    risky.sort_by_key(|f| -(f.1 * 2 + f.2 * 8));
    let risky: Vec<Value> = risky
        .into_iter()
        .take(12)
        .map(|f| o(vec![("fn", s(&f.0)), ("unwrap", Value::Int(f.1)), ("unsafe", Value::Int(f.2))]))
        .collect();
    (
        issues,
        RustMeta {
            fn_count: fn_names.len(),
            unsafe_count: unsafe_blocks.len(),
            risky,
            fns: fn_names.into_iter().take(40).collect(),
        },
    )
}

// ---------- S16：Rust 跨文件引用可达性 ----------

pub(crate) const RUST_KEYWORDS: [&str; 41] = [
    "fn", "let", "if", "else", "match", "return", "mod", "pub", "use", "impl",
    "self", "Self", "struct", "enum", "trait", "for", "in", "while", "loop",
    "const", "static", "type", "where", "as", "mut", "ref", "move", "dyn",
    "unsafe", "crate", "super", "true", "false", "assert", "assert_eq",
    "unsafe_fn", "async", "await", "box", "extern", "macro_rules",
];

