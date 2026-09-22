//! par.rs —— 并行度策略（S160 建，**S167 改"按机器来"**）。
//!
//! 单一入口：所有分块并行的 `available_parallelism()` 取值集中到这里，好处有三：
//! 1. **开关可测**：`UNIFIED_RX_NO_PAR=1` 强制串行——性能门（`scripts/perf_gate.py`）
//!    据此做**同机并行 vs 串行比值**判据（不依赖机器绝对速度，CI 也安全）；
//! 2. **策略一处**：与降级（拿不到并行度 → 1）语义统一；
//! 3. **S167：并行度按机器来，不再被调用点写死的 8 卡住**——24 核机上固定 8 等于闲置 2/3
//!    （实测：大语料下 bugscan/secrets 只到 31–38% 效率）。调用点统一传 `0`。

/// 并行度：`UNIFIED_RX_NO_PAR` 存在即 1（强制串行）；**`cap == 0` ⇒ 用满可用并行度**；
/// 否则 min(可用, cap)（留给"某处确实要更低并行度"的显式场景）；取不到并行度时降级为 1。
pub fn par_degree(cap: usize) -> usize {
    if std::env::var("UNIFIED_RX_NO_PAR").is_ok() {
        return 1;
    }
    let avail = std::thread::available_parallelism().map(|x| x.get()).unwrap_or(1);
    if cap == 0 {
        avail
    } else {
        avail.min(cap)
    }
}

/// 按**工作量**再收敛一次：每线程至少 `min_items` 个条目 ⇒ 别为几十个文件起满核线程。
/// （分块仍由调用点负责；这里只回答"该开几个线程"。）
pub fn par_degree_for(items: usize, min_items: usize, cap: usize) -> usize {
    let want = items / min_items.max(1);
    par_degree(cap).min(want.max(1))
}
