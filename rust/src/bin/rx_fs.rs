//! rx-fs —— 文件层四工具 CLI（S79 读面 / S90 写面收官）。
//! 用法：rx-fs <read|stat|list|write> <path> [depth]
//!       write 的内容不走 argv（Windows 命令行 32767 码元上限），从 stdin 读原始字节，
//!       恒读到 EOF——Python 壳恒传 input（空内容即空串）防继承宿主 MCP 协议管道。
//! 输出：stdout 一行 JSON。
//! 退出码：0 = 工具级结果（含 result.error 的正常返回，与 Python 侧返回 dict 同包络）；
//!         2 = resolve 层拒绝（沙盒越界/未配置/path 必填），Python 壳 raise ValueError
//!         → registry `ok:false`（与旧实现抛 ValueError 同包络）。
//! 沙盒：UNIFIED_RX_SANDBOX（fail-closed；"*" 全开；";" 分隔白名单）。

use rxrs::json::Value;
use rxrs::sandbox::SandboxCfg;

fn main() {
    if std::env::args().any(|a| a == "--version") {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (code, out) = match run(&args) {
        Ok(v) => (0, v.to_json()),
        Err(e) => (2, Value::Obj(vec![("error".into(), Value::Str(e))]).to_json()),
    };
    println!("{}", out);
    std::process::exit(code);
}

fn run(args: &[String]) -> Result<Value, String> {
    let op = args.first().map(|s| s.as_str()).unwrap_or("");
    let path = args.get(1).map(|s| s.as_str()).unwrap_or("");
    if op.is_empty() {
        return Err("用法: rx-fs <read|stat|list|write> <path> [depth]".into());
    }
    if path.is_empty() {
        // 与 tools/fs.py::_resolve 首道校验逐字对齐
        return Err("path 必填".into());
    }
    let cfg = SandboxCfg::from_env();
    match op {
        "read" => rxrs::fs::op_read(&cfg, path),
        "stat" => rxrs::fs::op_stat(&cfg, path),
        "list" => {
            // Python 侧 int(depth or 1) 同语义：缺省/垃圾值回退 1，钳到 0..=4
            let depth = args.get(2).and_then(|s| s.parse::<i64>().ok()).unwrap_or(1);
            rxrs::fs::op_list(&cfg, path, depth)
        }
        "write" => {
            // 内容走 stdin 原始字节（读到 EOF；壳侧恒传 input 防继承宿主协议管道）
            let mut buf = Vec::new();
            std::io::Read::read_to_end(&mut std::io::stdin(), &mut buf)
                .map_err(|e| format!("读取 stdin 失败: {}", e))?;
            rxrs::fs::op_write(&cfg, path, &buf)
        }
        other => Err(format!("未知操作: {}（应为 read|stat|list|write）", other)),
    }
}
