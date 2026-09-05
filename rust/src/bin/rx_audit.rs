//! rx-audit —— appaudit 域 app_audit 原生化（S85）。
//! 用法：rx-audit <snapshot_dir> [with_asar(0|1，默认 1)]
//! 输出：stdout 一行 JSON（与旧 Python 实现同构，不排序——顺序即遍历/插入序）。
//! 退出码：0 = 工具级结果（含 {"error": ...}，registry 统一转 ok:false）；
//!         2 = 用法错误。
//! 沙盒门在 Rust 侧等价复刻（appaudit.rs::strictly_under，env
//! UNIFIED_RX_AUDIT_SANDBOX），fail-closed；app_clone/app_clean 仍留 Python
//! （写面+授权门，按"纯读先迁"纪律后置）。

use rxrs::appaudit;

const USAGE: &str = "用法: rx-audit <snapshot_dir> [with_asar(0|1)]";

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(dir) = args.first() else {
        eprintln!("{USAGE}");
        std::process::exit(2);
    };
    let with_asar = match args.get(1).map(|s| s.as_str()) {
        None => true,
        Some("0") => false,
        Some(_) => true, // schema 校验后恒为 bool；非 "0" 一律按真（与 Python truthy 同向）
    };
    let out = appaudit::app_audit(dir, with_asar);
    println!("{}", out.to_json());
}
