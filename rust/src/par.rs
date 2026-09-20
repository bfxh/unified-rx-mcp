//! par.rs —— 并行度策略（S160）。
//!
//! 单一入口：所有分块并行的 `available_parallelism()` 取值集中到这里，好处有二：
//! 1. **开关可测**：`UNIFIED_RX_NO_PAR=1` 强制串行——性能门（`scripts/perf_gate.py`）
//!    据此做**同机并行 vs 串行比值**判据（不依赖机器绝对速度，CI 也安全）；
//! 2. **策略一处**：上限（min(max_threads)）与降级（拿不到并行度 → 1）语义统一。

/// 并行度：`UNIFIED_RX_NO_PAR` 存在即 1（强制串行）；否则 min(可用并行度, max_threads)，
/// 取不到并行度时降级为 1（串行路径，行为与旧版一致）。
pub fn par_degree(max_threads: usize) -> usize {
    if std::env::var("UNIFIED_RX_NO_PAR").is_ok() {
        return 1;
    }
    std::thread::available_parallelism()
        .map(|x| x.get())
        .unwrap_or(1)
        .min(max_threads.max(1))
}
