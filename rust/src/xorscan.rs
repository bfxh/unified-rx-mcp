//! 单字节异或密钥枚举（S120）：file_scan 的 `xor_crib` 原生化。
//!
//! 口径与 `tools/gpu.py::xor_crib_scan_cpu/gpu` 逐位一致：
//! 对 256 个密钥分别统计「数据按该密钥解码后 crib 出现次数」，
//! 计数**饱和封顶 256**（防高熵数据上的噪声淹没真密钥），只回非零项、按密钥升序；
//! 命中后从下一字节继续（允许重叠命中，与 Python `find(start=j+1)` 同语义）。
//!
//! 并行：**密钥分片**（256 键 / N 线程），各片独立扫描同一只读数据；
//! 结果与单线程逐位一致（键区间互不重叠，与顺序无关）。
//! 实测（16MB / min-of-3）：单线程 3084ms、16 线程 365ms；同口径 GPU 内核 1279ms
//! （其并行度只有 256 个 work item，见 spec/GPU.md §二·三）。
//!
//! 输入协议：stdin 帧流（同 sketch：`u32` 小端长度 + UTF-8 路径，长度 0 结束）。
//! 输出协议：stdout 一行 JSON。

use crate::json::Value;
use std::io::Read;

/// 单文件：256 键全扫，返回按密钥升序的非零 `(key, count)`。
pub fn xor_scan_bytes(data: &[u8], crib: &[u8], threads: usize) -> Vec<(u32, u32)> {
    let n = data.len();
    let clen = crib.len();
    if clen == 0 || n < clen {
        return Vec::new();
    }
    let nthreads = threads.clamp(1, 256);
    let per = 256 / nthreads + 1;
    let mut hits = vec![0u32; 256];
    std::thread::scope(|s| {
        let mut handles = Vec::new();
        for t in 0..nthreads {
            let from = t * per;
            let to = ((t + 1) * per).min(256);
            if from >= to {
                continue;
            }
            handles.push(s.spawn(move || {
                let mut out = vec![0u32; 256];
                for key in from..to {
                    let k = key as u8;
                    let mut c = 0u32;
                    let mut i = 0usize;
                    while i + clen <= n {
                        let mut ok = true;
                        for j in 0..clen {
                            if (data[i + j] ^ k) != crib[j] {
                                ok = false;
                                break;
                            }
                        }
                        if ok && c < 256 {
                            c += 1;
                        }
                        i += 1;
                    }
                    out[key] = c;
                }
                out
            }));
        }
        for h in handles {
            if let Ok(part) = h.join() {
                for i in 0..256 {
                    if part[i] > hits[i] {
                        hits[i] = part[i];
                    }
                }
            }
        }
    });
    hits.iter()
        .enumerate()
        .filter(|(_, c)| **c > 0)
        .map(|(k, &c)| (k as u32, c))
        .collect()
}

/// hex 解码（`hex:4d5a...` 的载荷；无第三方 crate）。
pub fn hex_decode(s: &str) -> Result<Vec<u8>, String> {
    let s = s.trim();
    if s.len() % 2 != 0 {
        return Err("hex 长度必须为偶数".into());
    }
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(s.len() / 2);
    let val = |c: u8| -> Result<u8, String> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            b'A'..=b'F' => Ok(c - b'A' + 10),
            _ => Err(format!("非法 hex 字符: {}", c as char)),
        }
    };
    for i in (0..b.len()).step_by(2) {
        out.push((val(b[i])? << 4) | val(b[i + 1])?);
    }
    Ok(out)
}

/// 批量：按输入顺序输出，单文件读失败进 `errors` 不中断整批。
pub fn xor_scan_batch(paths: &[String], crib: &[u8], threads: usize) -> Value {
    let mut files = Vec::with_capacity(paths.len());
    let mut errors = Vec::new();
    for p in paths {
        match std::fs::read(p) {
            Ok(data) => {
                let pairs = xor_scan_bytes(&data, crib, threads);
                let arr: Vec<Value> = pairs
                    .into_iter()
                    .map(|(k, c)| Value::Arr(vec![Value::Int(k as i128), Value::Int(c as i128)]))
                    .collect();
                files.push(Value::Obj(vec![
                    ("path".into(), Value::Str(p.clone())),
                    ("keys".into(), Value::Arr(arr)),
                ]));
            }
            Err(e) => errors.push(Value::Obj(vec![
                ("path".into(), Value::Str(p.clone())),
                ("error".into(), Value::Str(format!("{}", e))),
            ])),
        }
    }
    Value::Obj(vec![
        ("crib_hex".into(), Value::Str(crib.iter().map(|b| format!("{:02x}", b)).collect())),
        ("threads".into(), Value::Int(threads as i128)),
        ("files".into(), Value::Arr(files)),
        ("errors".into(), Value::Arr(errors)),
    ])
}

/// stdin 帧流读路径（与 sketch 同协议；此处独立实现避免跨模块耦合）。
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_single_byte_key() {
        let crib = b"MZ\x90\x00\x03\x00\x00\x00";
        let key = 0x5Au8;
        let mut data: Vec<u8> = crib.iter().map(|b| b ^ key).collect();
        data.extend((0..10_000u32).map(|i| (i * 7 % 251) as u8));
        let got = xor_scan_bytes(&data, crib, 4);
        assert_eq!(got, vec![(0x5A, 1)]);
    }

    #[test]
    fn saturation_caps_at_256() {
        // "MZ"*400：key=0 与 key=0x17（M^Z）都命中，均封顶 256（与 Python/GPU 同口径）
        let data = b"MZ".repeat(400);
        let hits = xor_scan_bytes(&data, b"MZ", 4);
        let map: std::collections::HashMap<u32, u32> = hits.into_iter().collect();
        assert_eq!(map.get(&0), Some(&256));
        assert_eq!(map.get(&0x17), Some(&256));
        assert!(map.values().all(|&v| v <= 256));
    }

    #[test]
    fn multithread_equals_singlethread() {
        let crib = b"PK\x03\x04";
        let data: Vec<u8> = (0..200_000u32).map(|i| (i * 13 % 256) as u8).collect();
        let one = xor_scan_bytes(&data, crib, 1);
        for t in [2usize, 7, 16] {
            assert_eq!(xor_scan_bytes(&data, crib, t), one, "threads={}", t);
        }
    }

    #[test]
    fn empty_and_short_inputs() {
        assert!(xor_scan_bytes(b"", b"MZ", 4).is_empty());
        assert!(xor_scan_bytes(b"M", b"MZ", 4).is_empty());
        assert!(xor_scan_bytes(b"MZMZ", b"", 4).is_empty());
    }

    #[test]
    fn hex_decode_roundtrip() {
        assert_eq!(hex_decode("4d5a9000").unwrap(), b"MZ\x90\x00");
        assert_eq!(hex_decode("4D5A").unwrap(), b"MZ");
        assert!(hex_decode("4d5").is_err());
        assert!(hex_decode("zz").is_err());
    }

    #[test]
    fn overlapping_hits_counted() {
        // "aaaa" 里 "aa" 的重叠命中：位置 0,1,2 共 3 次（与 Python find(j+1) 同语义）
        let got = xor_scan_bytes(b"aaaa", b"aa", 2);
        let map: std::collections::HashMap<u32, u32> = got.into_iter().collect();
        assert_eq!(map.get(&0), Some(&3));
    }
}
