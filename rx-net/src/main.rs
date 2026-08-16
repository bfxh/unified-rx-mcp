// SPDX-FileCopyrightText: 2026 bfxh
// SPDX-License-Identifier: MIT
//! rx-net — CLI 入口（弱网模拟代理）。
//!
//! 用法：
//!   rx-net --listen 127.0.0.1:8080 --target 127.0.0.1:80 \
//!          --delay 500 --loss 10 --reorder 5 --bandwidth 128
//!   rx-net --sanity             # 自检（echo 往返）
//!
//! 参数默认：delay=0ms loss=0% reorder=0% bandwidth=0（不限）。

use rx_net::{run_proxy, sanity_check, ChaosConfig};
use std::io::{self, BufRead};

const USAGE: &str = "\
用法: rx-net --listen <addr:port> --target <host:port> [混沌参数]
  --listen   代理监听地址（客户端连这里）
  --target   目标地址（转发到这里的真实服务）
  --delay    每块延迟毫秒（默认 0）
  --loss     丢包概率 %% 0-100（默认 0）
  --reorder  乱序概率 %% 0-100（默认 0）
  --bandwidth 带宽上限 KB/s（默认 0=不限）
  --sanity   自检（echo 往返验证）
";

fn parse_args() -> Result<(String, String, ChaosConfig, bool), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut listen = String::new();
    let mut target = String::new();
    let mut sanity = false;
    let mut cfg = ChaosConfig::default();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--listen" => {
                listen = args.get(i + 1).ok_or("--listen 缺值")?.clone();
                i += 2;
            }
            "--target" => {
                target = args.get(i + 1).ok_or("--target 缺值")?.clone();
                i += 2;
            }
            "--delay" => {
                cfg.delay_ms = args.get(i + 1).ok_or("--delay 缺值")?
                    .parse().map_err(|_| "--delay 非数字")?;
                i += 2;
            }
            "--loss" => {
                cfg.loss_pct = args.get(i + 1).ok_or("--loss 缺值")?
                    .parse().map_err(|_| "--loss 非数字")?;
                i += 2;
            }
            "--reorder" => {
                cfg.reorder_pct = args.get(i + 1).ok_or("--reorder 缺值")?
                    .parse().map_err(|_| "--reorder 非数字")?;
                i += 2;
            }
            "--bandwidth" => {
                cfg.bandwidth_kbps = args.get(i + 1).ok_or("--bandwidth 缺值")?
                    .parse().map_err(|_| "--bandwidth 非数字")?;
                i += 2;
            }
            "--sanity" => {
                sanity = true;
                i += 1;
            }
            other => return Err(format!("未知参数: {}", other)),
        }
    }
    if !sanity && (listen.is_empty() || target.is_empty()) {
        return Err("需要 --listen 与 --target（或 --sanity 自检）".into());
    }
    Ok((listen, target, cfg, sanity))
}

fn main() -> io::Result<()> {
    let (listen, target, cfg, sanity) = match parse_args() {
        Ok(v) => v,
        Err(e) => {
            eprintln!("参数错误: {}\n{}", e, USAGE);
            std::process::exit(2);
        }
    };
    if sanity {
        match sanity_check("127.0.0.1:0", cfg) {
            Ok(r) => {
                println!("sanity ok: {}", r);
                std::process::exit(0);
            }
            Err(e) => {
                eprintln!("sanity 失败: {}", e);
                std::process::exit(1);
            }
        }
    }
    println!("rx-net 混沌代理: {} -> {} (delay={}ms loss={}% reorder={}% bw={}KB/s)",
             listen, target, cfg.delay_ms, cfg.loss_pct,
             cfg.reorder_pct, cfg.bandwidth_kbps);
    // stdin 一行 "stop" 退出（net_chaos 工具用）。直接 exit：run_proxy 的
    // accept 循环是无限阻塞的，仅置 flag 进程不会退出（stop 工具要等超时强杀）。
    std::thread::spawn(|| {
        let stdin = io::stdin();
        for line in stdin.lock().lines() {
            if line.map(|l| l.trim() == "stop").unwrap_or(false) {
                std::process::exit(0);
            }
        }
    });
    match run_proxy(&listen, &target, cfg) {
        Ok(_) => Ok(()),
        Err(e) => Err(e),
    }
}
