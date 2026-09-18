//! rx-sys —— 混合架构（P/E 核）调度观测与引导（S148）。
//! 用法：rx-sys topology
//!       rx-sys threads <pid>
//!       rx-sys steer <pid> render|background [--hard] [tid ...]
//!       rx-sys affinity <pid>
//!       rx-sys devices
//! 输出：stdout 一行 JSON（registry 统一转 ok:false/结果对象）。
//! 退出码：0 = 工具级结果（含 {"error": ...}）；2 = 用法错误（薄壳转 ValueError）。
//! 授权门留在 Python registry（steer 为执行·写档 requires_auth）；exe 无自授权面。

use rxrs::sysinfo;

const USAGE: &str = "用法: rx-sys topology | threads <pid> | \
steer <pid> render|background [--hard] [tid ...] | affinity <pid> | devices";

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
        "threads" | "affinity" => {
            let Some(pid) = args.get(1).and_then(|s| s.parse::<u32>().ok()) else {
                eprintln!("{USAGE}");
                std::process::exit(2);
            };
            if op == "threads" { sysinfo::threads(pid) } else { sysinfo::process_affinity(pid) }
        }
        "steer" => {
            let (Some(pid), Some(profile)) = (args.get(1).and_then(|s| s.parse::<u32>().ok()),
                                              args.get(2)) else {
                eprintln!("{USAGE}");
                std::process::exit(2);
            };
            let hard = args.iter().any(|a| a == "--hard");
            let mut tids: Vec<u32> = Vec::new();
            for a in args.iter().skip(3) {
                if a == "--hard" {
                    continue;
                }
                match a.parse::<u32>() {
                    Ok(t) => tids.push(t),
                    Err(_) => {
                        eprintln!("{USAGE}");
                        std::process::exit(2);
                    }
                }
            }
            if tids.is_empty() {
                // 缺省=该进程全部线程（薄壳/用户可显式给 TID 缩小面）
                let t = sysinfo::threads(pid);
                if let Some(rxrs::json::Value::Arr(ts)) = t.get("threads") {
                    for e in ts {
                        if let Some(rxrs::json::Value::Int(v)) = e.get("tid") {
                            tids.push(*v as u32);
                        }
                    }
                }
            }
            sysinfo::steer(pid, profile, &tids, hard)
        }
        _ => {
            eprintln!("{USAGE}");
            std::process::exit(2);
        }
    };
    println!("{}", out.to_json());
}
