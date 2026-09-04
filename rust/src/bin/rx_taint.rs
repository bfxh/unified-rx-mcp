//! rx-taint —— Rust 污点引擎 CLI（S78）。
//! 用法：rx-taint <根目录|文件> [--naive]
//! 输出：stdout 一行 JSON（files_scanned / findings / errors）。
//! 沙盒：UNIFIED_RX_SANDBOX（fail-closed；"*" 全开；";" 分隔白名单）。
//! --naive = 模式匹配基线（S73 重放对比用，见 spec/VULN-HUNTING.md P1-a）。

use std::path::Path;

fn main() {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let naive = args.iter().any(|a| a == "--naive");
    args.retain(|a| a != "--naive");
    let out = match run(&args, naive) {
        Ok(v) => v.to_json(),
        Err(e) => rxrs::json::Value::Obj(vec![("error".into(), rxrs::json::Value::Str(e))])
            .to_json(),
    };
    println!("{}", out);
}

fn run(args: &[String], naive: bool) -> Result<rxrs::json::Value, String> {
    let target = match args.first() {
        Some(t) => t.clone(),
        None => return Err("用法: rx-taint <根目录|文件> [--naive]".into()),
    };
    // 沙盒钳制：fail-closed（未配置 = 拒绝），与 python 侧 _fs_resolve 同语义
    let resolved = rxrs::sandbox::resolve(Path::new(&target))?;
    let res = rxrs::taint::scan_path(&resolved, naive);
    Ok(rxrs::taint::result_to_json(&res))
}
