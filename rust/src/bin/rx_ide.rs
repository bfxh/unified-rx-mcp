//! rx-ide —— ide 域原生化（S92 读双件 + S93 编辑面三件）：
//! outline / read_symbol / locate / context / rename。
//! 用法：
//!   rx-ide outline <file>
//!   rx-ide read_symbol <file> <name> [occurrence]
//!   rx-ide locate <path> <query> [max_files] [limit]
//!   rx-ide context <path> [cursor_line] [radius]
//!   rx-ide rename <root> <symbol> <new_name> [include_plan 0|1]
//! 输出：stdout 一行 JSON（与旧 Python 实现同构）。
//! 退出码：0 = 工具级结果（含 {"error": ...}，registry 统一转 ok:false）；
//!         2 = 用法错误 / 沙盒拒绝（Python 壳 raise ValueError，同旧实现包络）。
//! 沙盒：UNIFIED_RX_SANDBOX（fail-closed；"*" 全开；";" 分隔白名单）。
//! stdin 恒不接管内容（无大文本通道需求），Python 壳恒传 input=b"" 防继承宿主协议管道。
//! S93 注：locate/rename 的解析类失败按旧实现回落为工具级 {"error": ...}
//! （exit 0）；code_context 的 getsize 门在沙盒 resolve 之前（裸路径）。

use rxrs::ide;
use rxrs::json::Value;
use rxrs::sandbox::SandboxCfg;

const USAGE: &str = "用法: rx-ide outline <file> | read_symbol <file> <name> [occurrence] | \
locate <path> <query> [max_files] [limit] | context <path> [cursor_line] [radius] | \
rename <root> <symbol> <new_name> [include_plan 0|1]";

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
        "locate" => {
            if args.len() < 3 {
                return Err(USAGE.into());
            }
            let query = args.get(2).cloned().unwrap_or_default();
            // query 允许空串（旧实现按"query 为空"报工具级错误，不是用法错误）
            let max_files = match args.get(3) {
                Some(s) => s.parse::<i64>().map_err(|_| "max_files 必须是整数".to_string())?,
                None => 100,
            };
            let limit = match args.get(4) {
                Some(s) => s.parse::<i64>().map_err(|_| "limit 必须是整数".to_string())?,
                None => 10,
            };
            ide::locate_edit(&cfg, file, &query, max_files, limit)
        }
        "context" => {
            if args.len() < 2 {
                return Err(USAGE.into());
            }
            let cursor = match args.get(2) {
                Some(s) => s.parse::<i64>().map_err(|_| "cursor_line 必须是整数".to_string())?,
                None => 0,
            };
            let radius = match args.get(3) {
                Some(s) => s.parse::<i64>().map_err(|_| "radius 必须是整数".to_string())?,
                None => 30,
            };
            ide::code_context(&cfg, file, cursor, radius)
        }
        "rename" => {
            if args.len() < 4 {
                return Err(USAGE.into());
            }
            let symbol = args.get(2).cloned().unwrap_or_default();
            let new_name = args.get(3).cloned().unwrap_or_default();
            let include_plan = match args.get(4) {
                Some(s) => match s.as_str() {
                    "0" => false,
                    "1" => true,
                    _ => return Err("include_plan 必须是 0 或 1".into()),
                },
                None => false,
            };
            ide::ide_rename(&cfg, file, &symbol, &new_name, include_plan)
        }
        _ => Err(USAGE.into()),
    }
}
