//! rx-mcp —— MCP stdio 协议层（Rust 化入口，S78）。
//! 用法：由宿主作为 MCP 服务器拉起（stdin/stdout JSON-RPC 行协议）。
//! 环境变量：UNIFIED_RX_SANDBOX（沙盒白名单，fail-closed）。

fn main() {
    let code = rxrs::server::run();
    std::process::exit(code);
}
