//! rxrs —— unified-rx 的 Rust 化地基（S78 起，spec/VULN-HUNTING.md 五·Rust 迁移路线图）。
//!
//! 纪律：零第三方 crate（Cargo [dependencies] 恒空），与 Python 侧"纯 stdlib"同等级。
//! 模块：
//! - `json`    手写 JSON 解析/序列化（限深 512 防栈溢出，fuzz 电池深嵌套用例的靶）
//! - `sandbox` 沙盒钳制（等价复刻 tools/fs.py::_resolve 语义：fail-closed / "*" / ; 分隔）
//! - `taint`   污点引擎（Python 子集词法 + 缩进作用域 + 来源→汇点浅数据流）
//! - `server`  MCP stdio 协议层（rx-mcp：initialize 拦截 + 全量转发代理到 python server.py）

pub mod json;
pub mod sandbox;
pub mod server;
pub mod taint;
