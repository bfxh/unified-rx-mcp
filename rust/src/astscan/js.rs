//! astscan 子模块（S168 从 astscan.rs 拆出；纯搬移，未改语义）。
use super::*;

/// 从 k 起取字符类最长游程（≥min），自最长向短回溯找 \b 成立的终点。
pub(crate) fn greedy_bounded(cs: &[char], k: usize, min: usize, in_class: impl Fn(char) -> bool) -> Option<usize> {
    let n = cs.len();
    let mut run = 0usize;
    while k + run < n && in_class(cs[k + run]) {
        run += 1;
    }
    if run < min {
        return None;
    }
    for len in (min..=run).rev() {
        let e = k + len;
        let before = is_py_word(cs[e - 1]);
        let after = if e < n { is_py_word(cs[e]) } else { false };
        if before != after {
            return Some(e);
        }
    }
    None
}

// ---------- JS：词法掩码（char 口径保长度） ----------

pub(crate) fn is_js_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}

pub(crate) fn mask_js(src: &str) -> (Vec<char>, usize, usize, usize) {
    let cs: Vec<char> = src.chars().collect();
    let n = cs.len();
    let mut out = cs.clone();
    let (mut strings, mut templates, mut comments) = (0usize, 0usize, 0usize);
    struct Tpl {
    
    pub(crate) brace: usize,    
    pub(crate) in_code: bool,    }
    let mut stack: Vec<Tpl> = Vec::new();
    let mut i = 0usize;
    while i < n {
        let c = cs[i];
        let nxt = if i + 1 < n { cs[i + 1] } else { '\0' };
        // 模板文本区
        if let Some(top) = stack.last()
            && !top.in_code {
                if c == '\\' && i + 1 < n {
                    out[i] = ' ';
                    out[i + 1] = ' ';
                    i += 2;
                    continue;
                }
                if c == '$' && nxt == '{' {
                    let top = stack.last_mut().unwrap();
                    top.in_code = true;
                    top.brace = 0;
                    i += 2;
                    continue;
                }
                if c == '`' {
                    out[i] = ' ';
                    stack.pop();
                    i += 1;
                    continue;
                }
                out[i] = ' ';
                i += 1;
                continue;
            }
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
        } else if c == '"' || c == '\'' {
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
        } else if c == '`' {
            out[i] = ' ';
            stack.push(Tpl { brace: 0, in_code: false });
            templates += 1;
            i += 1;
        } else if let Some(top) = stack.last_mut() {
            // top.in_code：插值内是真代码
            if c == '{' {
                top.brace += 1;
            } else if c == '}' {
                if top.brace == 0 {
                    top.in_code = false; // '}' 本身保留：插值边界结构
                } else {
                    top.brace -= 1;
                }
            }
            i += 1;
        } else {
            i += 1;
        }
    }
    (out, strings, templates, comments)
}

// ---------- JS：调用面提取（_CALL_RE 手写等价） ----------

pub(crate) fn is_js_chain_start(c: char) -> bool {
    c.is_ascii_alphabetic() || c == '_' || c == '$'
}
pub(crate) fn is_js_chain_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_' || c == '$'
}

/// 尝试在 i 匹配 (new\s+)?链\s*\(；返回 (is_new, 链, 匹配结束位)。
/// 正则回溯语义：new 组先试、失败回退空组。
pub(crate) fn try_call_at(cs: &[char], i: usize) -> Option<(bool, String, usize)> {
    if let Some(r) = attempt_call(cs, i, true) {
        return Some(r);
    }
    attempt_call(cs, i, false)
}

pub(crate) fn attempt_call(cs: &[char], i: usize, allow_new: bool) -> Option<(bool, String, usize)> {
    let n = cs.len();
    let mut j = i;
    let mut is_new = false;
    if allow_new
        && j + 3 < n
        && cs[j] == 'n'
        && cs[j + 1] == 'e'
        && cs[j + 2] == 'w'
        && is_js_space(cs[j + 3])
    {
        is_new = true;
        j += 3;
        while j < n && is_js_space(cs[j]) {
            j += 1;
        }
    }
    let read_seg = |k: usize| -> Option<usize> {
        if k >= n || !is_js_chain_start(cs[k]) {
            return None;
        }
        let mut e = k + 1;
        while e < n && is_js_chain_char(cs[e]) {
            e += 1;
        }
        Some(e)
    };
    let end0 = read_seg(j)?;
    let mut chain: String = cs[j..end0].iter().collect();
    j = end0;
    while j < n && cs[j] == '.' {
        match read_seg(j + 1) {
            Some(e) => {
                chain.push('.');
                chain.push_str(&cs[j + 1..e].iter().collect::<String>());
                j = e;
            }
            None => break,
        }
    }
    while j < n && is_js_space(cs[j]) {
        j += 1;
    }
    if j < n && cs[j] == '(' {
        Some((is_new, chain, j + 1))
    } else {
        None
    }
}

pub(crate) fn scan_js_calls(masked: &[char], fp: &str) -> (Vec<Value>, usize) {
    let n = masked.len();
    let mut line_starts: Vec<usize> = vec![0];
    for (k, &c) in masked.iter().enumerate() {
        if c == '\n' {
            line_starts.push(k + 1);
        }
    }
    let pos_of = |off: usize| -> (usize, usize) {
        // bisect_right(line_starts, off) - 1
        let mut lo = 0usize;
        let mut hi = line_starts.len();
        while lo < hi {
            let mid = (lo + hi) / 2;
            if line_starts[mid] <= off {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        let li = lo - 1;
        (li + 1, off - line_starts[li])
    };
    let mut issues = Vec::new();
    let mut total = 0usize;
    let mut cur = 0usize;
    while cur < n {
        // (?<![.\w$])：前一字符不得是点/单词符/$
        if cur > 0 && (masked[cur - 1] == '.' || masked[cur - 1] == '$' || is_py_word(masked[cur - 1])) {
            cur += 1;
            continue;
        }
        if let Some((is_new, chain, end)) = try_call_at(masked, cur) {
            total += 1;
            // （旧实现的括号平衡游标是死代码——匹配结果只用于统计与分类，略去）
            if is_new && chain == "Function" {
                let (ln, co) = pos_of(cur);
                issues.push(o(vec![
                    ("file", s(fp)),
                    ("line", i(ln)),
                    ("col", i(co)),
                    ("rule", s("js_new_function")),
                    ("callee", s("new Function(...)")),
                    ("span_args", Value::Null),
                    ("unit", s("call")),
                ]));
            } else if !chain.contains('.')
                && JS_SINKS_BARE.contains(&chain.as_str())
            {
                let (ln, co) = pos_of(cur);
                issues.push(o(vec![
                    ("file", s(fp)),
                    ("line", i(ln)),
                    ("col", i(co)),
                    ("rule", s("js_dynamic_exec")),
                    ("callee", s(&chain)),
                    ("unit", s("call")),
                ]));
            }
            cur = end; // finditer 语义：从匹配末尾续扫（嵌套调用因此单独计）
        } else {
            cur += 1;
        }
    }
    (issues, total)
}

// ---------- Rust：词法掩码 + 结构化信号 ----------

