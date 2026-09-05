//! rx-appops —— appaudit 域写面 app_clone/app_clean 原生化（S86）。
//! 用法：rx-appops clone <source_dir> <max_files> <max_bytes>
//!       rx-appops clean <target>
//! 输出：stdout 一行 JSON（与旧 Python 实现同构，不排序——顺序即遍历/插入序）。
//! 退出码：0 = 工具级结果（含 {"error": ...}，registry 统一转 ok:false）；
//!         2 = 用法错误（薄壳转 ValueError）。
//! 授权门留在 Python registry（requires_auth + __authorized），exe 无自授权面；
//! registry schema 门把 max_files/max_bytes 规范成 int 后才进 argv，
//! 直调时的非整数预算由 appclone::py_int 按 Python int() 语义兜底报错。

use rxrs::appclone;

const USAGE: &str =
    "用法: rx-appops clone <source_dir> <max_files> <max_bytes> | clean <target>";

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(op) = args.first().map(|s| s.as_str()) else {
        eprintln!("{USAGE}");
        std::process::exit(2);
    };
    let out = match op {
        "clone" => {
            let (Some(src), Some(mf), Some(mb)) =
                (args.get(1), args.get(2), args.get(3))
            else {
                eprintln!("{USAGE}");
                std::process::exit(2);
            };
            appclone::app_clone(src, mf, mb)
        }
        "clean" => match args.get(1) {
            Some(t) => appclone::app_clean(t),
            None => {
                eprintln!("{USAGE}");
                std::process::exit(2);
            }
        },
        _ => {
            eprintln!("{USAGE}");
            std::process::exit(2);
        }
    };
    println!("{}", out.to_json());
}
