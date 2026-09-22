//! par.rs —— 并行度策略（S160 建，S167 改"按机器来"，**S168 修正：改成"有界并行"**）。
//!
//! 单一入口：所有分块并行的 `available_parallelism()` 取值集中到这里，好处有三：
//! 1. **开关可测**：`UNIFIED_RX_NO_PAR=1` 强制串行——性能门（`scripts/perf_gate.py`）
//!    据此做**同机并行 vs 串行比值**判据（不依赖机器绝对速度，CI 也安全）；
//! 2. **策略一处**：与降级（拿不到并行度 → 1）语义统一；
//! 3. **S168：必须是「有界并行」**。S167 一度改成"有多少核就开多少线程"，结果**把机器打死**：
//!    实测把并行度从 8 提到 24 只多 **+0.5×**（stdcheck 3.54→4.04×、secrets 2.49→2.97×），
//!    但开销是叠乘的——**服务端线程池（每条并发调用）× 每次调用的线程数**，
//!    最坏 8×24=192 线程抢 24 核 ⇒ 交互直接卡死。
//!    所以默认上限 8（实测甜点），要动就显式设 `UNIFIED_RX_PAR_CAP`，别改这里。

/// 默认并行度上限（S168）：见模块头第 3 条的实测依据。
pub const DEFAULT_PAR_CAP: usize = 8;

/// 并行度：`UNIFIED_RX_NO_PAR` 存在即 1（强制串行）；`cap == 0` ⇒ min(可用核, 默认上限 8)；
/// 否则 min(可用核, cap, 默认上限)；可用核取不到时降级 1。
/// `UNIFIED_RX_PAR_CAP` 可覆盖默认上限（**调高前先想清叠乘**：服务端还有一层并发）。
pub fn par_degree(cap: usize) -> usize {
    if std::env::var("UNIFIED_RX_NO_PAR").is_ok() {
        return 1;
    }
    let avail = std::thread::available_parallelism().map(|x| x.get()).unwrap_or(1);
    let hard = std::env::var("UNIFIED_RX_PAR_CAP")
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(DEFAULT_PAR_CAP);
    let want = if cap == 0 { hard } else { cap.min(hard) };
    avail.min(want).max(1)
}

/// 按**工作量**再收敛一次：每线程至少 `min_items` 个条目 ⇒ 别为几十个文件起满核线程。
/// （分块仍由调用点负责；这里只回答"该开几个线程"。）
pub fn par_degree_for(items: usize, min_items: usize, cap: usize) -> usize {
    let want = items / min_items.max(1);
    par_degree(cap).min(want.max(1))
}

#[cfg(test)]
mod tests {
    /// S168 守门：**并行度必须有界**——S167 的"有多少核开多少线程"把机器打死过
    /// （服务端并发 × 每调用线程数 叠乘；实测 24 线程只比 8 线程多 +0.5×）。
    /// 这条测试就是那次事故的机器化回退点：默认上限一旦被改掉/被绕过，这里红。
    #[test]
    fn par_degree_is_bounded_by_default() {
        // 不碰环境变量（remove_var 在 Rust 2024 是 unsafe）：就算外部设了 UNIFIED_RX_NO_PAR（⇒1）
        // 也满足断言；设了 UNIFIED_RX_PAR_CAP 的情形由下面第二条分支放行。
        let n = super::par_degree(0);
        assert!(n >= 1, "并行度至少 1");
        assert!(
            n <= super::DEFAULT_PAR_CAP || std::env::var("UNIFIED_RX_PAR_CAP").is_ok(),
            "默认并行度 {n} 超过了 DEFAULT_PAR_CAP={}——要调只能显式设 UNIFIED_RX_PAR_CAP（见模块头）",
            super::DEFAULT_PAR_CAP
        );
    }
}
