//! rx-scan —— scan 域轻正则三工具（S82）：std_check / ui_check / bug_locate。
//! 用法：
//!   rx-scan stdcheck <path> [max_files]
//!   rx-scan uicheck  <path> [max_files]
//!   rx-scan buglocate <root> <error_text|->    （"-" = 改读 stdin 全文（lossy）——
//!                                               Windows 命令行 32767 码元上限装不下
//!                                               超大报错文本，薄壳对大文本走此通道）
//! 输出：stdout 一行 JSON（与 tools/scan.py 旧实现同构，不排序——顺序即遍历序）。
//! 退出码：0 = 工具级结果（含 {"error": ...}，registry 统一转 ok:false）；
//!         2 = 用法错误。
//! 无沙盒门：与 Python 版一致（纯读分析）。

use rxrs::json::Value;
use rxrs::scan;
use std::io::Read;

const USAGE: &str = "用法: rx-scan stdcheck <path> [max_files] | uicheck <path> [max_files] | buglocate <root> <error_text|->";

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
    match sub {
        "stdcheck" | "uicheck" => {
            let path = args.get(1).map(|s| s.as_str()).unwrap_or("");
            if path.is_empty() {
                return Err(USAGE.into());
            }
            // schema 校验后恒为整数；负数等价 0（Python count>=max 立即停走）；垃圾回退 100
            let mf = match args.get(2) {
                Some(s) => s.parse::<i64>().map(|n| n.max(0) as usize).unwrap_or(scan::MAX_FILES),
                None => scan::MAX_FILES,
            };
            if sub == "stdcheck" {
                Ok(scan::std_check(path, mf))
            } else {
                Ok(scan::ui_check(path, mf))
            }
        }
        "buglocate" => {
            let root = args.get(1).map(|s| s.as_str()).unwrap_or("").to_string();
            // error_text 允许为空串（Python 空 text → 0 候选合法），只拒缺参
            let Some(text) = args.get(2).cloned() else {
                return Err(USAGE.into());
            };
            if root.is_empty() {
                return Err(USAGE.into());
            }
            let mut text = text;
            if text == "-" {
                let mut buf = Vec::new();
                if std::io::stdin().read_to_end(&mut buf).is_ok() {
                    text = String::from_utf8_lossy(&buf).into_owned();
                }
            }
            Ok(scan::bug_locate(&root, &text))
        }
        _ => Err(USAGE.into()),
    }
}
