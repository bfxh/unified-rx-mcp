//! S94 bin 版本门测试：9 个 exe 的 --version 必须输出 CARGO_PKG_VERSION。
//!
//! Python 侧对账（server.py selftest EXE_TAG 行）拿真 exe 比对 SERVER_VERSION；
//! 本文件在 cargo test 侧锁同一契约：源码里的门漏改/删除即红。
//! CARGO_BIN_EXE_<name> 由 cargo 集成测试机制注入，指向刚构建好的 bin。

use std::process::Command;

const BINS: [&str; 9] = [
    "rx-mcp",
    "rx-taint",
    "rx-fs",
    "rx-ide",
    "rx-search",
    "rx-semantic",
    "rx-scan",
    "rx-audit",
    "rx-appops",
];

#[test]
fn bin_version_gate_reports_pkg_version() {
    let want = env!("CARGO_PKG_VERSION");
    for bin in BINS {
        let path = match bin {
            "rx-mcp" => env!("CARGO_BIN_EXE_rx-mcp"),
            "rx-taint" => env!("CARGO_BIN_EXE_rx-taint"),
            "rx-fs" => env!("CARGO_BIN_EXE_rx-fs"),
            "rx-ide" => env!("CARGO_BIN_EXE_rx-ide"),
            "rx-search" => env!("CARGO_BIN_EXE_rx-search"),
            "rx-semantic" => env!("CARGO_BIN_EXE_rx-semantic"),
            "rx-scan" => env!("CARGO_BIN_EXE_rx-scan"),
            "rx-audit" => env!("CARGO_BIN_EXE_rx-audit"),
            "rx-appops" => env!("CARGO_BIN_EXE_rx-appops"),
            _ => unreachable!(),
        };
        let out = Command::new(path)
            .arg("--version")
            .output()
            .unwrap_or_else(|e| panic!("{bin} --version 执行失败: {e}"));
        assert!(out.status.success(), "{bin} --version 退出码异常");
        let got = String::from_utf8_lossy(&out.stdout).trim().to_string();
        assert_eq!(got, want, "{bin} 版本漂移");
    }
}
