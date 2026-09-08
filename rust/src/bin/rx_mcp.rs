//! rx-mcp —— MCP stdio 协议层（Rust 化入口，S78）。
//! 用法：由宿主作为 MCP 服务器拉起（stdin/stdout JSON-RPC 行协议）。
//! 环境变量：UNIFIED_RX_SANDBOX（沙盒白名单，fail-closed）。

fn main() {
    // --version：S94 机器对账——selftest 用它核对 exe 与 SERVER_VERSION 不漂移
    if std::env::args().any(|a| a == "--version") {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let code = rxrs::server::run();
    std::process::exit(code);
}
