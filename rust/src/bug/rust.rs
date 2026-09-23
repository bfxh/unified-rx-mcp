//! bug 子模块（S168 从 bug.rs 拆出；纯搬移，未改语义）。
use super::*;

/// 文本规则的推送器签名（`push_rule` 闭包；别名是为了过 clippy::type_complexity）。
type PushRule<'a> = &'a dyn Fn(&mut Vec<Issue>, usize, &'static str, &str);
/// Bevy 规则的推送器签名（`push_bevy` 闭包，多一个 sev 参数）。
type PushBevy<'a> = &'a dyn Fn(&mut Vec<Issue>, usize, &'static str, &str, &'static str);

pub(crate) fn rust_rules_lexical(src: &str, lines: &[&str], path: &str, issues: &mut Vec<Issue>, push_rule: PushRule<'_>) {
    // unwrap：\.unwrap\(\)
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find(".unwrap()") {
        let p = cur + rel;
        push_rule(issues, p, "unwrap", "unwrap()——None/Err 时 panic（线索：确认有 ?/match 兜底即可忽略）");
        cur = p + 9;
    }
    // expect：\.expect\(\s*\"（\s 可跨行）
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find(".expect(") {
        let p = cur + rel;
        let mut j = p + 8;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if src.as_bytes().get(j) == Some(&b'"') {
            push_rule(issues, p, "expect", "expect()——带消息 panic（线索）");
            cur = j + 1;
        } else {
            cur = p + 1;
        }
    }
    // panic / unreachable：\b 词边界 + '!' 字面量
    for (lit, rule, msg) in [
        ("panic!(", "panic", "panic!()——直接崩溃"),
        ("unreachable!(", "unreachable", "unreachable!()——到达即 bug"),
    ] {
        let mut cur = 0usize;
        while let Some(rel) = src[cur..].find(lit) {
            let p = cur + rel;
            if p == 0 || !is_word(src.as_bytes()[p - 1]) {
                let line = count_newlines_before(src, p) + 1;
                let text = lines.get(line - 1).copied().unwrap_or("").trim();
                if !text.starts_with("//") {
                    issues.push(Issue {
                        line,
                        rule,
                        msg: msg.to_string(),
                        file: path.to_string(),
                        sev: Some("high"),
                        kind: Some("definite"),
                    });
                }
                cur = p + lit.len();
            } else {
                cur = p + 1;
            }
        }
    }
    // todo_unimplemented：\b(todo!|unimplemented!)\(——单趟两候选取更靠前者
    {
        const TODO_MSG: &str = "todo!/unimplemented!()——未实现即崩溃";
        let mut cur = 0usize;
        loop {
            let a = src[cur..].find("todo!(").map(|r| cur + r);
            let b = src[cur..].find("unimplemented!(").map(|r| cur + r);
            let p = match (a, b) {
                (Some(x), Some(y)) => x.min(y),
                (Some(x), None) => x,
                (None, Some(y)) => y,
                _ => break,
            };
            if p == 0 || !is_word(src.as_bytes()[p - 1]) {
                let line = count_newlines_before(src, p) + 1;
                let text = lines.get(line - 1).copied().unwrap_or("").trim();
                if !text.starts_with("//") {
                    issues.push(Issue {
                        line,
                        rule: "todo_unimplemented",
                        msg: TODO_MSG.to_string(),
                        file: path.to_string(),
                        sev: Some("high"),
                        kind: Some("definite"),
                    });
                }
                cur = p + if src[p..].starts_with("unimplemented!(") { 15 } else { 6 };
            } else {
                cur = p + 1;
            }
        }
    }
}

pub(crate) fn rust_rules_indexing(src: &str, issues: &mut Vec<Issue>, push_rule: PushRule<'_>) {
    // as_cast：\bas\s+(i64|i32|u64|u32|f64|f32|usize|isize)\b
    {
        const TYPES: [&str; 8] = ["i64", "i32", "u64", "u32", "f64", "f32", "usize", "isize"];
        let mut cur = 0usize;
        while let Some(rel) = src[cur..].find("as") {
            let p = cur + rel;
            cur = p + 1;
            if p != 0 && is_word(src.as_bytes()[p - 1]) {
                continue; // \b
            }
            let mut j = p + 2;
            let mut ws = 0usize;
            while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
                j += 1;
                ws += 1;
            }
            if ws == 0 {
                continue;
            }
            let Some(t) = TYPES.iter().find(|t| src[j..].starts_with(**t)) else { continue };
            let after = src.as_bytes().get(j + t.len());
            if after.is_none_or(|c| !is_word(*c)) {
                push_rule(issues, p, "as_cast", "as 类型转换——截断/精度丢失（线索：建议 try_from）");
                cur = p + 2 + ws + t.len();
            }
        }
    }
    // indexing / indexing2：(?<=[\w)\]]) 前环视——左字节是词字符、')' 或 ']'
    {
        let mut cur = 0usize;
        while let Some(rel) = src[cur..].find('[') {
            let p = cur + rel;
            cur = p + 1;
            let prev_ok = p > 0
                && (is_word(src.as_bytes()[p - 1])
                    || src.as_bytes()[p - 1] == b')'
                    || src.as_bytes()[p - 1] == b']');
            if !prev_ok {
                continue;
            }
            let b = src.as_bytes();
            // 规则 7：单个标识符 [A-Za-z_][A-Za-z0-9_]*
            let mut j = p + 1;
            let ident = matches!(b.get(j), Some(c) if c.is_ascii_alphabetic() || *c == b'_');
            if ident {
                j += 1;
                while matches!(b.get(j), Some(c) if c.is_ascii_alphanumeric() || *c == b'_') {
                    j += 1;
                }
                if b.get(j) == Some(&b']') {
                    push_rule(issues, p, "indexing", "索引访问——越界即 panic（线索：建议 .get()）");
                    cur = j + 1;
                    continue;
                }
            }
            // 规则 8：[^\]\[\n]{0,80}\bas\s+(usize|isize|i64|i32|u64|u32)\s*\]
            let mut k = p + 1;
            let mut cnt = 0usize;
            while let Some(&c) = b.get(k) {
                if c == b']' || c == b'[' || c == b'\n' || cnt >= 80 {
                    break;
                }
                k += 1;
                cnt += 1;
            }
            if b.get(k) == Some(&b']') && as_cast_suffix(&src[p + 1..k]) {
                push_rule(issues, p, "indexing", "索引访问（含 as 转换）——越界即 panic（线索：建议 .get()）");
                cur = k + 1;
            }
        }
    }

    // ---- Bevy 规则（无注释行闸；kind 统一 clue）----
}

pub(crate) fn rust_rules_bevy(src: &str, issues: &mut Vec<Issue>, push_bevy: PushBevy<'_>) {
    // bevy_old_system（字面量含 '('——.add_systems( 不命中）
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find(".add_system(") {
        let p = cur + rel;
        push_bevy(issues, p, "bevy_old_system", "add_system 旧 API——用 .add_systems（迁移线索）", "info");
        cur = p + 12;
    }
    // bevy_old_startup
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find(".add_startup_system(") {
        let p = cur + rel;
        push_bevy(issues, p, "bevy_old_startup", "add_startup_system 旧 API——用 .add_systems(Startup, ...)（迁移线索）", "info");
        cur = p + 20;
    }
    // bevy_event_iter：EventReader<[^>]+>\.iter\(（[^>]+ 跨行）
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("EventReader<") {
        let p = cur + rel;
        cur = p + 1;
        let content_start = p + 12;
        let Some(grel) = src[content_start..].find('>') else { continue };
        let gt = content_start + grel;
        if gt == content_start {
            continue; // [^>]+ 至少 1 字符
        }
        if src[gt + 1..].starts_with(".iter(") {
            push_bevy(issues, p, "bevy_event_iter", "EventReader.iter 旧 API——用 .read()（迁移线索）", "info");
            cur = gt + 7;
        }
    }
    // bevy_text_old：TextBundle\s*\{（\s 跨行）
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("TextBundle") {
        let p = cur + rel;
        let mut j = p + 10;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if src.as_bytes().get(j) == Some(&b'{') {
            push_bevy(issues, p, "bevy_text_old", "TextBundle 旧式——用 Text::new（迁移线索）", "info");
            cur = j + 1;
        } else {
            cur = p + 1;
        }
    }
    // bevy_query_single
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find(".single()") {
        let p = cur + rel;
        push_bevy(issues, p, "bevy_query_single", "query.single() 自 Bevy 0.16 起返回 Result——Err 静默失败是逻辑雷（用 let Ok = .. else return 兜）；.single().unwrap() 才会 panic（09-05 VoxelForge 11 处甄别：全部正确 else-return，零真险）（线索）", "low");
        cur = p + 9;
    }
    // bevy_phys_locked_axes_bits
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("LockedAxes::from_bits(") {
        let p = cur + rel;
        let mut j = p + 22;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if src[j..].starts_with("0b") {
            push_bevy(issues, p, "bevy_phys_locked_axes_bits", "LockedAxes 魔数位——位序易错（VoxelForge 0b000_101 曾误读为锁平移），用具名位常量 ROTATION_X/TRANSLATION_* 核对", "info");
            cur = j + 2;
        } else {
            cur = p + 1;
        }
    }
    // bevy_phys_static_with_velocity：双分支受限惰性匹配（见 try_static_velocity）
    {
        let mut cur = 0usize;
        while let Some(rel) = src[cur..].find("spawn") {
            let p = cur + rel;
            match try_static_velocity(src, p) {
                Some(end) => {
                    push_bevy(issues, p, "bevy_phys_static_with_velocity", "spawn 元组里 RigidBody::Static 携带速度/受力组件——Static 体不响应力与速度，写了不生效（::ZERO 冗余不报；VoxelForge 09-05 甄别：matches! 判断与测试 fixture 为误报源；S74 两个分支都锚 spawn 元组——前一条 spawn 的速度逗号 + 200 字符内另一条 Static spawn 不再跨语句误连）", "low");
                    cur = end;
                }
                None => cur = p + 1,
            }
        }
    }
    // bevy_phys_manual_support_force：apply_force_at_point\(\s*Vec3::Y\b
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("apply_force_at_point(") {
        let p = cur + rel;
        let mut j = p + 21;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if src[j..].starts_with("Vec3::Y") {
            let after = src.as_bytes().get(j + 7);
            if after.is_none_or(|c| !is_word(*c)) {
                push_bevy(issues, p, "bevy_phys_manual_support_force", "手写竖直支撑/弹簧力（Vec3::Y × f）——多轮/多执行器各自封顶≠总和有界：四轮同压可叠到 3×车重持续弹起（VoxelForge 09-04 四轮弹跳床案），须有整车总力预算", "med");
                cur = j + 7;
                continue;
            }
        }
        cur = p + 1;
    }

}

pub(crate) fn scan_rust(src: &str, path: &str) -> Vec<Issue> {
    let mut issues: Vec<Issue> = Vec::new();
    let lines: Vec<&str> = src.split('\n').collect();
    // 命中行是整行注释则跳过——只作用于 _RUST_RULES 表（bevy 无此闸，与 Python 一致）
    let push_rule = |issues: &mut Vec<Issue>, pos: usize, rule: &'static str, msg: &str| {
        let line = count_newlines_before(src, pos) + 1;
        let text = lines.get(line - 1).copied().unwrap_or("").trim();
        if text.starts_with("//") {
            return;
        }
        issues.push(Issue {
            line,
            rule,
            msg: msg.to_string(),
            file: path.to_string(),
            sev: Some("info"),
            kind: Some("clue"),
        });
    };

    rust_rules_lexical(src, &lines, path, &mut issues, &push_rule);
    rust_rules_indexing(src, &mut issues, &push_rule);
    let push_bevy = |issues: &mut Vec<Issue>, pos: usize, rule: &'static str, msg: &str, sev: &'static str| {
        issues.push(Issue {
            line: count_newlines_before(src, pos) + 1,
            rule,
            msg: msg.to_string(),
            file: path.to_string(),
            sev: Some(sev),
            kind: Some("clue"),
        });
    };
    rust_rules_bevy(src, &mut issues, &push_bevy);
    // ---- 测试代码降级：文件级（tests 目录 / *_test.rs）+ 行级（#[cfg(test)] 起）----
    let norm = path.replace('\\', "/").replace("_tmp/", "");
    let is_test_file =
        norm.ends_with("_test.rs") || contains_tests_segment(&norm);
    let mut test_start_line: Option<usize> = None;
    if !is_test_file {
        test_start_line = find_cfg_test_line(src);
    }
    for i in issues.iter_mut() {
        let in_test =
            is_test_file || test_start_line.is_some_and(|t| i.line >= t);
        if in_test && matches!(i.rule, "unwrap" | "expect" | "as_cast" | "indexing") {
            i.sev = Some("low");
            i.kind = Some("clue");
            i.msg.push_str("（测试代码，降级）");
        } else if in_test && i.rule == "panic" {
            i.sev = Some("low");
            i.kind = Some("clue");
            i.msg.push_str("（测试上下文，通常为断言用途，降级）");
        }
    }
    issues
}

/// 规则 8 的尾部校验：^.*\bas\s+(usize|isize|i64|i32|u64|u32)\s*$（'as' 带词边界）
pub(crate) fn as_cast_suffix(inner: &str) -> bool {
    let nb = inner.as_bytes();
    for (pos, _) in inner.match_indices("as") {
        if pos != 0 && is_word(nb[pos - 1]) {
            continue;
        }
        let mut j = pos + 2;
        let mut ws = 0usize;
        while matches!(nb.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
            ws += 1;
        }
        if ws == 0 {
            continue;
        }
        let mut matched = false;
        for t in ["usize", "isize", "i64", "i32", "u64", "u32"] {
            if inner[j..].starts_with(t) {
                j += t.len();
                matched = true;
                break;
            }
        }
        if !matched {
            continue;
        }
        while matches!(nb.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if j == inner.len() {
            return true;
        }
    }
    false
}

/// /tests 段检测（re.search(r"/tests(?:/|$)") 等价）
pub(crate) fn contains_tests_segment(norm: &str) -> bool {
    let mut from = 0usize;
    while let Some(rel) = norm[from..].find("/tests") {
        let p = from + rel;
        match norm.as_bytes().get(p + 6) {
            None => return true,
            Some(b'/') => return true,
            _ => from = p + 1,
        }
    }
    false
}

/// ^#\[\s*cfg\s*\(\s*test\s*\)\s*\]（MULTILINE）：返回属性行号（首处）
pub(crate) fn find_cfg_test_line(src: &str) -> Option<usize> {
    for (idx, line) in src.split('\n').enumerate() {
        let b = line.as_bytes();
        let mut j = 0usize;
        if !line.starts_with("#[") {
            continue;
        }
        j += 2;
        while matches!(b.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if !line[j..].starts_with("cfg") {
            continue;
        }
        j += 3;
        while matches!(b.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if b.get(j) != Some(&b'(') {
            continue;
        }
        j += 1;
        while matches!(b.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if !line[j..].starts_with("test") {
            continue;
        }
        j += 4;
        while matches!(b.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if b.get(j) != Some(&b')') {
            continue;
        }
        j += 1;
        while matches!(b.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if b.get(j) == Some(&b']') {
            return Some(idx + 1);
        }
    }
    None
}

// ---------- bevy_phys_static_with_velocity：双分支受限惰性匹配 ----------

