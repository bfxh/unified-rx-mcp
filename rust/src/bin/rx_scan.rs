//! rx-scan —— scan 域轻正则三工具（S82）+ bug_scan 原生化（S83）+ ast_scan 原生化（S84）：
//! std_check / ui_check / bug_locate / bug_scan / ast_scan。
//! 用法：
//!   rx-scan stdcheck <path> [max_files]
//!   rx-scan uicheck  <path> [max_files]
//!   rx-scan bugscan  <path> [max_files]   （S83：bug_scan 全量原生，见 rust/src/bug.rs）
//!   rx-scan astscan  <path> [max_files]   （S84：ast_scan 全量原生，默认 200——
//!                                          注意与 stdcheck 系的默认 100 不同）
//!   rx-scan buglocate <root> <error_text|->    （"-" = 改读 stdin 全文（lossy）——
//!                                               Windows 命令行 32767 码元上限装不下
//!                                               超大报错文本，薄壳对大文本走此通道）
//!   rx-scan resolve  <file>               （S107：单文件名字解析——引用→定义边，
//!                                           见 rust/src/nameres.rs；跨文件留给 S108）
//!   rx-scan sketch   <ng> <k> [threads]   （S119：批量 n-gram bottom-k 指纹，
//!                                           路径走 stdin 帧流，见 rust/src/sketch.rs）
//! 输出：stdout 一行 JSON（与旧 Python 实现同构，不排序——顺序即遍历序；
//! bugscan 例外：结果按 (severity, file, line) 稳定排序，与 Python 版一致）。
//! 退出码：0 = 工具级结果（含 {"error": ...}，registry 统一转 ok:false）；
//!         2 = 用法错误。
//! 无沙盒门：与 Python 版一致（纯读分析）。

use rxrs::astgrep;
use rxrs::astscan;
use rxrs::bug;
use rxrs::json::Value;
use rxrs::nameres;
use rxrs::scan;
use rxrs::sketch;
use std::io::Read;

const USAGE: &str = "用法: rx-scan stdcheck <path> [max_files] | uicheck <path> [max_files] | bugscan <path> [max_files] | astscan <path> [max_files] | buglocate <root> <error_text|-> | resolve <file> | sketch <ng> <k> [threads]";

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
    match sub {
        "stdcheck" | "uicheck" | "bugscan" => {
            let path = args.get(1).map(|s| s.as_str()).unwrap_or("");
            if path.is_empty() {
                return Err(USAGE.into());
            }
            // schema 校验后恒为整数；负数等价 0（Python count>=max 立即停走）；垃圾回退 100
            let mf = match args.get(2) {
                Some(s) => s.parse::<i64>().map(|n| n.max(0) as usize).unwrap_or(scan::MAX_FILES),
                None => scan::MAX_FILES,
            };
            match sub {
                "stdcheck" => Ok(scan::std_check(path, mf)),
                "uicheck" => Ok(scan::ui_check(path, mf)),
                _ => Ok(bug::bug_scan(path, mf)),
            }
        }
        "astscan" => {
            let path = args.get(1).map(|s| s.as_str()).unwrap_or("");
            if path.is_empty() {
                return Err(USAGE.into());
            }
            // ast_scan 的默认上限是 200（Python ast_scan 默认值），不是 scan::MAX_FILES=100
            let mf = match args.get(2) {
                Some(s) => s
                    .parse::<i64>()
                    .map(|n| n.max(0) as usize)
                    .unwrap_or(astscan::AST_MAX_FILES),
                None => astscan::AST_MAX_FILES,
            };
            Ok(astscan::ast_scan(path, mf))
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
        "resolve" => {
            let file = args.get(1).map(|s| s.as_str()).unwrap_or("");
            if file.is_empty() {
                return Err(USAGE.into());
            }
            let src = match std::fs::read(file) {
                Ok(b) => String::from_utf8_lossy(&b).into_owned(),
                Err(e) => {
                    return Ok(Value::Obj(vec![(
                        "error".into(),
                        Value::Str(format!("读取失败: {} ({})", file, e)),
                    )]));
                }
            };
            Ok(nameres::resolve_file(file, &src))
        }
        "astgrep" => {
            let pattern = args.get(1).map(|s| s.as_str()).unwrap_or("");
            let path = args.get(2).map(|s| s.as_str()).unwrap_or("");
            if pattern.is_empty() || path.is_empty() {
                return Err(USAGE.into());
            }
            let k = match args.get(3) {
                Some(s) => s.parse::<usize>().map(|n| n.max(1)).unwrap_or(50),
                None => 50,
            };
            Ok(astgrep::ast_grep(pattern, path, k))
        }
        "resolvedir" => {
            let root = args.get(1).map(|s| s.as_str()).unwrap_or("");
            if root.is_empty() {
                return Err(USAGE.into());
            }
            let mf = match args.get(2) {
                Some(s) => s.parse::<i64>().map(|n| n.max(0) as usize).unwrap_or(300),
                None => 300,
            };
            let p = std::path::Path::new(root);
            if !p.is_dir() {
                return Ok(Value::Obj(vec![(
                    "error".into(),
                    Value::Str(format!("不是目录: {}", root)),
                )]));
            }
            Ok(nameres::resolve_dir(p, mf))
        }
        "sketch" => {
            // S119：批量 bottom-k 指纹。ng/k 越界钳到合法域（与 Python 侧同口径）；
            // threads=0/缺省 = 自动（available_parallelism）。
            let ng = match args.get(1) {
                Some(s) => s.parse::<usize>().map(|n| n.clamp(1, sketch::MAX_NG)).unwrap_or(4),
                None => 4,
            };
            let k = match args.get(2) {
                Some(s) => s.parse::<usize>().map(|n| n.clamp(1, 4096)).unwrap_or(128),
                None => 128,
            };
            let threads = match args.get(3) {
                Some(s) => s.parse::<usize>().unwrap_or(0),
                None => 0,
            };
            let threads = if threads == 0 {
                std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
            } else {
                threads
            };
            let paths = sketch::read_path_frames(std::io::stdin())?;
            Ok(sketch::sketch_batch(&paths, ng, k, threads))
        }
        _ => Err(USAGE.into()),
    }
}
