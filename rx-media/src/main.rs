// SPDX-FileCopyrightText: 2026 bfxh
// SPDX-License-Identifier: MIT
//! rx-media — MP4/MOV 容器零依赖解析（2026-08-17）。
//!
//! 用户要求（2026-08-17）：剪辑/动画检查——"看看有没有相关的工具 Rust 的，
//! 如果没有就自己造一个"；Rust 生态 symphonia/ffmpeg-next 要么重要么绑 C 库，
//! 自造零依赖（对齐 rx-core 纯标准库承诺）。
//!
//! 解析目标（box 树）：
//!   ftyp                    品牌/兼容
//!   moov/mvhd               时长（timescale/duration）
//!   moov/trak/tkhd          轨道 id、宽高（16.16 定点）
//!   moov/trak/mdia/mdhd     媒体时长
//!   moov/trak/mdia/hdlr     轨道类型（vide/soun/hint）
//!   moov/trak/mdia/minf/stbl/stsd  编码四字符码（avc1/hev1/mp4a/av01/vp09）
//!   moov/trak/mdia/minf/stbl/stts  采样计数 → 帧率估算
//!
//! 损坏检测：非 MP4 魔数、box size 越界、moov 缺失、duration=0。
//!
//! 两种模式（对齐 rx-core 惯例）：
//!   命令行：rx-media info <file>        → JSON（用户/脚本直接查）
//!   stdin： 每行 {tool:"media_info", args:{path}} → 结果行（server.py 常驻）

use serde_json::{json, Map, Value};
use std::io::{BufRead, Write};

// ── box 解析 ───────────────────────────────────────────────────────────

struct Box<'a> {
    kind: [u8; 4],
    #[allow(dead_code)] // 保留：越界诊断/损坏报告用
    start: usize,
    #[allow(dead_code)]
    size: u64,
    data: &'a [u8], // 本 box 数据区（含子 box 起始）
}

/// 遍历 box 序列：size 为 0（到文件尾）/1（64 位 extended size）处理。
fn boxes(data: &[u8], offset: usize, end: usize) -> Vec<Box<'_>> {
    let mut out = Vec::new();
    let mut pos = offset;
    while pos + 8 <= end {
        let size32 = u32::from_be_bytes([data[pos], data[pos + 1], data[pos + 2], data[pos + 3]]);
        let kind = [data[pos + 4], data[pos + 5], data[pos + 6], data[pos + 7]];
        let (size, header_len) = match size32 {
            1 if pos + 16 <= end => {
                let ext = u64::from_be_bytes([
                    data[pos + 8], data[pos + 9], data[pos + 10], data[pos + 11],
                    data[pos + 12], data[pos + 13], data[pos + 14], data[pos + 15],
                ]);
                (ext, 16)
            }
            0 => (end as u64 - pos as u64, 8), // 到文件尾
            n => (n as u64, 8),
        };
        // 防回绕（审查 2026-08-17 抓出）：64 位扩展 size 接近 u64::MAX 时
        // `pos + size as usize` 加法溢出——debug 构建 panic、release 回绕后
        // slice 起始>结束同样 panic。统一用 u64 比较，切片前再次 checked 校验。
        if size < header_len as u64 || pos as u64 + size > end as u64 {
            break; // 越界：损坏
        }
        let body_start = pos + header_len;
        // size 含 header：body_end = pos + size（checked 防回绕）
        let body_end = match pos.checked_add(size as usize) {
            Some(e) if e <= end => e,
            _ => break, // 回绕或越界：损坏
        };
        out.push(Box {
            kind,
            start: pos,
            size,
            data: &data[body_start..body_end],
        });
        pos = body_end;
    }
    out
}

fn kind_str(k: &[u8; 4]) -> String {
    k.iter().map(|b| {
        if b.is_ascii_graphic() { *b as char } else { '.' }
    }).collect()
}

fn u32be(d: &[u8], off: usize) -> u32 {
    u32::from_be_bytes([d[off], d[off + 1], d[off + 2], d[off + 3]])
}

fn u64be(d: &[u8], off: usize) -> u64 {
    u64::from_be_bytes([
        d[off], d[off + 1], d[off + 2], d[off + 3],
        d[off + 4], d[off + 5], d[off + 6], d[off + 7],
    ])
}

/// 查找第一个指定 kind 的子 box。
fn find<'a>(data: &'a [u8], kind: &[u8; 4]) -> Option<Box<'a>> {
    boxes(data, 0, data.len()).into_iter().find(|b| &b.kind == kind)
}

// ── 媒体信息提取 ───────────────────────────────────────────────────────

#[derive(Default, Clone)]
struct Track {
    id: u32,
    kind: String,        // vide/soun/...
    width: u32,          // 16.16 定点 → 取整
    height: u32,
    codec: String,       // avc1/hev1/mp4a/...
    samples: u64,        // stts 累计
}

fn parse_tracks(moov: &[u8]) -> Vec<Track> {
    let mut tracks = Vec::new();
    for b in boxes(moov, 0, moov.len()) {
        if &b.kind != b"trak" {
            continue;
        }
        let mut t = Track::default();
        let tkhd = find(b.data, b"tkhd");
        if let Some(tk) = tkhd {
            let d = tk.data;
            let ver = d.first().copied().unwrap_or(0);
            // v0: track_ID@12, v1: track_ID@20
            let tid_off = if ver == 1 { 20 } else { 12 };
            if d.len() >= tid_off + 4 {
                t.id = u32be(d, tid_off);
            }
            // width/height 在 tkhd 末尾（16.16 定点）：v0 末尾 8 字节 @76/80，v1 @88/92
            let wh = d.len();
            if wh >= 12 {
                let w = u32be(d, wh - 8);
                let h = u32be(d, wh - 4);
                t.width = w >> 16;
                t.height = h >> 16;
            }
        }
        let mdia = find(b.data, b"mdia");
        if let Some(md) = mdia {
            if let Some(h) = find(md.data, b"hdlr") {
                // hdlr: version/flags(4) + pre_defined(4) + handler_type(4)
                if h.data.len() >= 12 {
                    let ht = &h.data[8..12];
                    t.kind = String::from_utf8_lossy(ht).to_string();
                }
            }
            if let Some(minf) = find(md.data, b"minf") {
                if let Some(stbl) = find(minf.data, b"stbl") {
                    if let Some(stsd) = find(stbl.data, b"stsd") {
                        if stsd.data.len() >= 8 {
                            let n = u32be(stsd.data, 4) as usize;
                            for e in boxes(stsd.data, 8, stsd.data.len()).into_iter().take(n.max(1)) {
                                t.codec = kind_str(&e.kind);
                                break;
                            }
                        }
                    }
                    if let Some(stts) = find(stbl.data, b"stts") {
                        // stts: ver/flags(4) + entry_count(4) + entries(sample_count u32, delta u32)
                        if stts.data.len() >= 8 {
                            let n = u32be(stts.data, 4) as usize;
                            let mut samples: u64 = 0;
                            for i in 0..n {
                                let off = 8 + i * 8;
                                if off + 8 > stts.data.len() {
                                    break;
                                }
                                samples += u32be(stts.data, off) as u64;
                            }
                            t.samples = samples;
                        }
                    }
                }
            }
        }
        tracks.push(t);
    }
    tracks
}

/// 解析文件 → JSON 结果。
fn media_info(path: &str) -> Result<Value, String> {
    let data = std::fs::read(path).map_err(|e| format!("读取失败: {e}"))?;
    let mut info = Map::new();
    info.insert("path".into(), Value::String(path.to_string()));
    info.insert("file_size".into(), json!(data.len()));

    // 损坏检测 1：魔数（ftyp 或 moov 开头；mov 允许 wide/mdat 前置但至少要有 box）
    if data.len() < 12 {
        info.insert("damaged".into(), json!(true));
        info.insert("reason".into(), json!("文件过短（<12 字节）"));
        info.insert("ok".into(), json!(false));
        return Ok(Value::Object(info));
    }
    let top = boxes(&data, 0, data.len());
    if top.is_empty() {
        info.insert("damaged".into(), json!(true));
        info.insert("reason".into(), json!("无有效 box"));
        info.insert("ok".into(), json!(false));
        return Ok(Value::Object(info));
    }

    // ftyp 品牌
    let mut brand = "unknown";
    let mut compatible = Vec::new();
    if let Some(ftyp) = find(&data, b"ftyp") {
        if ftyp.data.len() >= 8 {
            brand = std::str::from_utf8(&ftyp.data[0..4]).unwrap_or("????");
            let n = (ftyp.data.len() - 8) / 4;
            for i in 0..n {
                compatible.push(kind_str(&[ftyp.data[8 + i * 4], ftyp.data[9 + i * 4],
                                           ftyp.data[10 + i * 4], ftyp.data[11 + i * 4]]));
            }
        }
    }
    info.insert("brand".into(), json!(brand));
    info.insert("compatible".into(), json!(compatible));

    // moov 缺失 = 损坏（无法播放）
    let moov = find(&data, b"moov");
    if moov.is_none() {
        info.insert("damaged".into(), json!(true));
        info.insert("reason".into(), json!("moov box 缺失（不可播放/损坏）"));
        info.insert("ok".into(), json!(false));
        return Ok(Value::Object(info));
    }
    let moov = moov.unwrap();

    // mvhd：时长
    let mut timescale: u64 = 0;
    let mut duration: u64 = 0;
    if let Some(mvhd) = find(moov.data, b"mvhd") {
        let d = mvhd.data;
        let ver = d.first().copied().unwrap_or(0);
        if ver == 1 && d.len() >= 32 {
            timescale = u32be(d, 20) as u64;
            duration = u64be(d, 24);
        } else if d.len() >= 20 {
            timescale = u32be(d, 12) as u64;
            duration = u32be(d, 16) as u64;
        }
    }
    info.insert("timescale".into(), json!(timescale));
    info.insert("duration".into(), json!(duration));
    let dur_sec = if timescale > 0 {
        duration as f64 / timescale as f64
    } else {
        0.0
    };
    info.insert("duration_sec".into(), json!((dur_sec * 1000.0).round() / 1000.0));

    // 轨道
    let tracks = parse_tracks(moov.data);
    let mut tracks_json = Vec::new();
    for t in &tracks {
        let mut tm = Map::new();
        tm.insert("id".into(), json!(t.id));
        tm.insert("kind".into(), json!(t.kind));
        tm.insert("width".into(), json!(t.width));
        tm.insert("height".into(), json!(t.height));
        tm.insert("codec".into(), json!(t.codec));
        tm.insert("samples".into(), json!(t.samples));
        // 帧率估算：视频轨 samples / 时长
        if t.kind == "vide" && dur_sec > 0.0 && t.samples > 0 {
            tm.insert("fps_est".into(), json!((t.samples as f64 / dur_sec * 100.0).round() / 100.0));
        }
        tracks_json.push(Value::Object(tm));
    }
    info.insert("tracks".into(), json!(tracks_json));

    let video = tracks.iter().find(|t| t.kind == "vide");
    let audio = tracks.iter().find(|t| t.kind == "soun");
    if let Some(v) = video {
        info.insert("width".into(), json!(v.width));
        info.insert("height".into(), json!(v.height));
        info.insert("codec".into(), json!(v.codec));
        info.insert("has_video".into(), json!(true));
    } else {
        info.insert("has_video".into(), json!(false));
    }
    info.insert("has_audio".into(), json!(audio.is_some()));
    info.insert("track_count".into(), json!(tracks.len()));

    // 损坏检测 2：duration=0
    let mut damaged = false;
    let mut reason = "";
    if duration == 0 && !tracks.is_empty() {
        damaged = true;
        reason = "duration=0（时长信息缺失/损坏）";
    }
    if tracks.is_empty() {
        damaged = true;
        reason = "无轨道（空文件或损坏）";
    }
    info.insert("damaged".into(), json!(damaged));
    if damaged {
        info.insert("reason".into(), json!(reason));
    }
    info.insert("ok".into(), json!(!damaged));
    Ok(Value::Object(info))
}

// ── 入口 ───────────────────────────────────────────────────────────────

fn dispatch(req: &Value) -> String {
    let tool = req["tool"].as_str().unwrap_or("");
    let path = req["args"]["path"].as_str().unwrap_or("");
    match tool {
        "media_info" => match media_info(path) {
            Ok(v) => v.to_string(),
            Err(e) => format!("ERR: {e}"),
        },
        _ => format!("ERR: unknown tool: {tool}"),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() >= 3 && args[1] == "info" {
        // 命令行模式：rx-media info <file>
        match media_info(&args[2]) {
            Ok(v) => {
                println!("{v}");
                std::process::exit(0);
            }
            Err(e) => {
                eprintln!("{e}");
                std::process::exit(1);
            }
        }
    }
    // stdin 常驻模式（对齐 rx-core）：每行 {tool, args} → 结果行
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let req: Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(_) => {
                let _ = writeln!(out, "ERR: bad json");
                continue;
            }
        };
        let _ = writeln!(out, "{}", dispatch(&req));
        let _ = out.flush();
    }
}
