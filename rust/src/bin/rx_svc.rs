//! rx-svc —— 常驻服务（S151）：省掉每次 CreateProcess 的启动成本（约 8ms/次）。
//!
//! **stdio 常驻**（首版走 TCP 环回，实测被本机安全栈间歇拦截：min 0.4ms /
//! 中位 15ms——弃用）：由 Python 宿主 `Popen` 起本进程，请求/应答走
//! stdin/stdout 各一行 JSON——
//!   请求：{"domain":"sys|fs|taint","argv":["topology"]}
//!   应答：{"rc":0|2,"out":"<与 CLI stdout 逐字节同形的 JSON 行>"}
//!   rc 语义与各 CLI 一致：0=工具级结果；2=用法/沙盒拒绝（Python 壳转 ValueError）。
//!
//! 无监听端口、无令牌——攻击面为零（只有父进程能写它的 stdin）；父进程退出
//! → stdin EOF → 本进程自动退出（无孤儿）。空闲由宿主控制（宿主可随时结束）。
//!
//! **质量不变是硬约束**：服务不做任何缓存/状态——每个请求重跑与 CLI 完全相同的
//! 库函数；金标准（bench/cli_bench.py）与 tests/test_s151_svc.py 逐字节比对
//! "服务回包 vs CLI stdout"（两份解析各一，防漂移）。

use rxrs::json::Value;
use rxrs::sandbox::SandboxCfg;
use std::path::Path;
use std::io::{BufRead, Write};


// ---------------- 请求分发（与各 CLI 同函数的另一份解析；由逐字节比对防漂移） ----------------

fn run_op(domain: &str, argv: &[String]) -> (i32, String) {
    match domain {
        "sys" => {
            let op = argv.first().map(|s| s.as_str()).unwrap_or("");
            let v = match op {
                "topology" => rxrs::sysinfo::topology(),
                "devices" => rxrs::sysinfo::devices(),
                "procs" => rxrs::sysinfo::procs(
                    argv.get(1).map(|s| s.as_str()).unwrap_or("")),
                "threads" | "affinity" => {
                    let Some(pid) = argv.get(1).and_then(|s| s.parse::<u32>().ok())
                    else { return (2, err("用法: sys threads <pid>")) };
                    if op == "threads" { rxrs::sysinfo::threads(pid) }
                    else { rxrs::sysinfo::process_affinity(pid) }
                }
                "privilege" => {
                    let (ok, note) = rxrs::sysinfo::enable_debug_privilege();
                    Value::Obj(vec![("ok".into(), Value::Bool(ok)),
                                    ("note".into(), Value::Str(note)),
                                    ("engine".into(), Value::Str("rust:sysinfo".into()))])
                }
                "steer" => {
                    let Some(target) = argv.get(1) else {
                        return (2, err("用法: sys steer <target> [flags]"));
                    };
                    let mut preset = None; let mut class_want = None;
                    let mut priority = None; let mut eco = None; let mut hard = false;
                    let mut tids: Vec<u32> = Vec::new();
                    let mut i = 2usize;
                    while i < argv.len() {
                        let a = argv[i].as_str();
                        let val = |k: usize| argv.get(k + 1).map(|s| s.as_str());
                        match a {
                            "--preset" => { preset = val(i); i += 2; }
                            "--class" => { class_want = val(i); i += 2; }
                            "--priority" => { priority = val(i); i += 2; }
                            "--eco" => {
                                eco = match val(i) { Some("on") => Some(true),
                                                    Some("off") => Some(false), _ => None };
                                i += 2;
                            }
                            "--hard" => { hard = true; i += 1; }
                            "--tids" => {
                                if let Some(list) = val(i) {
                                    for part in list.split(',') {
                                        match part.trim().parse::<u32>() {
                                            Ok(t) => tids.push(t),
                                            Err(_) => return (2, err("用法: sys steer --tids a,b")),
                                        }
                                    }
                                }
                                i += 2;
                            }
                            _ => return (2, err("用法: sys steer <target> [flags]")),
                        }
                    }
                    rxrs::sysinfo::steer(target, &rxrs::sysinfo::SteerOpts {
                        preset, class_want, priority, eco, hard, tids: &tids })
                }
                _ => return (2, err("用法: sys <topology|devices|procs|threads|affinity|privilege|steer>")),
            };
            (0, v.to_json())
        }
        "fs" => {
            let op = argv.first().map(|s| s.as_str()).unwrap_or("");
            let path = argv.get(1).map(|s| s.as_str()).unwrap_or("");
            if op.is_empty() { return (2, err("用法: fs <read|stat|list> <path> [depth]")) }
            if path.is_empty() { return (2, err("path 必填")) }
            let cfg = SandboxCfg::from_env();
            let r = match op {
                "read" => rxrs::fs::op_read(&cfg, path),
                "stat" => rxrs::fs::op_stat(&cfg, path),
                "list" => {
                    let depth = argv.get(2).and_then(|s| s.parse::<i64>().ok()).unwrap_or(1);
                    rxrs::fs::op_list(&cfg, path, depth)
                }
                // write 走 stdin 语义，不经服务（保持原路径）
                _ => return (2, err("用法: fs <read|stat|list> <path> [depth]（write 不经服务）")),
            };
            match r { Ok(v) => (0, v.to_json()), Err(e) => (2, err(&e)) }
        }
        "taint" => {
            let naive = argv.iter().any(|a| a == "--naive");
            let no_cross = argv.iter().any(|a| a == "--no-cross");
            let rest: Vec<String> = argv.iter()
                .filter(|a| a.as_str() != "--naive" && a.as_str() != "--no-cross")
                .cloned().collect();
            let Some(target) = rest.first() else {
                return (2, err("用法: taint <根目录|文件> [--naive] [--no-cross]"));
            };
            let resolved = match rxrs::sandbox::resolve(Path::new(target)) {
                Ok(p) => p, Err(e) => return (2, err(&e)),
            };
            let res = rxrs::taint::scan_path_opts(&resolved, naive, !no_cross);
            (0, rxrs::taint::result_to_json(&res).to_json())
        }
        "scan" => {
            let op = argv.first().map(|s| s.as_str()).unwrap_or("");
            let path = argv.get(1).map(|s| s.as_str()).unwrap_or("");
            if path.is_empty() {
                return (2, err("用法: scan <bugscan|stdcheck|uicheck|secrets> <path> …"));
            }
            match op {
                "bugscan" | "stdcheck" | "uicheck" => {
                    let mf = match argv.get(2) {
                        Some(x) => x.parse::<i64>().map(|n| n.max(0) as usize)
                            .unwrap_or(rxrs::scan::MAX_FILES),
                        None => rxrs::scan::MAX_FILES,
                    };
                    let v = match op {
                        "stdcheck" => rxrs::scan::std_check(path, mf),
                        "uicheck" => rxrs::scan::ui_check(path, mf),
                        _ => rxrs::bug::bug_scan(path, mf),
                    };
                    (0, v.to_json())
                }
                "secrets" => {
                    let mf = argv.get(2).and_then(|s| s.parse::<usize>().ok()).unwrap_or(3000);
                    let mk = argv.get(3).and_then(|s| s.parse::<usize>().ok()).unwrap_or(512);
                    let me = argv.get(4).and_then(|s| s.parse::<f64>().ok()).unwrap_or(4.5);
                    let mr = argv.get(5).and_then(|s| s.parse::<usize>().ok()).unwrap_or(200);
                    let inc = argv.get(6).map(|s| s.as_str()).unwrap_or("");
                    (
                        0,
                        rxrs::secrets::secrets_scan(
                            std::path::Path::new(path), mf, mk, me, mr, inc).to_json(),
                    )
                }
                _ => (2, err("用法: scan <bugscan|stdcheck|uicheck|secrets> <path> …")),
            }
        }
        "search" => {
            let root = argv.first().map(|s| s.as_str()).unwrap_or("");
            let query = argv.get(1).map(|s| s.as_str()).unwrap_or("");
            if root.is_empty() {
                return (2, err("用法: search <root> <query> [k]（长查询走 CLI stdin）"));
            }
            if query.is_empty() || query == "-" {
                return (2, err("query 必填（长查询=经 stdin 的原路径，服务不接）"));
            }
            let k = argv.get(2).and_then(|s| s.parse::<usize>().ok()).unwrap_or(10);
            (0, rxrs::search::code_search(std::path::Path::new(root), query, k).to_json())
        }
        "semantic" => {
            let root = argv.first().map(|s| s.as_str()).unwrap_or("");
            let query = argv.get(1).map(|s| s.as_str()).unwrap_or("");
            if root.is_empty() || query == "-" {
                return (2, err("用法: semantic <root> <query> [search|related] [k]（长查询走 CLI）"));
            }
            let mode = argv.get(2).map(|s| s.as_str()).unwrap_or("search");
            if mode != "search" && mode != "related" {
                return (2, err("mode 必须是 search 或 related"));
            }
            let k = argv.get(3).and_then(|s| s.parse::<usize>().ok()).unwrap_or(8);
            (
                0,
                rxrs::sem::code_semantic(std::path::Path::new(root), query, mode, k).to_json(),
            )
        }
        _ => (2, err("未知域（服务支持 sys/fs/scan/search/semantic/taint）")),
    }
}

fn err(msg: &str) -> String {
    Value::Obj(vec![("error".into(), Value::Str(msg.into()))]).to_json()
}

// ---------------- stdio 常驻主循环 ----------------

fn serve_stdio() {
    // S152：常驻进程内启用文件内容缓存（键含 size+mtime_ns；CLI 路径不启用）
    rxrs::rcache::enable();
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        let t = line.trim();
        if t.is_empty() {
            continue;
        }
        let reply = match rxrs::json::parse(t) {
            Ok(req) => {
                let domain = match req.get("domain") {
                    Some(Value::Str(d)) => d.clone(), _ => String::new() };
                let argv_owned: Vec<String> = match req.get("argv") {
                    Some(Value::Arr(a)) => a.iter().filter_map(|v| match v {
                        Value::Str(s) => Some(s.clone()), _ => None }).collect(),
                    _ => Vec::new(),
                };
                if matches!(argv_owned.first().map(|s| s.as_str()), Some("__ping__")) {
                    "{\"rc\":0,\"out\":\"{\\\"pong\\\":true}\"}".to_string()
                } else {
                    let (rc, o) = run_op(&domain, &argv_owned);
                    Value::Obj(vec![("rc".into(), Value::Int(rc as i128)),
                                    ("out".into(), Value::Str(o))]).to_json()
                }
            }
            Err(_) => "{\"rc\":2,\"out\":\"{\\\"error\\\":\\\"bad request json\\\"}\"}".to_string(),
        };
        if out.write_all(reply.as_bytes()).is_err()
            || out.write_all(b"
").is_err()
            || out.flush().is_err()
        {
            break;      // 宿主管道断了 → 退出（无孤儿）
        }
    }
    // stdin EOF（宿主退出）→ 正常收尾
}

fn main() {
    if std::env::args().any(|a| a == "--version") {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return;
    }
    match std::env::args().nth(1).as_deref() {
        Some("serve") => serve_stdio(),
        _ => {
            eprintln!("用法: rx-svc serve | --version（stdio 常驻协议见文件头注释）");
            std::process::exit(2);
        }
    }
}
