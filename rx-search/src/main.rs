// SPDX-FileCopyrightText: 2026 bfxh
// SPDX-License-Identifier: MIT
//! rx-search — 常驻 serve 行协议（对齐 rx-core/rx-telemetry）。
//!
//! stdin 每行命令 JSON，stdout 每行响应 JSON：
//!   {"cmd":"index","root":...,"limit":50000} → {"ok":true,"data":{"docs":N}}
//!   {"cmd":"search","q":...,"k":20}          → {"ok":true,"data":[...Hit]}
//!   {"cmd":"status"}                          → {"ok":true,"data":{docs,terms}}
//!   {"cmd":"quit"}                            → {"ok":true} 退出
//!
//! CLI：rx-search index <root> / rx-search search <root> <query> [-k N]

use rx_search::{build_index, search, Index};
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};
use std::path::PathBuf;

fn respond(out: &mut impl Write, v: Value) {
    let _ = writeln!(out, "{}", v);
    let _ = out.flush();
}

fn main() -> io::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(|s| s.as_str()) {
        None | Some("serve") => serve(),
        Some("index") => {
            let root = args.get(2).map(PathBuf::from).unwrap_or_default();
            let idx = build_index(&root, 50_000);
            println!("{}", json!({"ok": true, "docs": idx.n_docs}));
            std::process::exit(0);
        }
        Some("search") => {
            let root = args.get(2).map(PathBuf::from).unwrap_or_default();
            let q = args.get(3).map(|s| s.as_str()).unwrap_or("");
            let k = args.iter().position(|a| a == "-k")
                .and_then(|i| args.get(i + 1))
                .and_then(|v| v.parse::<usize>().ok())
                .unwrap_or(20);
            let idx = build_index(&root, 50_000);
            let hits = search(&idx, q, k);
            println!("{}", serde_json::to_string_pretty(&hits).unwrap_or_default());
            std::process::exit(0);
        }
        Some(other) => {
            eprintln!("未知子命令: {}（可用: serve / index / search）", other);
            std::process::exit(2);
        }
    }
}

fn serve() -> io::Result<()> {
    let stdin = io::stdin();
    let mut out = io::stdout();
    // 常驻内存索引（index 命令后驻留——重复 search 不重建）
    let mut idx: Option<Index> = None;
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
                respond(&mut out, json!({"ok": false, "error": format!("命令解析失败: {}", e)}));
                continue;
            }
        };
        let Some(c) = cmd.get("cmd").and_then(|v| v.as_str()) else {
            respond(&mut out, json!({"ok": false, "error": "缺少 cmd"}));
            continue;
        };
        match c {
            "index" => {
                let root = cmd.get("root").and_then(|v| v.as_str())
                    .map(PathBuf::from)
                    .unwrap_or_default();
                let limit = cmd.get("limit").and_then(|v| v.as_u64())
                    .unwrap_or(50_000) as usize;
                idx = Some(build_index(&root, limit));
                let n = idx.as_ref().map(|i| i.n_docs).unwrap_or(0);
                respond(&mut out, json!({"ok": true, "data": {"docs": n}}));
            }
            "search" => {
                let q = cmd.get("q").and_then(|v| v.as_str()).unwrap_or("");
                let k = cmd.get("k").and_then(|v| v.as_u64()).unwrap_or(20) as usize;
                match &idx {
                    Some(i) => respond(&mut out, json!({"ok": true, "data": search(i, q, k)})),
                    None => respond(&mut out, json!({"ok": false, "error": "未索引——先发 index 命令"})),
                }
            }
            "status" => {
                let n = idx.as_ref().map(|i| i.n_docs).unwrap_or(0);
                let terms = idx.as_ref().map(|i| i.df.len()).unwrap_or(0);
                respond(&mut out, json!({"ok": true, "data": {"docs": n, "terms": terms}}));
            }
            "quit" => {
                respond(&mut out, json!({"ok": true}));
                break;
            }
            other => respond(&mut out, json!({"ok": false, "error": format!("未知命令: {}", other)})),
        }
    }
    Ok(())
}
