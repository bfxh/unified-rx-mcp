//! rx-sys —— 混合架构（P/E 核）调度观测与引导（S148/S149）。
//! 用法：rx-sys topology
//!       rx-sys threads <pid>
//!       rx-sys procs [名称子串]
//!       rx-sys steer <pid|名称子串> [--preset render|background]
//!             [--class p|e|any] [--priority highest|above|normal|below|lowest|idle]
//!             [--eco on|off] [--hard] [--tids a,b,c]
//!       rx-sys privilege        （尝试为本进程开启 SeDebugPrivilege）
//!       rx-sys affinity <pid>
//!       rx-sys devices
//! 输出：stdout 一行 JSON（registry 统一转 ok:false/结果对象）。
//! 退出码：0 = 工具级结果（含 {"error": ...}）；2 = 用法错误（薄壳转 ValueError）。
//! 授权门留在 Python registry（steer 为执行·写档 requires_auth）；exe 无自授权面。
//! S149：steer 通用化（预设只是显式档位的别名——不只是游戏，任意应用/工具可引导）；
//! 目标可用 pid 或可执行名子串；访问被拒时自动尝试 SeDebugPrivilege 并重试一次。

use rxrs::json::Value;
use rxrs::sysinfo;


const USAGE: &str = "用法: rx-sys topology | threads <pid> | procs [子串] | \
steer <pid|名称子串> [--preset render|background] [--class p|e|any] \
[--priority highest|above|normal|below|lowest|idle] [--eco on|off] [--hard] [--tids a,b] | \
privilege | affinity <pid> | devices";

fn main() {
    if std::env::args().any(|a| a == "--version") {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(op) = args.first().map(|s| s.as_str()) else {
        eprintln!("{USAGE}");
        std::process::exit(2);
    };
    let out = match op {
        "topology" => sysinfo::topology(),
        "devices" => sysinfo::devices(),
        "procs" => sysinfo::procs(args.get(1).map(|s| s.as_str()).unwrap_or("")),
        "privilege" => {
            let (ok, note) = sysinfo::enable_debug_privilege();
            Value::Obj(vec![("ok".into(), Value::Bool(ok)),
                            ("note".into(), Value::Str(note)),
                            ("engine".into(), Value::Str("rust:sysinfo".into()))])
        }
        "threads" | "affinity" => {
            let Some(pid) = args.get(1).and_then(|s| s.parse::<u32>().ok()) else {
                eprintln!("{USAGE}");
                std::process::exit(2);
            };
            if op == "threads" { sysinfo::threads(pid) } else { sysinfo::process_affinity(pid) }
        }
        "steer" => {
            let Some(target) = args.get(1) else {
                eprintln!("{USAGE}");
                std::process::exit(2);
            };
            let mut preset: Option<&str> = None;
            let mut class_want: Option<&str> = None;
            let mut priority: Option<&str> = None;
            let mut eco: Option<bool> = None;
            let mut hard = false;
            let mut tids: Vec<u32> = Vec::new();
            let mut i = 2usize;
            while i < args.len() {
                let a = args[i].as_str();
                let val = |k: usize| args.get(k + 1).map(|s| s.as_str());
                match a {
                    "--preset" => { preset = val(i); i += 2; }
                    "--class" => { class_want = val(i); i += 2; }
                    "--priority" => { priority = val(i); i += 2; }
                    "--eco" => {
                        eco = match val(i) {
                            Some("on") => Some(true),
                            Some("off") => Some(false),
                            _ => {
                                eprintln!("{USAGE}");
                                std::process::exit(2);
                            }
                        };
                        i += 2;
                    }
                    "--hard" => { hard = true; i += 1; }
                    "--tids" => {
                        match val(i) {
                            Some(list) => {
                                for part in list.split(',') {
                                    match part.trim().parse::<u32>() {
                                        Ok(t) => tids.push(t),
                                        Err(_) => {
                                            eprintln!("{USAGE}");
                                            std::process::exit(2);
                                        }
                                    }
                                }
                            }
                            None => {
                                eprintln!("{USAGE}");
                                std::process::exit(2);
                            }
                        }
                        i += 2;
                    }
                    _ => {
                        eprintln!("{USAGE}");
                        std::process::exit(2);
                    }
                }
            }
            sysinfo::steer(target, &sysinfo::SteerOpts {
                preset, class_want, priority, eco, hard, tids: &tids,
            })
        }
        _ => {
            eprintln!("{USAGE}");
            std::process::exit(2);
        }
    };
    println!("{}", out.to_json());
}
