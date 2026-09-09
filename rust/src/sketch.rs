//! n-gram bottom-k MinHash 指纹（S119）：near_dupes 的 sketch 原生化。
//!
//! 口径与 `tools/gpu.py::ngram_bottomk_*` 逐位一致（FNV-1a 32，同款常量与窗口序）：
//! 每个长度 ng 的窗口算一个 32 位哈希，保留最小的 k 个（重复值保留，Python 侧
//! 转 frozenset 后与 `heapq.nsmallest` 口径等价）。
//!
//! 并行：单文件位置数超过 CHUNK_POSITIONS 时按**位置空间**切块，`std::thread::scope`
//! 起线程（零第三方依赖）；bottom-k 与处理顺序无关，故多线程结果与单线程逐位一致
//! （tests 里锁死）。切块读同一份只读缓冲，窗口可跨块重叠——不存在边界丢失。
//!
//! 输入协议：stdin 帧流——每帧 `u32` 小端长度 + UTF-8 路径字节，长度 0 表示结束
//! （路径含换行/空格也安全；命令行 32767 码元上限装不下大文件清单）。
//! 输出协议：stdout 一行 JSON（与 Python 薄壳同构）。
//!
//! 无沙盒门：与 scan 域同纪律（纯读分析，路径由 Python 侧 `_fs_resolve` 先钳制）。

use crate::json::Value;
use std::collections::BinaryHeap;
use std::io::Read;

/// 单个并行块的位置数（约 1MB 数据；低于此不值当起线程）。
const CHUNK_POSITIONS: usize = 1 << 20;

/// n-gram 长度上限（与 Python 侧 `near_dupes` 的钳制域一致）。
pub const MAX_NG: usize = 16;

#[inline]
fn fnv1a32(data: &[u8], start: usize, ng: usize) -> u32 {
    let mut h: u32 = 2166136261;
    for j in 0..ng {
        h = (h ^ data[start + j] as u32).wrapping_mul(16777619);
    }
    h
}

/// 位置区间 `[from, to)` 的 bottom-k，升序返回（重复值保留）。
fn bottomk_range(data: &[u8], ng: usize, from: usize, to: usize, k: usize) -> Vec<u32> {
    let mut heap: BinaryHeap<u32> = BinaryHeap::with_capacity(k + 1);
    for i in from..to {
        let h = fnv1a32(data, i, ng);
        if heap.len() < k {
            heap.push(h);
        } else if let Some(&top) = heap.peek() {
            if h < top {
                heap.pop();
                heap.push(h);
            }
        }
    }
    heap.into_sorted_vec()
}

/// 单文件指纹：`threads<=1` 或位置数不足一块时走单线程。
pub fn sketch_bytes(data: &[u8], ng: usize, k: usize, threads: usize) -> Vec<u32> {
    let m = if data.len() >= ng { data.len() - ng + 1 } else { 0 };
    if m == 0 || k == 0 {
        return Vec::new();
    }
    let nchunks = if threads <= 1 {
        1
    } else {
        threads.min((m + CHUNK_POSITIONS - 1) / CHUNK_POSITIONS)
    };
    if nchunks <= 1 {
        return bottomk_range(data, ng, 0, m, k);
    }
    let per = m / nchunks + 1;
    let mut parts: Vec<Vec<u32>> = Vec::with_capacity(nchunks);
    std::thread::scope(|s| {
        let mut handles = Vec::with_capacity(nchunks);
        for t in 0..nchunks {
            let from = t * per;
            let to = ((t + 1) * per).min(m);
            handles.push(s.spawn(move || {
                if from >= to {
                    Vec::new()
                } else {
                    bottomk_range(data, ng, from, to, k)
                }
            }));
        }
        for h in handles {
            parts.push(h.join().unwrap_or_default());
        }
    });
    let mut all: Vec<u32> = parts.into_iter().flatten().collect();
    all.sort_unstable();
    all.truncate(k);
    all
}

/// 读 stdin 帧流（`u32` 小端长度 + UTF-8 路径，长度 0 结束）。
pub fn read_path_frames<R: Read>(mut r: R) -> Result<Vec<String>, String> {
    let mut buf = Vec::new();
    r.read_to_end(&mut buf)
        .map_err(|e| format!("stdin 读取失败: {}", e))?;
    let mut out = Vec::new();
    let mut i = 0usize;
    while i + 4 <= buf.len() {
        let len = u32::from_le_bytes([buf[i], buf[i + 1], buf[i + 2], buf[i + 3]]) as usize;
        i += 4;
        if len == 0 {
            return Ok(out);
        }
        if i + len > buf.len() {
            return Err(format!("路径帧流截断（帧长 {} 越界）", len));
        }
        out.push(String::from_utf8_lossy(&buf[i..i + len]).into_owned());
        i += len;
    }
    Ok(out)
}

/// 批量指纹：按输入顺序输出，单文件读失败进 `errors` 不中断整批。
pub fn sketch_batch(paths: &[String], ng: usize, k: usize, threads: usize) -> Value {
    let mut files = Vec::with_capacity(paths.len());
    let mut errors = Vec::new();
    for p in paths {
        match std::fs::read(p) {
            Ok(data) => {
                let fp = sketch_bytes(&data, ng, k, threads);
                let arr: Vec<Value> = fp.into_iter().map(|h| Value::Int(h as i128)).collect();
                files.push(Value::Obj(vec![
                    ("path".into(), Value::Str(p.clone())),
                    ("fingerprint".into(), Value::Arr(arr)),
                ]));
            }
            Err(e) => errors.push(Value::Obj(vec![
                ("path".into(), Value::Str(p.clone())),
                ("error".into(), Value::Str(format!("{}", e))),
            ])),
        }
    }
    Value::Obj(vec![
        ("ng".into(), Value::Int(ng as i128)),
        ("k".into(), Value::Int(k as i128)),
        ("threads".into(), Value::Int(threads as i128)),
        ("files".into(), Value::Arr(files)),
        ("errors".into(), Value::Arr(errors)),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn fnv_known_answers() {
        // 由 Python 参考实现 tools/gpu.py::ngram_hashes_cpu 实算取值（非手写估计）
        let data = b"abcd";
        assert_eq!(fnv1a32(data, 0, 4), 0xCE3479BD);
        assert_eq!(fnv1a32(b"aaaa", 0, 4), 0x4CEB2DB9);
        assert_eq!(fnv1a32(b"zzzz", 0, 4), 0xE16993A5);
    }

    #[test]
    fn bottomk_matches_bruteforce() {
        let data: Vec<u8> = (0..5000u32).map(|i| (i * 37 % 251) as u8).collect();
        let mut all: Vec<u32> = (0..data.len() - 3).map(|i| fnv1a32(&data, i, 4)).collect();
        all.sort_unstable();
        all.truncate(128);
        assert_eq!(sketch_bytes(&data, 4, 128, 1), all);
    }

    #[test]
    fn short_and_empty_inputs() {
        assert_eq!(sketch_bytes(b"", 4, 128, 4), Vec::<u32>::new());
        assert_eq!(sketch_bytes(b"abc", 4, 128, 4), Vec::<u32>::new());
        assert_eq!(sketch_bytes(b"abcd", 4, 0, 4), Vec::<u32>::new());
        assert_eq!(sketch_bytes(b"abcd", 4, 128, 4).len(), 1);
    }

    #[test]
    fn multithread_equals_singlethread() {
        // 位置数 > CHUNK_POSITIONS 才会切块；锁死"多线程与单线程逐位一致"
        let data: Vec<u8> = (0..(CHUNK_POSITIONS + 12_345)).map(|i| (i * 31 % 253) as u8).collect();
        let one = sketch_bytes(&data, 4, 128, 1);
        for t in [2usize, 4, 8] {
            assert_eq!(sketch_bytes(&data, 4, 128, t), one, "threads={}", t);
        }
        assert_eq!(one.len(), 128);
    }

    #[test]
    fn frame_stream_roundtrip() {
        let mut buf = Vec::new();
        for p in ["D:/a/b.py", "带中文/路径 x.txt"] {
            let b = p.as_bytes();
            buf.extend_from_slice(&(b.len() as u32).to_le_bytes());
            buf.extend_from_slice(b);
        }
        buf.extend_from_slice(&0u32.to_le_bytes());
        let got = read_path_frames(Cursor::new(buf)).unwrap();
        assert_eq!(got, vec!["D:/a/b.py".to_string(), "带中文/路径 x.txt".into()]);
        assert!(read_path_frames(Cursor::new(vec![9, 0, 0, 0, b'x'])).is_err());
    }
}
