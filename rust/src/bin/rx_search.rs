//! rx-search —— code_search 的 Rust 原生 CLI（S80）。
//! 用法：rx-search <root> <query> [k]
//!       query 为 "-" 时改读 stdin 全文（lossy）——Windows 命令行 32767 码元
//!       上限装不下超大查询，薄壳对大查询走此通道（S81 补）。
//! 输出：stdout 一行 JSON（query / total / hits[file,line,score,snippet]）。
//! 退出码：0 = 正常返回（含 {"error": "不是目录: ..."}，registry 统一转 ok:false）；
//!         2 = 用法错误（缺参数）。
//! 无沙盒门：与 Python 版一致（S75 审计定性：纯读分析=本职）。

use rxrs::json::Value;
use std::io::Read;
use std::path::Path;

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
    let root = args.first().map(|s| s.as_str()).unwrap_or("");
    let mut query = args.get(1).map(|s| s.as_str()).unwrap_or("").to_string();
    if root.is_empty() {
        return Err("用法: rx-search <root> <query> [k]".into());
    }
    // "-" = 从 stdin 整读；替换后仍受空 query 拒绝门约束
    if query == "-" {
        let mut buf = Vec::new();
        if std::io::stdin().read_to_end(&mut buf).is_ok() {
            query = String::from_utf8_lossy(&buf).into_owned();
        }
    }
    if query.is_empty() {
        return Err("query 必填".into());
    }
    // schema 校验后 k 恒为正整数；垃圾值回退 10（与 Python 默认同）
    let k = args.get(2).and_then(|s| s.parse::<usize>().ok()).unwrap_or(10);
    Ok(rxrs::search::code_search(Path::new(root), &query, k))
}
