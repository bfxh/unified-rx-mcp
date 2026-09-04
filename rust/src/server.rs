//! server —— rx-mcp：MCP stdio 协议层（Rust 化第一步，S78）。
//!
//! 形态：独立协议层 + 原生工具（当前原生面：rust_taint_scan，污点引擎同 crate）。
//! 不拉起任何子进程（动态拉起被 Mimosa 静态规则否决，且独立进程让 fuzz 靶更干净；
//! 转发代理形态随 S79 首批原生迁移再评估）。Python 工具箱照旧由 python server.py
//! 承载，attack 域 rust_taint_scan 工具以薄壳调 rx-taint.exe 复用同一引擎。
//!
//! 敌意输入纪律（fuzz 电池 S78 验收）：
//! - 每行先过自研 JSON 解析（限深 512）——解析失败回 -32700，顶层非对象回 -32600，
//!   绝不 panic、绝不退出；
//! - jsonrpc 字段缺省放行、非 "2.0" 拒（与 python 侧一致）；
//! - 单行上限 64MB（与 python server.py 对齐），超限丢弃整行；
//! - id 原样回显（Int/Float/Str 保真——JSON-RPC 宿主靠 id 配对）。

use std::io::{BufRead, BufReader, Write};
use std::sync::Arc;

use crate::json::{self, Value};
use crate::{sandbox, taint};

const MAX_LINE_BYTES: u64 = 64 * 1024 * 1024;
const PROTOCOL_VERSION: &str = "2025-03-26";

fn obj<const N: usize>(pairs: [(&str, Value); N]) -> Value {
    Value::Obj(pairs.iter().map(|(k, v)| (k.to_string(), v.clone())).collect())
}

fn error_resp(id: Value, code: i64, msg: &str) -> String {
    obj([
        ("jsonrpc", Value::Str("2.0".into())),
        ("id", id),
        (
            "error",
            obj([("code", Value::Int(code as i128)), ("message", Value::Str(msg.into()))]),
        ),
    ])
    .to_json()
}

/// MCP 工具结果信封（isError 语义与 python 侧一致）。
fn tool_result(text: String, is_error: bool) -> Value {
    obj([
        (
            "content",
            Value::Arr(vec![obj([
                ("type", Value::Str("text".into())),
                ("text", Value::Str(text)),
            ])]),
        ),
        ("isError", Value::Bool(is_error)),
    ])
}

fn native_tools() -> Value {
    Value::Arr(vec![obj([
        ("name", Value::Str("rust_taint_scan".into())),
        (
            "description",
            Value::Str("Rust 污点引擎（S78）：来源→汇点浅数据流扫 Python 代码；\
                        参数即来源（MCP 威胁模型），净化器 basename/_fs_resolve 识别"
                .into()),
        ),
        (
            "inputSchema",
            obj([
                ("type", Value::Str("object".into())),
                (
                    "properties",
                    obj([
                        ("root", obj([("type", Value::Str("string".into()))])),
                        (
                            "naive",
                            obj([("type", Value::Str("boolean".into()))]),
                        ),
                    ]),
                ),
                ("required", Value::Arr(vec![Value::Str("root".into())])),
            ]),
        ),
    ])])
}

/// 工具调用（原生面）。返回结果信封。
fn call_tool(args: &Value) -> Value {
    let name = args.get("name").and_then(|v| v.as_str()).unwrap_or("");
    if name != "rust_taint_scan" {
        return tool_result(format!("未知工具: {}", name), true);
    }
    let root = match args.get("arguments").and_then(|a| a.get("root")).and_then(|v| v.as_str())
    {
        Some(r) => r.to_string(),
        None => return tool_result("缺少 root 参数".into(), true),
    };
    let naive = args
        .get("arguments")
        .and_then(|a| a.get("naive"))
        .map(|v| matches!(v, Value::Bool(true)))
        .unwrap_or(false);
    // 沙盒钳制：fail-closed（未配置 = 拒绝），与 python 侧 _fs_resolve 同语义
    let root_path = std::path::Path::new(&root);
    let resolved = match sandbox::resolve(root_path) {
        Ok(p) => p,
        Err(e) => return tool_result(e, true),
    };
    let res = taint::scan_path(&resolved, naive);
    tool_result(taint::result_to_json(&res).to_json(), false)
}

/// 单请求分发。Some(响应串) = 需要回；None = 不回。
/// JSON-RPC 纪律：带 method 且无 id = 通知，无论方法名一律不回
/// （回包会污染宿主 id 配对；S78 首测实锤未知通知被回 id:null）。
fn dispatch(msg: &Value) -> Option<String> {
    // jsonrpc 字段校验（与 python 侧一致：缺省放行）
    if let Some(j) = msg.get("jsonrpc") {
        if j.as_str() != Some("2.0") {
            let id = msg.get("id").cloned().unwrap_or(Value::Null);
            return Some(error_resp(id, -32600, "Invalid Request: jsonrpc must be 2.0"));
        }
    }
    let method = msg.get("method").and_then(|m| m.as_str()).unwrap_or("");
    if !method.is_empty() && msg.get("id").is_none() {
        return None; // 通知不回
    }
    let id = msg.get("id").cloned().unwrap_or(Value::Null);
    let params = msg.get("params").cloned().unwrap_or(Value::Obj(vec![]));
    let reply = |result: Value| {
        Some(obj([
            ("jsonrpc", Value::Str("2.0".into())),
            ("id", id.clone()),
            ("result", result),
        ])
        .to_json())
    };
    match method {
        "initialize" => Some(obj([
            ("jsonrpc", Value::Str("2.0".into())),
            ("id", id),
            (
                "result",
                obj([
                    ("protocolVersion", Value::Str(PROTOCOL_VERSION.into())),
                    (
                        "capabilities",
                        obj([(
                            "tools",
                            obj([("listChanged", Value::Bool(true))]),
                        )]),
                    ),
                    (
                        "serverInfo",
                        obj([
                            ("name", Value::Str("unified-rx-rs".into())),
                            ("version", Value::Str(env!("CARGO_PKG_VERSION").into())),
                        ]),
                    ),
                ]),
            ),
        ])
        .to_json()),
        "ping" | "logging/setLevel" => reply(Value::Obj(vec![])),
        "tools/list" => reply(obj([("tools", native_tools())])),
        "tools/call" => reply(call_tool(&params)),
        "" => Some(error_resp(id, -32600, "Invalid Request: missing method")),
        _ => reply(obj([
            (
                "content",
                Value::Arr(vec![obj([
                    ("type", Value::Str("text".into())),
                    ("text", Value::Str(format!("UNKNOWN_METHOD {}", method))),
                ])]),
            ),
            ("isError", Value::Bool(true)),
        ])),
    }
}

use std::sync::Mutex;

type Out = Arc<Mutex<std::io::Stdout>>;

fn send(out: &Out, line: &str) {
    if let Ok(mut o) = out.lock() {
        let _ = o.write_all(line.as_bytes());
        let _ = o.write_all(b"\n");
        let _ = o.flush();
    }
}

/// 带上限读一行（到 \n）。Ok(None)=EOF；Ok(Some(vec)) 内容不含 \n；空 vec = 超限丢弃。
fn read_line_capped<R: BufRead>(r: &mut R, cap: u64) -> std::io::Result<Option<Vec<u8>>> {
    let mut buf: Vec<u8> = Vec::new();
    let mut byte = [0u8; 1];
    loop {
        match r.read(&mut byte)? {
            0 => {
                if buf.is_empty() {
                    return Ok(None);
                }
                return Ok(Some(buf)); // 尾行无换行
            }
            _ => {
                if byte[0] == b'\n' {
                    return Ok(Some(buf));
                }
                buf.push(byte[0]);
                if buf.len() as u64 > cap {
                    loop {
                        match r.read(&mut byte)? {
                            0 => return Ok(Some(Vec::new())),
                            _ => {
                                if byte[0] == b'\n' {
                                    return Ok(Some(Vec::new()));
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

/// 主入口：stdio 服务循环。敌意输入下绝不 panic 退出。
pub fn run() -> i32 {
    let out: Out = Arc::new(Mutex::new(std::io::stdout()));
    let stdin = std::io::stdin();
    let mut rin = BufReader::new(stdin.lock());
    loop {
        let line = match read_line_capped(&mut rin, MAX_LINE_BYTES) {
            Ok(Some(l)) => l,
            Ok(None) => return 0,
            Err(_) => return 0,
        };
        if line.is_empty() {
            continue; // 超限丢弃行
        }
        let text = String::from_utf8_lossy(&line).to_string();
        let t = text.trim();
        if t.is_empty() {
            continue;
        }
        let parsed = match json::parse(t) {
            Ok(v) => v,
            Err(e) => {
                send(&out, &error_resp(Value::Null, -32700, &format!("Parse error: {}", e)));
                continue;
            }
        };
        if !matches!(parsed, Value::Obj(_)) {
            send(&out, &error_resp(Value::Null, -32600, "Invalid Request: expected object"));
            continue;
        }
        if let Some(resp) = dispatch(&parsed) {
            send(&out, &resp);
        }
    }
}
