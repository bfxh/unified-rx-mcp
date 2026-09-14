//! rx-taint —— Rust 污点引擎 CLI（S78；S128 跨文件链）。
//! 用法：rx-taint <根目录|文件> [--naive] [--no-cross]
//! 输出：stdout 一行 JSON（files_scanned / findings / errors / cross_file_findings /
//!       cross_skipped_ambiguous）。
//! 沙盒：UNIFIED_RX_SANDBOX（fail-closed；"*" 全开；";" 分隔白名单）。
//! --naive = 模式匹配基线（S73 重放对比用，见 spec/VULN-HUNTING.md P1-a）。
//! --no-cross = 关闭跨文件传播（逐字节回到 S78 语义，A/B 对照用）。

use std::path::Path;

fn main() {
    if std::env::args().any(|a| a == "--version") {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let naive = args.iter().any(|a| a == "--naive");
    let no_cross = args.iter().any(|a| a == "--no-cross");
    args.retain(|a| a != "--naive" && a != "--no-cross");
    let out = match run(&args, naive, !no_cross) {
        Ok(v) => v.to_json(),
        Err(e) => rxrs::json::Value::Obj(vec![("error".into(), rxrs::json::Value::Str(e))])
            .to_json(),
    };
    println!("{}", out);
}

fn run(args: &[String], naive: bool, cross: bool) -> Result<rxrs::json::Value, String> {
    let target = match args.first() {
        Some(t) => t.clone(),
        None => return Err("用法: rx-taint <根目录|文件> [--naive] [--no-cross]".into()),
    };
    // 沙盒钳制：fail-closed（未配置 = 拒绝），与 python 侧 _fs_resolve 同语义
    let resolved = rxrs::sandbox::resolve(Path::new(&target))?;
    let res = rxrs::taint::scan_path_opts(&resolved, naive, cross);
    Ok(rxrs::taint::result_to_json(&res))
}
