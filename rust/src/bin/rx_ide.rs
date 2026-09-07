//! rx-ide —— ide 域读面原生化（S92）：outline / read_symbol。
//! 用法：
//!   rx-ide outline <file>
//!   rx-ide read_symbol <file> <name> [occurrence]
//! 输出：stdout 一行 JSON（与旧 Python 实现同构）。
//! 退出码：0 = 工具级结果（含 {"error": ...}，registry 统一转 ok:false）；
//!         2 = 用法错误 / 沙盒拒绝（Python 壳 raise ValueError，同旧实现包络）。
//! 沙盒：UNIFIED_RX_SANDBOX（fail-closed；"*" 全开；";" 分隔白名单）。
//! stdin 恒不接管内容（无大文本通道需求），Python 壳恒传 input=b"" 防继承宿主协议管道。

use rxrs::ide;
use rxrs::json::Value;
use rxrs::sandbox::SandboxCfg;

const USAGE: &str = "用法: rx-ide outline <file> | read_symbol <file> <name> [occurrence]";

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (code, out) = match run(&args) {
        Ok(v) => (0, v.to_json()),
        Err(e) => (2, Value::Obj(vec![("error".into(), Value::Str(e))]).to_json()),
    };
    println!("{}", out);
    std::process::exit(code);
}

fn run(args: &[String]) -> Result<Value, String> {
    let sub = args.first().map(|s| s.as_str()).unwrap_or("");
    let file = args.get(1).map(|s| s.as_str()).unwrap_or("");
    let cfg = SandboxCfg::from_env();
    match sub {
        "outline" => ide::outline(&cfg, file),
        "read_symbol" => {
            let name = args.get(2).cloned().unwrap_or_default();
            if args.len() < 3 {
                return Err(USAGE.into());
            }
            // name 允许为空串（旧实现按"符号不存在"报，不是用法错误）
            let occ = match args.get(3) {
                Some(s) => s.parse::<i64>().map_err(|_| "occurrence 必须是整数".to_string())?,
                None => 1,
            };
            ide::read_symbol(&cfg, file, &name, occ)
        }
        _ => Err(USAGE.into()),
    }
}
