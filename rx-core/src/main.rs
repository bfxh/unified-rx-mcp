// SPDX-FileCopyrightText: 2026 bfxh
// SPDX-License-Identifier: MIT
//! rx-core-cli — 命令行入口：stdin JSON {tool, args} → stdout 结果。
//!
//! 支持两种模式：
//!   - 单发：读全部 stdin（EOF 结束）处理一个请求后退出（parity_check 用）
//!   - 常驻：逐行读取，每行一个 JSON 请求，每行一个结果（server.py 用）
//! 两种模式协议相同：{tool, args} JSON → 结果字符串（错误为 "ERR: ..."）。

use rx_core::*;
use serde_json::Value;
use std::io::{BufRead, Write};

fn dispatch(req: &Value) -> String {
    let tool = req["tool"].as_str().unwrap_or("");
    let a = &req["args"];
    let out: Result<String, String> = match tool {
        "math_div" => math_div(num(a, "a"), num(a, "b")),
        "math_power" => math_power(num(a, "base"), num(a, "exponent")),
        "math_sqrt" => math_sqrt(num(a, "x")),
        "math_factorial" => math_factorial(a["n"].as_i64().unwrap_or(0)),
        "fib" => fib_fibonacci(a["n"].as_i64().unwrap_or(0)),
        "str_reverse" => Ok(str_reverse(a["s"].as_str().unwrap_or(""))),
        "str_upper" => Ok(str_upper(a["s"].as_str().unwrap_or(""))),
        "str_lower" => Ok(str_lower(a["s"].as_str().unwrap_or(""))),
        "str_palindrome" => Ok(str_palindrome(a["s"].as_str().unwrap_or(""))),
        "sort_quick" => sort_quick(arr(a)),
        "sort_bubble" => sort_bubble(arr(a)),
        "search_binary" => search_binary(arr(a), &a["target"]),
        "stat_mean" => stat_mean(arr(a)),
        "stat_median" => stat_median(arr(a)),
        "geo_circle" => geo_circle(num(a, "radius")),
        "geo_rect" => geo_rect(num(a, "length"), num(a, "width")),
        "c2f" => Ok(conv_c2f(num(a, "celsius"))),
        "f2c" => Ok(conv_f2c(num(a, "fahrenheit"))),
        "json_parse" => json_parse(a["json_string"].as_str().unwrap_or("")),
        "json_valid" => Ok(json_valid(a["json_string"].as_str().unwrap_or(""))),
        "email" => Ok(valid_email(a["email"].as_str().unwrap_or(""))),
        "is_prime" => prime_is_prime(a["n"].as_i64().unwrap_or(0)),
        "gen_primes" => prime_generate(a["limit"].as_i64().unwrap_or(0)),
        "list_unique" => list_unique(arr(a)),
        "list_flatten" => list_flatten(&a["nested_list"]),
        _ => Err(format!("unknown tool: {tool}")),
    };
    match out {
        Ok(s) => s,
        Err(e) => format!("ERR: {e}"),
    }
}

fn main() {
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = std::io::BufWriter::new(stdout.lock());
    let mut buf = String::new();
    loop {
        buf.clear();
        let n = {
            let mut lock = stdin.lock();
            lock.read_line(&mut buf).unwrap_or(0)
        };
        if n == 0 {
            break; // EOF：单发模式处理完退出，常驻模式由对端关闭
        }
        let line = buf.trim();
        if line.is_empty() {
            continue;
        }
        let req: Value = serde_json::from_str(line).unwrap_or(Value::Null);
        let _ = writeln!(out, "{}", dispatch(&req));
        let _ = out.flush();
    }
}

fn num(a: &Value, k: &str) -> f64 {
    a[k].as_f64().unwrap_or(0.0)
}

fn arr(a: &Value) -> &[Value] {
    match a.get("arr").or_else(|| a.get("data")).or_else(|| a.get("lst")) {
        Some(Value::Array(v)) => v,
        _ => &[],
    }
}
