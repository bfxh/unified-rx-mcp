//! astgrep —— ast-grep 结构搜索（S111，ADVANCES P2 第 10 项：可选外部引擎）。
//!
//! 定位：ast-grep 是**可选外部引擎**（模式即代码，$VAR/$$$ 通配）。本模块只做
//! 探测 + argv 直调（无 shell）：
//! - 探测：PATH 上试 `ast-grep` 与 `sg`（以 `--version` 探活，NotFound 即下一个）；
//!   不支持 env 覆盖（外部引擎按 PATH 约定，避免把"可执行文件路径"变成配置面）；
//! - 未安装 → 返回清晰错误 + 安装提示（不静默降级，本仓红线）；
//! - 只读搜索，不做 rewrite；`--json=stream` 逐行解析后归一化为本仓形状。
//!
//! 实现注记：`Command::new` 一律**字面量**调用点（静态门要求；变量命令名会被
//! 判为命令注入面）——因此 `ast-grep`/`sg` 各写一处，不做参数化封装。

use std::process::{Command, Output};

use crate::json::{self, Value};

const MAX_PATTERN: usize = 4096;

fn err_obj(msg: &str) -> Value {
    Value::Obj(vec![("error".into(), Value::Str(msg.into()))])
}

/// 探测引擎：返回 true 表示 `sg`（短名），false 表示 `ast-grep`，None 表示未装。
fn find_engine() -> Option<bool> {
    if Command::new("ast-grep")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        return Some(false);
    }
    if Command::new("sg")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        return Some(true);
    }
    None
}

/// 执行搜索（两个字面量调用点，见文件头实现注记）。
fn invoke(short: bool, pattern: &str, path: &str) -> std::io::Result<Output> {
    if short {
        Command::new("sg")
            .args(["run", "-p", pattern, "--json=stream", path])
            .output()
    } else {
        Command::new("ast-grep")
            .args(["run", "-p", pattern, "--json=stream", path])
            .output()
    }
}

/// 结构搜索入口：pattern/path/k 由 Python 薄壳传入（path 已过沙盒）。
pub fn ast_grep(pattern: &str, path: &str, k: usize) -> Value {
    if pattern.is_empty() || pattern.len() > MAX_PATTERN {
        return err_obj("pattern 必填且 ≤4096 字符");
    }
    if !std::path::Path::new(path).exists() {
        return err_obj(&format!("路径不存在: {}", path));
    }
    let Some(short) = find_engine() else {
        return err_obj(
            "ast-grep 未安装（可选外部引擎）：npm i -g @ast-grep/cli 或 cargo install ast-grep，\
             装后本工具自动启用；不装时结构搜索可用 ast_scan（自研 AST-lite，覆盖较窄）",
        );
    };
    let out = match invoke(short, pattern, path) {
        Ok(o) => o,
        Err(e) => return err_obj(&format!("ast-grep 启动失败: {}", e)),
    };
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
    // ast-grep 退出码：0=有命中 1=无命中 其他=错误
    let code = out.status.code().unwrap_or(-1);
    if code != 0 && code != 1 {
        let tail: String = stderr.trim().chars().rev().take(300).collect::<String>()
            .chars().rev().collect();
        return err_obj(&format!("ast-grep 执行失败（exit={}）: {}", code, tail));
    }
    let mut hits: Vec<Value> = Vec::new();
    for line in stdout.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(v) = json::parse(line) else { continue };
        let file = match v.get("file") {
            Some(Value::Str(s)) => s.clone(),
            _ => String::new(),
        };
        let line_no = match v.get("range").and_then(|r| r.get("start")).and_then(|s| s.get("line")) {
            Some(Value::Int(i)) => i + 1,
            _ => 0,
        };
        let text: String = match v.get("text") {
            Some(Value::Str(s)) => s.chars().take(200).collect(),
            _ => String::new(),
        };
        hits.push(Value::Obj(vec![
            ("file".into(), Value::Str(file)),
            ("line".into(), Value::Int(line_no)),
            ("text".into(), Value::Str(text)),
        ]));
        if hits.len() >= k.max(1) {
            break;
        }
    }
    Value::Obj(vec![
        ("engine".into(), Value::Str("ast-grep".into())),
        ("pattern".into(), Value::Str(pattern.into())),
        ("total".into(), Value::Int(hits.len() as i128)),
        ("hits".into(), Value::Arr(hits)),
        ("note".into(), Value::Str(
            "可选外部引擎（模式即代码，$VAR/$$$ 通配）；只读搜索，不做 rewrite".into())),
    ])
}
