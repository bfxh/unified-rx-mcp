// SPDX-FileCopyrightText: 2026 bfxh
// SPDX-License-Identifier: MIT
//! rx-net — 弱网模拟（Clumsy 式，Windows）。
//!
//! 方案：**本地 TCP 代理**（Clumsy 的 WinDivert 需要驱动+管理员；代理式
//! 纯 std 零依赖、无需管理员、可测试）。客户端连代理端口 → 代理转发到
//! 目标，转发途中注入混沌：
//!   - delay_ms    每块数据延迟（毫秒）
//!   - loss_pct    按概率丢块（0-100）
//!   - reorder_pct 按概率乱序（块队列交换）
//!   - bandwidth_kbps 令牌桶限速（超出按比例 sleep 补偿）
//!
//! 用法：
//!   rx-net --listen 127.0.0.1:8080 --target 127.0.0.1:80 \
//!          --delay 500 --loss 10 --reorder 5 --bandwidth 128
//!
//! 集成：net_chaos 工具经 subprocess 启动/停止（进程句柄管理）。

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::{Duration, Instant};

/// 混沌配置。
#[derive(Clone, Debug)]
pub struct ChaosConfig {
    /// 每块转发延迟（毫秒）
    pub delay_ms: u64,
    /// 丢块概率 0-100
    pub loss_pct: f64,
    /// 乱序概率 0-100（块队列内交换顺序）
    pub reorder_pct: f64,
    /// 带宽上限 KB/s（0 = 不限）
    pub bandwidth_kbps: u64,
}

impl Default for ChaosConfig {
    fn default() -> Self {
        ChaosConfig {
            delay_ms: 0,
            loss_pct: 0.0,
            reorder_pct: 0.0,
            bandwidth_kbps: 0,
        }
    }
}

/// 丢包判定（纯函数，可测）。
pub fn should_drop(rng: &mut impl FnMut() -> f64, loss_pct: f64) -> bool {
    loss_pct > 0.0 && rng() * 100.0 < loss_pct
}

/// 乱序判定（纯函数，可测）。
pub fn should_reorder(rng: &mut impl FnMut() -> f64, reorder_pct: f64) -> bool {
    reorder_pct > 0.0 && rng() * 100.0 < reorder_pct
}

/// 限速：发送 n 字节按带宽应补睡多久（纯函数，可测）。
pub fn bandwidth_sleep(bytes: u64, kbps: u64) -> Duration {
    if kbps == 0 || bytes == 0 {
        return Duration::ZERO;
    }
    let bits = bytes as f64 * 8.0;
    let capacity_bps = kbps as f64 * 1000.0;
    let secs = bits / capacity_bps;
    if secs <= 0.0 {
        Duration::ZERO
    } else {
        Duration::from_secs_f64(secs)
    }
}

/// 乱序重排（纯函数，可测）：对块队列按概率交换相邻块。
pub fn maybe_reorder(
    chunks: &mut [Vec<u8>],
    rng: &mut impl FnMut() -> f64,
    reorder_pct: f64,
) {
    if chunks.len() < 2 || !should_reorder(rng, reorder_pct) {
        return;
    }
    let i = ((rng() * chunks.len() as f64) as usize).min(chunks.len() - 2);
    chunks.swap(i, i + 1);
}

const CHUNK: usize = 16 * 1024;

/// 单向转发（src → dst），带混沌注入。返回转发的块数与字节数。
pub fn forward_chaos(
    mut src: TcpStream,
    mut dst: TcpStream,
    cfg: &ChaosConfig,
    rng: &mut impl FnMut() -> f64,
) -> (u64, u64) {
    let mut buf = vec![0u8; CHUNK];
    let mut blocks = 0u64;
    let mut bytes = 0u64;
    loop {
        let n = match src.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => n,
            Err(_) => break,
        };
        if should_drop(rng, cfg.loss_pct) {
            continue; // 丢块
        }
        let mut chunks = vec![buf[..n].to_vec()];
        maybe_reorder(&mut chunks, rng, cfg.reorder_pct);
        for c in &chunks {
            if cfg.delay_ms > 0 {
                std::thread::sleep(Duration::from_millis(cfg.delay_ms));
            }
            if cfg.bandwidth_kbps > 0 {
                let s = bandwidth_sleep(c.len() as u64, cfg.bandwidth_kbps);
                if !s.is_zero() {
                    std::thread::sleep(s);
                }
            }
            if dst.write_all(c).is_err() {
                return (blocks, bytes);
            }
        }
        blocks += chunks.len() as u64;
        bytes += n as u64;
    }
    let _ = dst.flush();
    (blocks, bytes)
}

/// 启动混沌代理：listen 端口接受连接 → 双向转发（各自带混沌）。
pub fn run_proxy(
    listen_addr: &str,
    target_addr: &str,
    cfg: ChaosConfig,
) -> std::io::Result<u64> {
    let listener = TcpListener::bind(listen_addr)?;
    let mut conns = 0u64;
    for stream in listener.incoming() {
        let Ok(client) = stream else { continue };
        let Ok(target) = TcpStream::connect(target_addr) else { continue };
        conns += 1;
        let cfg1 = cfg.clone();
        let cfg2 = cfg.clone();
        let c1 = client.try_clone().expect("clone client");
        let t1 = target.try_clone().expect("clone target");
        // 客户端→目标
        std::thread::spawn(move || {
            let _ = forward_chaos(c1, t1, &cfg1, &mut || rand_f64());
        });
        // 目标→客户端
        std::thread::spawn(move || {
            let _ = forward_chaos(target, client, &cfg2, &mut || rand_f64());
        });
    }
    Ok(conns)
}

/// 简单随机（std 无 rand——用时间+线性同余）。
static mut RNG_STATE: u64 = 0x853c49e6748fea9b;

fn rand_f64() -> f64 {
    unsafe {
        // xorshift64
        let mut x = RNG_STATE;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        RNG_STATE = x;
        (x >> 11) as f64 / (1u64 << 53) as f64
    }
}

/// 校验混沌代理可用（测试用）：内部启动 echo + 代理 + 往返。
pub fn sanity_check(listen: &str, cfg: ChaosConfig) -> std::io::Result<String> {
    // echo server
    let echo = TcpListener::bind("127.0.0.1:0")?;
    let echo_addr = echo.local_addr()?;
    std::thread::spawn(move || {
        for mut s in echo.incoming().flatten() {
            let mut buf = [0u8; 4096];
            while let Ok(n) = s.read(&mut buf) {
                if n == 0 {
                    break;
                }
                if s.write_all(&buf[..n]).is_err() {
                    break;
                }
            }
        }
    });
    // 代理（先 bind 探测空闲端口再释放，run_proxy 自己 bind——
    // 不释放会 AddrInUse：代理线程静默失败，客户端连接永远无人 accept）
    let probe = TcpListener::bind(listen)?;
    let proxy_addr = probe.local_addr()?;
    drop(probe);
    std::thread::spawn(move || {
        if let Err(e) = run_proxy(&proxy_addr.to_string(), &echo_addr.to_string(), cfg) {
            eprintln!("rx-net sanity: proxy 启动失败: {e}");
        }
    });
    // 客户端往返
    let mut c = TcpStream::connect(proxy_addr)?;
    let t0 = Instant::now();
    c.write_all(b"hello chaos")?;
    let mut buf = [0u8; 64];
    let n = c.read(&mut buf)?;
    let elapsed = t0.elapsed();
    if &buf[..n] != b"hello chaos" {
        return Err(std::io::Error::other("echo 数据不一致"));
    }
    Ok(format!("{}ms/{}B", elapsed.as_millis(), n))
}

// ─────────────────────────────────────────────────────────────
// 测试
// ─────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn rng_seq(vals: Vec<f64>) -> impl FnMut() -> f64 {
        let mut i = 0;
        move || {
            let v = vals[i % vals.len()];
            i += 1;
            v
        }
    }

    #[test]
    fn drop_logic() {
        // 0% 永不丢
        assert!(!should_drop(&mut rng_seq(vec![0.99]), 0.0));
        // 100% 必丢
        assert!(should_drop(&mut rng_seq(vec![0.0]), 100.0));
        // 50%：rng=0.4 丢，rng=0.6 不丢
        assert!(should_drop(&mut rng_seq(vec![0.4]), 50.0));
        assert!(!should_drop(&mut rng_seq(vec![0.6]), 50.0));
    }

    #[test]
    fn reorder_logic() {
        let mut chunks = vec![vec![1], vec![2], vec![3]];
        maybe_reorder(&mut chunks, &mut rng_seq(vec![0.1, 0.1]), 100.0);
        // 交换了相邻块
        assert_ne!(chunks, vec![vec![1], vec![2], vec![3]]);
        // 0% 不乱序
        let mut chunks2 = vec![vec![1], vec![2], vec![3]];
        maybe_reorder(&mut chunks2, &mut rng_seq(vec![0.1]), 0.0);
        assert_eq!(chunks2, vec![vec![1], vec![2], vec![3]]);
    }

    #[test]
    fn bandwidth_calc() {
        // 128kbps = 16KB/s，传 16KB ≈ 1s（按比特计算：128*1000 bps）
        let s = bandwidth_sleep(16 * 1024, 128);
        assert!(s.as_millis() >= 900 && s.as_millis() <= 1100, "{:?}", s);
        // 不限速 = 0
        assert_eq!(bandwidth_sleep(100, 0), Duration::ZERO);
        // 带宽足够大 → 微量延迟但不为零
        assert!(bandwidth_sleep(1, 1_000_000) > Duration::ZERO);
    }

    #[test]
    fn proxy_roundtrip() {
        // 无混沌代理：数据一致
        let r = sanity_check("127.0.0.1:0", ChaosConfig::default());
        assert!(r.is_ok(), "{:?}", r);
    }

    #[test]
    fn proxy_with_delay() {
        // 延迟 100ms：往返 ≥ 100ms 且数据一致
        let cfg = ChaosConfig { delay_ms: 100, ..Default::default() };
        let r = sanity_check("127.0.0.1:0", cfg).unwrap();
        let ms: u64 = r.split("ms").next().unwrap().parse().unwrap();
        assert!(ms >= 100, "延迟注入失效: {}ms", ms);
    }
}
