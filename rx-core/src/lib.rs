// SPDX-FileCopyrightText: 2026 bfxh
// SPDX-License-Identifier: MIT
//! rx-core — unified-rx 纯函数层（Rust 迁移一期）
//!
//! 对齐 Python 版 server.py 的纯函数语义（零第三方依赖 + serde_json）：
//! 边界校验 / DoS 防护 / 错误消息 / 输出格式逐一对应。
//! 验收标准：与 Python 版 1000 次输出一致（见 tests/parity）。
//!
//! 迁移来源（Python）：
//!   - math_ops / text_ops / sort_search / stat_geo / json_email /
//!     prime_list / fib_fibonacci（server.py `_m_*` 函数）
//!   - 错误用 `Err(String)` 返回，消息与 Python `ValueError` 一致。

use serde_json::Value;

// ─────────────────────────────────────────────────────────────
// 数学
// ─────────────────────────────────────────────────────────────

/// 除法：b==0 → 与 Python `ValueError("除数不能为 0")` 一致。
pub fn math_div(a: f64, b: f64) -> Result<String, String> {
    if b == 0.0 {
        return Err("除数不能为 0".into());
    }
    Ok(format_num(a / b))
}

/// 幂：指数/底数过大拒绝（DoS 防护，对齐 Python）。
pub fn math_power(base: f64, exponent: f64) -> Result<String, String> {
    if exponent.abs() > 1000.0 {
        return Err("指数绝对值过大（>1000），拒绝计算".into());
    }
    if base.abs() > 1e9 {
        return Err("底数过大（>1e9），拒绝计算".into());
    }
    Ok(format_num(base.powf(exponent)))
}

/// 平方根：负数拒绝。
pub fn math_sqrt(x: f64) -> Result<String, String> {
    if x < 0.0 {
        return Err("负数无实数平方根".into());
    }
    Ok(format_num(x.sqrt()))
}

/// 阶乘：非负 + ≤1000（对齐 Python 上限）。用 u128 精确计算（≤34! 精确，更大溢出报错——二期换 bigint）。
pub fn math_factorial(n: i64) -> Result<String, String> {
    if n < 0 {
        return Err("阶乘要求非负整数".into());
    }
    if n > 1000 {
        return Err("n 过大（>1000，超过 Python int→str 位限）".into());
    }
    let mut acc: u128 = 1;
    for i in 2..=n as u128 {
        acc = acc.checked_mul(i).ok_or("n 过大（超出 u128 精度，二期换 bigint）")?;
    }
    Ok(acc.to_string())
}

/// 斐波那契第 n 项：n≤20000（对齐 Python 上限）。u128 精确（≤185 项精确，更大溢出报错——二期换 bigint）。
pub fn fib_fibonacci(n: i64) -> Result<String, String> {
    if n < 0 {
        return Err("n 不能为负".into());
    }
    if n > 20000 {
        return Err("n 过大（>20000，超过 Python int→str 位限）".into());
    }
    let (mut a, mut b): (u128, u128) = (0, 1);
    for _ in 0..n {
        let c = a.checked_add(b).ok_or("n 过大（超出 u128 精度，二期换 bigint）")?;
        a = b;
        b = c;
    }
    Ok(a.to_string())
}

// ─────────────────────────────────────────────────────────────
// 字符串
// ─────────────────────────────────────────────────────────────

pub fn str_reverse(s: &str) -> String {
    s.chars().rev().collect()
}

pub fn str_upper(s: &str) -> String {
    s.to_uppercase()
}

pub fn str_lower(s: &str) -> String {
    s.to_lowercase()
}

/// 回文：与 Python `s == s[::-1]` 一致（字符级）。输出 Python 风格 True/False。
pub fn str_palindrome(s: &str) -> String {
    let rev: String = s.chars().rev().collect();
    py_bool(s == rev)
}

/// Python 风格布尔字符串（True/False，对齐 Python str()）。
pub fn py_bool(b: bool) -> String {
    if b { "True".into() } else { "False".into() }
}

// ─────────────────────────────────────────────────────────────
// 排序 / 搜索
// ─────────────────────────────────────────────────────────────

/// 快速排序（Python list.sort() 语义）：arr ≤100000 防 DoS。输出 Python 风格 `[1, 2]`。
pub fn sort_quick(arr: &[Value]) -> Result<String, String> {
    if arr.len() > 100_000 {
        return Err("数组过大（>100000）".into());
    }
    let mut v: Vec<Value> = arr.to_vec();
    v.sort_by(cmp_value);
    py_json(&Value::Array(v))
}

/// 冒泡排序：≤2000（O(n²) 防 DoS，对齐 Python）。
pub fn sort_bubble(arr: &[Value]) -> Result<String, String> {
    if arr.len() > 2000 {
        return Err("数组过大（>2000，冒泡 O(n²) 防 DoS）".into());
    }
    let mut v: Vec<Value> = arr.to_vec();
    let n = v.len();
    for i in 0..n {
        for j in 0..n - i - 1 {
            if cmp_value(&v[j], &v[j + 1]) == std::cmp::Ordering::Greater {
                v.swap(j, j + 1);
            }
        }
    }
    py_json(&Value::Array(v))
}

/// 二分搜索：返回下标或 -1（对齐 Python）。
pub fn search_binary(arr: &[Value], target: &Value) -> Result<String, String> {
    if arr.len() > 100_000 {
        return Err("数组过大（>100000）".into());
    }
    let (mut lo, mut hi) = (0i64, arr.len() as i64 - 1);
    while lo <= hi {
        let mid = (lo + hi) / 2;
        match cmp_value(&arr[mid as usize], target) {
            std::cmp::Ordering::Equal => return Ok(mid.to_string()),
            std::cmp::Ordering::Less => lo = mid + 1,
            std::cmp::Ordering::Greater => hi = mid - 1,
        }
    }
    Ok("-1".into())
}

// ─────────────────────────────────────────────────────────────
// 统计 / 几何 / 温度
// ─────────────────────────────────────────────────────────────

pub fn stat_mean(data: &[Value]) -> Result<String, String> {
    if data.is_empty() {
        return Err("数据为空".into());
    }
    if data.len() > 100_000 {
        return Err("数据过大（>100000）".into());
    }
    let mut sum = 0.0;
    for d in data {
        sum += as_f64(d);
    }
    Ok(format_num(sum / data.len() as f64))
}

pub fn stat_median(data: &[Value]) -> Result<String, String> {
    if data.is_empty() {
        return Err("数据为空".into());
    }
    if data.len() > 100_000 {
        return Err("数据过大（>100000）".into());
    }
    let mut v: Vec<f64> = data.iter().map(as_f64).collect();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = v.len();
    if n % 2 == 1 {
        Ok(format_num(v[n / 2]))
    } else {
        Ok(format_num((v[n / 2 - 1] + v[n / 2]) / 2.0))
    }
}

pub fn geo_circle(radius: f64) -> Result<String, String> {
    if radius < 0.0 {
        return Err("半径不能为负".into());
    }
    Ok(format_num(std::f64::consts::PI * radius * radius))
}

pub fn geo_rect(length: f64, width: f64) -> Result<String, String> {
    if length < 0.0 || width < 0.0 {
        return Err("边长不能为负".into());
    }
    Ok(format_num(2.0 * (length + width)))
}

pub fn conv_c2f(celsius: f64) -> String {
    format_num(celsius * 9.0 / 5.0 + 32.0)
}

pub fn conv_f2c(fahrenheit: f64) -> String {
    format_num((fahrenheit - 32.0) * 5.0 / 9.0)
}

// ─────────────────────────────────────────────────────────────
// JSON / 校验
// ─────────────────────────────────────────────────────────────

pub fn json_parse(s: &str) -> Result<String, String> {
    let v: Value = serde_json::from_str(s).map_err(|e| format!("JSON 解析失败: {}", e))?;
    py_json(&v)
}

pub fn json_valid(s: &str) -> String {
    if serde_json::from_str::<Value>(s).is_ok() { "true".into() } else { "false".into() }
}

/// 邮箱校验：对齐 Python `^[\w.+-]+@[\w-]+\.[\w.]+$`（近似——Rust 无 \w 直接等价，用 ASCII 实现）。
pub fn valid_email(email: &str) -> String {
    py_bool(regex_like_email(email))
}

// ─────────────────────────────────────────────────────────────
// 素数 / 列表
// ─────────────────────────────────────────────────────────────

pub fn prime_is_prime(n: i64) -> Result<String, String> {
    if n < 2 {
        return Ok("false".into());
    }
    if n > 10_000_000 {
        return Err("n 过大（>10M）".into());
    }
    let limit = (n as f64).sqrt() as i64;
    for i in 2..=limit {
        if n % i == 0 {
            return Ok("false".into());
        }
    }
    Ok("true".into())
}

/// 素数筛：limit ≤1M（对齐 Python）。
pub fn prime_generate(limit: i64) -> Result<String, String> {
    if limit > 1_000_000 {
        return Err("limit 过大（>1M）".into());
    }
    if limit < 2 {
        return Ok("[]".into());
    }
    let mut sieve = vec![true; (limit + 1) as usize];
    sieve[0] = false;
    sieve[1] = false;
    let root = (limit as f64).sqrt() as i64;
    for i in 2..=root {
        if sieve[i as usize] {
            let mut j = i * i;
            while j <= limit {
                sieve[j as usize] = false;
                j += i;
            }
        }
    }
    let primes: Vec<i64> = (2..=limit).filter(|&x| sieve[x as usize]).collect();
    py_json(&Value::Array(primes.iter().map(|p| Value::from(*p)).collect()))
}

/// 去重保序（对齐 Python set 语义——保首现顺序）。
pub fn list_unique(lst: &[Value]) -> Result<String, String> {
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    for x in lst {
        let key = x.to_string();
        if seen.insert(key) {
            out.push(x.clone());
        }
    }
    py_json(&Value::Array(out))
}

/// 展平嵌套列表（对齐 Python 递归 flatten）。
pub fn list_flatten(nested: &Value) -> Result<String, String> {
    fn flat(v: &Value, out: &mut Vec<Value>) {
        match v {
            Value::Array(arr) => {
                for i in arr {
                    flat(i, out);
                }
            }
            other => out.push(other.clone()),
        }
    }
    let mut out = Vec::new();
    flat(nested, &mut out);
    py_json(&Value::Array(out))
}

// ─────────────────────────────────────────────────────────────
// 内部工具（数值格式化对齐 Python str() 语义）
// ─────────────────────────────────────────────────────────────

/// 数值格式化对齐 Python str()：整数值浮点输出 "2.0"（Python 除法结果）；
/// 负零 "-0.0"；大数用 Rust 默认表示（与 Python 科学计数有差异，已标注）。
fn format_num(x: f64) -> String {
    if x == 0.0 && x.is_sign_negative() {
        return "-0.0".into();
    }
    if x == x.trunc() && x.abs() < 1e15 {
        format!("{}.0", x as i64)
    } else {
        format!("{}", x)
    }
}

/// Python 风格 JSON 序列化（`[1, 2]` 逗号+空格，对齐 Python json.dumps 默认）。
pub fn py_json(v: &Value) -> Result<String, String> {
    match v {
        Value::Array(items) => {
            let parts: Result<Vec<String>, String> =
                items.iter().map(py_json).collect();
            Ok(format!("[{}]", parts?.join(", ")))
        }
        Value::Object(map) => {
            let parts: Result<Vec<String>, String> = map
                .iter()
                .map(|(k, val)| Ok(format!("\"{}\": {}", json_escape(k), py_json(val)?)))
                .collect();
            Ok(format!("{{{}}}", parts?.join(", ")))
        }
        Value::String(s) => Ok(format!("\"{}\"", json_escape(s))),
        Value::Number(n) => Ok(n.to_string()),
        Value::Bool(b) => Ok(if *b { "true".into() } else { "false".into() }),
        Value::Null => Ok("null".into()),
    }
}

/// JSON 字符串完整转义（对齐 Python json.dumps）。
fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}


fn regex_like_email(email: &str) -> bool {
    let email = email.as_bytes();
    // 找 @
    let at = match email.iter().position(|&b| b == b'@') {
        Some(i) => i,
        None => return false,
    };
    let (local, rest) = (&email[..at], &email[at + 1..]);
    if local.is_empty() || rest.is_empty() {
        return false;
    }
    // local: [\w.+-]+
    if !local.iter().all(|&b| b.is_ascii_alphanumeric() || b == b'_' || b == b'.' || b == b'+' || b == b'-') {
        return false;
    }
    // domain: [\w-]+ \. [\w.]+
    let dot = match rest.iter().position(|&b| b == b'.') {
        Some(i) => i,
        None => return false,
    };
    let (d1, d2) = (&rest[..dot], &rest[dot + 1..]);
    if d1.is_empty() || d2.is_empty() {
        return false;
    }
    d1.iter().all(|&b| b.is_ascii_alphanumeric() || b == b'-')
        && d2.iter().all(|&b| b.is_ascii_alphanumeric() || b == b'.')
}

/// JSON Value 比较（数字/字符串/布尔；混合类型按字符串兜底，与 Python 语义近似）。
fn cmp_value(a: &Value, b: &Value) -> std::cmp::Ordering {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            let xf = x.as_f64().unwrap_or(0.0);
            let yf = y.as_f64().unwrap_or(0.0);
            xf.partial_cmp(&yf).unwrap_or(std::cmp::Ordering::Equal)
        }
        (Value::String(x), Value::String(y)) => x.cmp(y),
        (Value::Bool(x), Value::Bool(y)) => x.cmp(y),
        _ => a.to_string().cmp(&b.to_string()),
    }
}

fn as_f64(v: &Value) -> f64 {
    v.as_f64().unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn math_div_basic_and_zero() {
        assert_eq!(math_div(6.0, 3.0).unwrap(), "2.0");
        assert_eq!(math_div(1.0, 0.0).unwrap_err(), "除数不能为 0");
    }

    #[test]
    fn math_power_limits() {
        assert_eq!(math_power(2.0, 10.0).unwrap(), "1024.0");
        assert!(math_power(2.0, 1001.0).is_err(), "指数超限拒绝");
        assert!(math_power(1e10, 2.0).is_err(), "底数超限拒绝");
    }

    #[test]
    fn math_sqrt_negative_rejected() {
        assert_eq!(math_sqrt(9.0).unwrap(), "3.0");
        assert_eq!(math_sqrt(-1.0).unwrap_err(), "负数无实数平方根");
    }

    #[test]
    fn math_factorial_limits() {
        assert_eq!(math_factorial(5).unwrap(), "120");
        assert_eq!(math_factorial(-1).unwrap_err(), "阶乘要求非负整数");
        assert!(math_factorial(1001).is_err());
    }

    #[test]
    fn fib_values() {
        assert_eq!(fib_fibonacci(0).unwrap(), "0");
        assert_eq!(fib_fibonacci(1).unwrap(), "1");
        assert_eq!(fib_fibonacci(10).unwrap(), "55");
        assert_eq!(fib_fibonacci(-1).unwrap_err(), "n 不能为负");
    }

    #[test]
    fn str_ops() {
        assert_eq!(str_reverse("abc"), "cba");
        assert_eq!(str_upper("AbC"), "ABC");
        assert_eq!(str_lower("AbC"), "abc");
        assert_eq!(str_palindrome("abba"), "True");
        assert_eq!(str_palindrome("abc"), "False");
    }

    #[test]
    fn sort_and_search() {
        let arr: Vec<Value> = serde_json::from_str("[3,1,2]").unwrap();
        assert_eq!(sort_quick(&arr).unwrap(), "[1, 2, 3]");
        assert_eq!(sort_bubble(&arr).unwrap(), "[1, 2, 3]");
        let sorted: Vec<Value> = serde_json::from_str("[1,2,3,5]").unwrap();
        assert_eq!(search_binary(&sorted, &Value::from(3)).unwrap(), "2");
        assert_eq!(search_binary(&sorted, &Value::from(9)).unwrap(), "-1");
    }

    #[test]
    fn stat_geo_conv() {
        let data: Vec<Value> = serde_json::from_str("[1,2,3]").unwrap();
        assert_eq!(stat_mean(&data).unwrap(), "2.0");
        assert_eq!(stat_median(&data).unwrap(), "2.0");
        assert_eq!(geo_circle(1.0).unwrap(), "3.141592653589793"); // π
        assert_eq!(geo_rect(3.0, 4.0).unwrap(), "14.0");
        assert_eq!(conv_c2f(100.0), "212.0");
        assert_eq!(conv_f2c(212.0), "100.0");
    }

    #[test]
    fn json_and_email() {
        assert_eq!(json_parse(r#"{"a":1}"#).unwrap(), r#"{"a": 1}"#);
        assert_eq!(json_valid("{bad}"), "false");
        assert_eq!(valid_email("a@b.com"), "True");
        assert_eq!(valid_email("bad"), "False");
    }

    #[test]
    fn prime_and_list() {
        assert_eq!(prime_is_prime(17).unwrap(), "true");
        assert_eq!(prime_is_prime(18).unwrap(), "false");
        assert_eq!(prime_generate(10).unwrap(), "[2, 3, 5, 7]");
        let lst: Vec<Value> = serde_json::from_str("[1,2,1,3]").unwrap();
        assert_eq!(list_unique(&lst).unwrap(), "[1, 2, 3]");
        let nested: Value = serde_json::from_str("[1,[2,[3]],4]").unwrap();
        assert_eq!(list_flatten(&nested).unwrap(), "[1, 2, 3, 4]");
    }

    #[test]
    fn dos_limits() {
        let big: Vec<Value> = (0..100_001).map(|i| Value::from(i)).collect();
        assert!(sort_quick(&big).is_err(), ">100000 拒绝");
        let big2: Vec<Value> = (0..2001).map(|i| Value::from(i)).collect();
        assert!(sort_bubble(&big2).is_err(), ">2000 冒泡拒绝");
        assert!(prime_generate(1_000_001).is_err());
    }
}
