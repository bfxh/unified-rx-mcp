// SPDX-FileCopyrightText: 2026 bfxh
// SPDX-License-Identifier: MIT
//! rx-telemetry — 遥测守护（常驻 serve 行协议 + CLI）。
//!
//! serve 协议（stdin 每行一条命令 JSON，stdout 每行一条响应 JSON）：
//!   {"cmd":"record","rec":{...Record 字段...}}  → {"ok":true,"flushed":0}
//!   {"cmd":"flush"}                             → {"ok":true,"flushed":N}
//!   {"cmd":"tail","n":20,"since_ts":0.0}        → {"ok":true,"data":[...]}
//!   {"cmd":"agg","since_ts":0.0}                → {"ok":true,"data":{...AggReport}}
//!   {"cmd":"status"}                            → {"ok":true,"data":{...}}
//!   {"cmd":"quit"}                              → {"ok":true} 退出
//!
//! CLI（调试/脚本直用）：
//!   rx-telemetry agg [path] [--since TS]
//!   rx-telemetry tail [path] [-n N]
//!   rx-telemetry status [path]

use rx_telemetry::{
    aggregate_file, default_telemetry_path, tail_file, Record, TelemetryStore,
};
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};
use std::path::Path;

fn respond(out: &mut impl Write, v: Value) {
    let line = v.to_string();
    let _ = writeln!(out, "{}", line);
    let _ = out.flush();
}

fn ok(out: &mut impl Write, data: Value) {
    respond(out, json!({"ok": true, "data": data}));
}

fn err(out: &mut impl Write, msg: &str) {
    respond(out, json!({"ok": false, "error": msg}));
}

fn handle_cmd(
    store: &mut TelemetryStore,
    cmd: &Value,
    out: &mut impl Write,
) -> bool {
    let Some(c) = cmd.get("cmd").and_then(|v| v.as_str()) else {
        err(out, "缺少 cmd 字段");
        return true;
    };
    match c {
        "record" => {
            let Some(rec) = cmd.get("rec") else {
                err(out, "record 命令缺少 rec 字段");
                return true;
            };
            match serde_json::from_value::<Record>(rec.clone()) {
                Ok(r) => {
                    store.push(r);
                    ok(out, json!({"buffered": store.buffered(),
                                   "flushed": store.flushed()}));
                }
                Err(e) => err(out, &format!("rec 解析失败: {}", e)),
            }
        }
        "flush" => match store.flush() {
            Ok(n) => ok(out, json!({"flushed": n})),
            Err(e) => err(out, &format!("flush 失败: {}", e)),
        },
        "tail" => {
            let _ = store.flush(); // 查询前先落盘缓冲（2026-08-16：否则记录还在内存，查询读文件看不到）
            let n = cmd.get("n").and_then(|v| v.as_u64()).unwrap_or(20) as usize;
            let path = cmd
                .get("path")
                .and_then(|v| v.as_str())
                .map(std::path::PathBuf::from)
                .unwrap_or_else(default_telemetry_path);
            match tail_file(&path, n) {
                Ok(recs) => ok(out, json!(recs)),
                Err(e) => err(out, &format!("tail 失败: {}", e)),
            }
        }
        "agg" => {
            let _ = store.flush(); // 同上：聚合前先落盘缓冲
            let since_ts = cmd.get("since_ts").and_then(|v| v.as_f64());
            let path = cmd
                .get("path")
                .and_then(|v| v.as_str())
                .map(std::path::PathBuf::from)
                .unwrap_or_else(default_telemetry_path);
            match aggregate_file(&path, since_ts) {
                Ok(rep) => ok(out, json!(rep)),
                Err(e) => err(out, &format!("agg 失败: {}", e)),
            }
        }
        "status" => {
            let _ = store.flush(); // 同上：状态前先落盘缓冲
            let path = store.path().to_path_buf();
            let file_size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
            ok(out, json!({
                "path": path.to_string_lossy(),
                "file_size": file_size,
                "buffered": store.buffered(),
                "flushed": store.flushed(),
                "cap": rx_telemetry::DEFAULT_CAP,
            }))
        }
        "quit" => {
            let _ = store.flush();
            respond(out, json!({"ok": true}));
            return false;
        }
        other => err(out, &format!("未知命令: {}", other)),
    }
    true
}

fn serve() -> io::Result<()> {
    let stdin = io::stdin();
    let mut out = io::stdout();
    let mut store = TelemetryStore::new();
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let cmd: Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(e) => {
                err(&mut out, &format!("命令解析失败: {}", e));
                continue;
            }
        };
        if !handle_cmd(&mut store, &cmd, &mut out) {
            break;
        }
    }
    // EOF：落盘收尾
    let _ = store.flush();
    Ok(())
}

fn cli_agg(path: &Path, since: Option<f64>) -> i32 {
    match aggregate_file(path, since) {
        Ok(rep) => {
            println!("{}", serde_json::to_string_pretty(&rep).unwrap_or_default());
            0
        }
        Err(e) => {
            eprintln!("agg 失败: {}", e);
            1
        }
    }
}

fn cli_tail(path: &Path, n: usize) -> i32 {
    match tail_file(path, n) {
        Ok(recs) => {
            for r in &recs {
                println!("{}", r.to_json_line());
            }
            0
        }
        Err(e) => {
            eprintln!("tail 失败: {}", e);
            1
        }
    }
}

fn main() -> io::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(|s| s.as_str()) {
        None | Some("serve") => serve(),
        Some("agg") => {
            let path = args
                .get(2)
                .map(std::path::PathBuf::from)
                .unwrap_or_else(default_telemetry_path);
            let since = args
                .iter()
                .position(|a| a == "--since")
                .and_then(|i| args.get(i + 1))
                .and_then(|v| v.parse::<f64>().ok());
            std::process::exit(cli_agg(&path, since));
        }
        Some("tail") => {
            let path = args
                .get(2)
                .map(std::path::PathBuf::from)
                .unwrap_or_else(default_telemetry_path);
            let n = args
                .iter()
                .position(|a| a == "-n")
                .and_then(|i| args.get(i + 1))
                .and_then(|v| v.parse::<usize>().ok())
                .unwrap_or(20);
            std::process::exit(cli_tail(&path, n));
        }
        Some("status") => {
            let path = args
                .get(2)
                .map(std::path::PathBuf::from)
                .unwrap_or_else(default_telemetry_path);
            let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
            println!(
                "{}",
                json!({"path": path.to_string_lossy(), "file_size": size})
            );
            std::process::exit(0);
        }
        Some(other) => {
            eprintln!("未知子命令: {}（可用: serve / agg / tail / status）", other);
            std::process::exit(2);
        }
    }
}
