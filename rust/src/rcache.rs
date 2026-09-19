//! rcache —— 进程内文件内容缓存（S152）：**只由常驻服务显式启用**。
//!
//! 动机（S152 实测，进程内计时）：重活命令全是"走树读文件"的 I/O 成本——
//! code_search 113ms / code_semantic 198ms / bug_scan 138ms / secrets 236ms /
//! rust_taint_scan 385ms（仓库自身为语料）。按次起进程时缓存无意义（S80 已
//! 退役过一代），**常驻服务让缓存重新成立**。
//!
//! 质量边界（"不变质量"硬约束）：
//! - 缓存**不改变任何计算结果**——只是把 `std::fs::read` 的字节复用；键为
//!   (路径, 文件大小, mtime_ns)，任一变化即回源重读（NTFS mtime 精度 100ns；
//!   改用同尺寸同 mtime 内容的窗口极小，且 CLI 路径无缓存可对照）；
//! - **CLI 路径永不启用**（只有 `rx-svc serve` 启动时调 `enable()`）——按次
//!   调用行为与从前逐字节一致；
//! - 上限（文件数/总字节）到顶即整体清空（简单、可解释；不做 LRU 花活）。
//!   默认上限可用 env `UNIFIED_RX_RCACHE_MB` 调整（0 = 关闭）。

use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::UNIX_EPOCH;

static ENABLED: AtomicBool = AtomicBool::new(false);
static BYTES: AtomicUsize = AtomicUsize::new(0);
static CACHE: Mutex<Option<HashMap<String, Entry>>> = Mutex::new(None);

struct Entry {
    size: u64,
    mtime_ns: u128,
    data: Vec<u8>,
}

fn max_bytes() -> usize {
    let mb = std::env::var("UNIFIED_RX_RCACHE_MB").ok()
        .and_then(|s| s.parse::<usize>().ok()).unwrap_or(64);
    mb.saturating_mul(1024 * 1024)
}

/// 由常驻服务启动时调用；CLI 路径不调用（保证语义零变化）。
pub fn enable() {
    ENABLED.store(true, Ordering::Relaxed);
    let mut g = CACHE.lock().unwrap_or_else(|e| e.into_inner());
    if g.is_none() {
        *g = Some(HashMap::new());
    }
}

pub fn enabled() -> bool {
    ENABLED.load(Ordering::Relaxed)
}

fn stat_key(path: &Path) -> Option<(u64, u128)> {
    let md = std::fs::metadata(path).ok()?;
    let size = md.len();
    let mtime_ns = md.modified().ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    Some((size, mtime_ns))
}

/// 读文件：启用时走缓存（键含大小与 mtime_ns），否则直读。
pub fn read(path: &Path) -> std::io::Result<Vec<u8>> {
    if !enabled() {
        return std::fs::read(path);
    }
    let Some((size, mtime_ns)) = stat_key(path) else {
        return std::fs::read(path);      // stat 失败 → 回源（错误语义不变）
    };
    let key = path.to_string_lossy().to_string();
    {
        let g = CACHE.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(map) = g.as_ref()
            && let Some(e) = map.get(&key)
            && e.size == size && e.mtime_ns == mtime_ns
        {
            return Ok(e.data.clone());
        }
    }
    let data = std::fs::read(path)?;
    let mut g = CACHE.lock().unwrap_or_else(|e| e.into_inner());
    if g.is_none() {
        *g = Some(HashMap::new());
    }
    if let Some(map) = g.as_mut() {
        if BYTES.load(Ordering::Relaxed) + data.len() > max_bytes() {
            // 到顶：整体清空（简单可解释——比 LRU 更少代码、更少分歧）
            map.clear();
            BYTES.store(0, Ordering::Relaxed);
        }
        map.insert(key, Entry { size, mtime_ns, data: data.clone() });
        BYTES.fetch_add(data.len(), Ordering::Relaxed);
    }
    Ok(data)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 生命周期一次跑完（默认关 → 透传；开 → 命中；改文件 → 失效重读）。
    /// 单测并行 + 全局开关：所有断言必须在**同一个测试**里按序发生。
    #[test]
    fn read_cache_lifecycle() {
        let p = std::env::temp_dir().join("urx-rcache-lifecycle.txt");
        std::fs::write(&p, b"hello").unwrap();
        assert!(!enabled(), "默认必须关（CLI 路径语义零变化）");
        assert_eq!(read(&p).unwrap(), b"hello");          // 透传
        enable();
        assert!(enabled());
        assert_eq!(read(&p).unwrap(), b"hello");          // 走缓存
        std::fs::write(&p, b"v2-longer").unwrap();        // 大小与 mtime 都变
        assert_eq!(read(&p).unwrap(), b"v2-longer");      // 必须失效重读
        let _ = std::fs::remove_file(&p);
    }
}
