//! bug 子模块（S168 从 bug.rs 拆出；纯搬移，未改语义）。

/// spawn \s* \( \s* \(? \s* UNITS{0,160} 惰性 → 分支 A：RigidBody::Static \s* , [200字符]惰性 MARKER
///                                    分支 B：MARKER \s* , [200字符]惰性 RigidBody::Static \s* ,
/// 返回 match 终点（finditer 非重叠的游标依据）。
/// \(? 贪婪优先吃掉第二个 '('（整条元组直落单元起点），失败回溯不吃——
/// 单元从第二个 '(' 起（括号组整体算一个单元）。
pub(crate) fn try_static_velocity(src: &str, p: usize) -> Option<usize> {
    let mut i = p + 5;
    i = skip_ws(src, i);
    if src.as_bytes().get(i) != Some(&b'(') {
        return None;
    }
    let i1 = skip_ws(src, i + 1);
    if src.as_bytes().get(i1) == Some(&b'(') {
        let i2 = skip_ws(src, i1 + 1);
        if let Some(end) = units_then(src, i2, true) {
            return Some(end);
        }
        if let Some(end) = units_then(src, i2, false) {
            return Some(end);
        }
    }
    if let Some(end) = units_then(src, i1, true) {
        return Some(end);
    }
    units_then(src, i1, false)
}

pub(crate) fn units_then(src: &str, start: usize, branch_a: bool) -> Option<usize> {
    let mut j = start;
    let mut count = 0usize;
    loop {
        if branch_a {
            if src[j..].starts_with("RigidBody::Static") {
                let k = skip_ws(src, j + "RigidBody::Static".len());
                if src.as_bytes().get(k) == Some(&b',')
                    && let Some(end) = scan_for_marker(src, k + 1, 200) {
                        return Some(end);
                    }
            }
        } else if let Some(mend) = marker_at(src, j) {
            let k = skip_ws(src, mend);
            if src.as_bytes().get(k) == Some(&b',')
                && let Some(end) = scan_for_rigid(src, k + 1, 200) {
                    return Some(end);
                }
        }
        if count >= 160 {
            return None;
        }
        {
            let j2 = unit_step(src, j)?;
            j = j2;
            count += 1;
        }
    }
}

/// 单元 (?:[^);]|\([^)]*\))：非 )/; 字符，或一层括号组（内层无 ')'）
/// 按字符步进（Python [^);] 计字符；按字节会踩进 CJK 多字节序列中间）
pub(crate) fn unit_step(src: &str, j: usize) -> Option<usize> {
    let c = src[j..].chars().next()?;
    match c {
        ')' | ';' => None,
        '(' => {
            let close = src[j + 1..].find(')')?;
            Some(j + 1 + close + 1)
        }
        _ => Some(j + c.len_utf8()),
    }
}

/// MARKER = LinearVelocity(?!::ZERO) | ExternalForce | AngularVelocity(?!::ZERO)
/// 返回 marker 文本终点；::ZERO 的两兄弟在当前位置失配后由调用方前进 1 字符（回溯等价）
pub(crate) fn marker_at(src: &str, pos: usize) -> Option<usize> {
    for (name, zero_ok) in [("LinearVelocity", false), ("ExternalForce", true), ("AngularVelocity", false)] {
        if src[pos..].starts_with(name) {
            let end = pos + name.len();
            if zero_ok || !src[end..].starts_with("::ZERO") {
                return Some(end);
            }
        }
    }
    None
}

/// [\s\S]{0,200}?MARKER：按字符计数（Python {n} 计字符不计字节）
pub(crate) fn scan_for_marker(src: &str, from: usize, cap: usize) -> Option<usize> {
    let mut pos = from;
    let mut cnt = 0usize;
    loop {
        if let Some(end) = marker_at(src, pos) {
            return Some(end);
        }
        if cnt >= cap {
            return None;
        }
        {
            let c = src[pos..].chars().next()?;
            pos += c.len_utf8();
            cnt += 1;
        }
    }
}

/// [\s\S]{0,200}?RigidBody::Static\s*,
pub(crate) fn scan_for_rigid(src: &str, from: usize, cap: usize) -> Option<usize> {
    let mut pos = from;
    let mut cnt = 0usize;
    loop {
        if src[pos..].starts_with("RigidBody::Static") {
            let k = skip_ws(src, pos + "RigidBody::Static".len());
            if src.as_bytes().get(k) == Some(&b',') {
                return Some(k + 1);
            }
        }
        if cnt >= cap {
            return None;
        }
        {
            let c = src[pos..].chars().next()?;
            pos += c.len_utf8();
            cnt += 1;
        }
    }
}

pub(crate) fn skip_ws(src: &str, mut i: usize) -> usize {
    while matches!(src.as_bytes().get(i), Some(c) if c.is_ascii_whitespace()) {
        i += 1;
    }
    i
}

// ---------- 通用正则规则 ----------

