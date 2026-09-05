//! rx-semantic —— code_semantic 的 Rust 原生 CLI（S81）。
//! 用法：rx-semantic <root> <query> [search|related] [k]
//!       query 为 "-" 时改读 stdin 全文（lossy）——Windows 命令行 32767 码元
//!       上限装不下超大查询，薄壳对大查询走此通道。
//! 输出：stdout 一行 JSON（query / mode[/anchor] / total / hits）。
//! 退出码：0 = 正常返回（含 {"error": "不是目录: ..."}，registry 统一转 ok:false）；
//!         2 = 用法错误（缺参数 / mode 非法）。
//! 无沙盒门：与 Python 版一致（S75 审计定性：纯读分析=本职）。

use rxrs::json::Value;
use std::io::Read;
use std::path::Path;

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
    let root = args.first().map(|s| s.as_str()).unwrap_or("");
    let mut query = args.get(1).map(|s| s.as_str()).unwrap_or("").to_string();
    if root.is_empty() {
        return Err("用法: rx-semantic <root> <query> [search|related] [k]".into());
    }
    // 空 query 合法（原实现 search 模式返回 total=0），不设 query 必填门
    if query == "-" {
        let mut buf = Vec::new();
        if std::io::stdin().read_to_end(&mut buf).is_ok() {
            query = String::from_utf8_lossy(&buf).into_owned();
        }
    }
    let mode = args.get(2).map(|s| s.as_str()).unwrap_or("search");
    if mode != "search" && mode != "related" {
        return Err("mode 必须是 search 或 related".into());
    }
    let k = args.get(3).and_then(|s| s.parse::<usize>().ok()).unwrap_or(8);
    Ok(rxrs::sem::code_semantic(Path::new(root), &query, mode, k))
}
