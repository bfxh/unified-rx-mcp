//! bug 子模块（S168 从 bug.rs 拆出；纯搬移，未改语义）。
use super::*;

pub(crate) fn scan_generic(src: &str, path: &str) -> Vec<Issue> {
    let mut issues: Vec<Issue> = Vec::new();
    let push = |issues: &mut Vec<Issue>, pos: usize, rule: &'static str, msg: &str| {
        issues.push(Issue {
            line: count_newlines_before(src, pos) + 1,
            rule,
            msg: msg.to_string(),
            file: path.to_string(),
            sev: Some("med"),
            kind: Some("clue"),
        });
    };
    // assert_always_true：assert\s+True\b——无左侧边界（保真：myassert True 也命中）
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("assert") {
        let p = cur + rel;
        let mut j = p + 6;
        let mut ws = 0usize;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
            ws += 1;
        }
        if ws >= 1 && src[j..].starts_with("True") {
            let after = src.as_bytes().get(j + 4);
            if after.is_none_or(|c| !is_word(*c)) {
                push(&mut issues, p, "assert_always_true", "恒真断言（永远通过，无意义）");
                cur = j + 4;
                continue;
            }
        }
        cur = p + 1;
    }
    // equal_float：==\s*\d+\.\d+
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("==") {
        let p = cur + rel;
        let mut j = p + 2;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        let d0 = j;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_digit()) {
            j += 1;
        }
        if j > d0 && src.as_bytes().get(j) == Some(&b'.') {
            j += 1;
            let d1 = j;
            while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_digit()) {
                j += 1;
            }
            if j > d1 {
                push(&mut issues, p, "equal_float", "浮点相等比较（精度风险）");
                cur = j;
                continue;
            }
        }
        cur = p + 1;
    }
    // eval_exec：(?<![.\w])(eval|exec|execSync)\s*\(——成员调用 .exec( 排除（S61 教训）
    let mut cur = 0usize;
    loop {
        let cand = ["eval", "exec", "execSync"]
            .iter()
            .filter_map(|name| src[cur..].find(name).map(|r| cur + r))
            .min();
        let Some(p) = cand else { break };
        let prev_ok = p == 0
            || {
                let c = src.as_bytes()[p - 1];
                c != b'.' && !is_word(c)
            };
        if prev_ok {
            // 交替序 eval → exec → execSync（execSync 在 exec 失配后回溯胜出）
            let mut hit = None;
            for name in ["eval", "exec", "execSync"] {
                if src[p..].starts_with(name) {
                    let j = skip_ws(src, p + name.len());
                    if src.as_bytes().get(j) == Some(&b'(') {
                        hit = Some(j + 1);
                        break;
                    }
                }
            }
            if let Some(end) = hit {
                push(&mut issues, p, "eval_exec", "eval/exec 动态执行（安全风险）");
                cur = end;
                continue;
            }
        }
        cur = p + 1;
    }
    issues
}

