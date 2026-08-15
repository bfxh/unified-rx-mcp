// SPDX-FileCopyrightText: 2026 bfxh
// SPDX-License-Identifier: MIT
//! rx-telemetry — unified-rx 遥测核心（Rust）。
//!
//! 目标（用户："监控太弱了……各种都要监控搞强点，信息收集的太少了"）：
//!   1. **工具调用遥测**：耗时（wall_ms）/ 结果状态 / 错误采样 / 调用频率
//!   2. **daemon 心跳**：7 循环各自 last-run / 本轮耗时 / 卡死检测数据
//!   3. **进程资源**：RSS / CPU（采样）
//!   4. **环形缓冲**：内存保留最近 N 条 → JSONL 批量追加落盘
//!   5. **GB 级流式读**：聚合 / tail 均不整载文件（Superluminal 式大文件流畅）
//!
//! 集成方式（对齐 rx-core）：`rx-telemetry serve` 常驻子进程（stdin 行协议，
//! stdout 行响应），由 server.py / daemon.py 经 subprocess.Popen 调用。

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

// ─────────────────────────────────────────────────────────────
// 常量
// ─────────────────────────────────────────────────────────────

/// 内存环形缓冲上限（条）。
pub const DEFAULT_CAP: usize = 10_000;
/// 缓冲批量落盘阈值（条）。
pub const FLUSH_BATCH: usize = 100;
/// 单个 telemetry.jsonl 超过该大小（字节）触发轮转。
pub const DEFAULT_MAX_FILE: u64 = 100 * 1024 * 1024;
/// 轮转保留份数（telemetry.jsonl / .1 / .2，不含更多）。
pub const ROTATE_KEEP: usize = 3;
/// 聚合时每个工具保留的最大耗时样本（超出则每隔一条降采样）。
pub const MAX_SAMPLES_PER_TOOL: usize = 100_000;

/// 默认状态目录（~/.unified-rx），可用环境变量 UNIFIED_RX_STATE_DIR 覆盖。
pub fn default_state_dir() -> PathBuf {
    if let Ok(d) = std::env::var("UNIFIED_RX_STATE_DIR") {
        if !d.trim().is_empty() {
            return PathBuf::from(d);
        }
    }
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_else(|_| ".".into());
    PathBuf::from(home).join(".unified-rx")
}

/// 遥测文件路径（默认 ~/.unified-rx/telemetry.jsonl）。
pub fn default_telemetry_path() -> PathBuf {
    default_state_dir().join("telemetry.jsonl")
}

// ─────────────────────────────────────────────────────────────
// 记录结构（JSONL 每行一条）
// ─────────────────────────────────────────────────────────────

/// 一条遥测记录。JSON 形态由 `kind` 字段区分（serde tag）。
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Record {
    /// 工具调用：耗时 / 结果状态 / 错误 / 参数摘要
    Tool {
        ts: f64,
        tool: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        args: Option<String>,
        wall_ms: f64,
        status: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        err: Option<String>,
    },
    /// daemon 循环心跳：last-run 时刻 + 本轮循环耗时（卡死检测依据）
    Hb {
        ts: f64,
        #[serde(rename = "loop")]
        loop_name: String,
        cycle_ms: f64,
        #[serde(skip_serializing_if = "Option::is_none")]
        pid: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        rss_kb: Option<u64>,
    },
    /// 进程资源采样
    Res {
        ts: f64,
        pid: u32,
        rss_kb: u64,
        cpu_pct: f64,
    },
}

impl Record {
    /// 构造工具调用记录（成功）。
    pub fn tool_ok(ts: f64, tool: &str, args: Option<String>, wall_ms: f64) -> Self {
        Record::Tool {
            ts,
            tool: tool.to_string(),
            args,
            wall_ms,
            status: "ok".into(),
            err: None,
        }
    }
    /// 构造工具调用记录（失败）。
    pub fn tool_err(ts: f64, tool: &str, wall_ms: f64, err: &str) -> Self {
        Record::Tool {
            ts,
            tool: tool.to_string(),
            args: None,
            wall_ms,
            status: "error".into(),
            err: Some(err.to_string()),
        }
    }
    /// 构造心跳记录。
    pub fn hb(ts: f64, loop_name: &str, cycle_ms: f64) -> Self {
        Record::Hb {
            ts,
            loop_name: loop_name.to_string(),
            cycle_ms,
            pid: None,
            rss_kb: None,
        }
    }
    /// 记录时间戳。
    pub fn ts(&self) -> f64 {
        match self {
            Record::Tool { ts, .. } => *ts,
            Record::Hb { ts, .. } => *ts,
            Record::Res { ts, .. } => *ts,
        }
    }
    /// 序列化为单行 JSON（追加到 JSONL）。
    pub fn to_json_line(&self) -> String {
        serde_json::to_string(self).unwrap_or_else(|_| "{}".into())
    }
}

// ─────────────────────────────────────────────────────────────
// 环形缓冲 + JSONL 落盘
// ─────────────────────────────────────────────────────────────

/// 遥测存储：内存环形缓冲 + 批量追加落盘 + 轮转。
pub struct TelemetryStore {
    path: PathBuf,
    buf: VecDeque<Record>,
    cap: usize,
    max_file: u64,
    flushed: u64,
}

impl TelemetryStore {
    /// 新建存储（默认路径 + 默认容量 + 默认轮转阈值）。
    pub fn new() -> Self {
        Self::with_path(default_telemetry_path())
    }

    /// 指定落盘路径。
    pub fn with_path(path: PathBuf) -> Self {
        TelemetryStore {
            path,
            buf: VecDeque::with_capacity(DEFAULT_CAP.min(FLUSH_BATCH)),
            cap: DEFAULT_CAP,
            max_file: DEFAULT_MAX_FILE,
            flushed: 0,
        }
    }

    /// 配置容量（测试用）。
    pub fn with_cap(mut self, cap: usize) -> Self {
        self.cap = cap;
        self
    }

    /// 配置轮转阈值（测试用）。
    pub fn with_max_file(mut self, max_file: u64) -> Self {
        self.max_file = max_file;
        self
    }

    /// 落盘路径。
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// 已落盘条数。
    pub fn flushed(&self) -> u64 {
        self.flushed
    }

    /// 当前缓冲条数。
    pub fn buffered(&self) -> usize {
        self.buf.len()
    }

    /// 入缓冲；达批量阈值自动落盘。
    pub fn push(&mut self, rec: Record) {
        self.buf.push_back(rec);
        if self.buf.len() >= self.cap {
            // 容量保护：放不下时丢弃最旧（环形语义）
            while self.buf.len() > self.cap {
                self.buf.pop_front();
            }
        }
        if self.buf.len() >= FLUSH_BATCH {
            let _ = self.flush();
        }
    }

    /// 强制落盘（把缓冲全部追加写文件；文件超限轮转）。
    /// 返回本次写入条数；失败返回 io::Error（调用方静默降级）。
    pub fn flush(&mut self) -> std::io::Result<usize> {
        if self.buf.is_empty() {
            return Ok(0);
        }
        let n = self.buf.len();
        // 确保目录存在
        if let Some(dir) = self.path.parent() {
            let _ = fs::create_dir_all(dir);
        }
        // 超限轮转
        if let Ok(meta) = fs::metadata(&self.path) {
            if meta.len() > self.max_file {
                self.rotate();
            }
        }
        let mut f = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        let mut out = String::with_capacity(n * 96);
        for rec in self.buf.drain(..) {
            out.push_str(&rec.to_json_line());
            out.push('\n');
        }
        f.write_all(out.as_bytes())?;
        f.flush()?;
        self.flushed += n as u64;
        Ok(n)
    }

    /// 轮转：telemetry.jsonl → .1 → .2（保留 ROTATE_KEEP 份）。
    fn rotate(&mut self) {
        for i in (1..ROTATE_KEEP).rev() {
            let from = self.path.with_extension(format!("{}.jsonl", i));
            let to = self.path.with_extension(format!("{}.jsonl", i + 1));
            if from.exists() {
                let _ = fs::rename(&from, &to);
            }
        }
        let first = self.path.with_extension("1.jsonl");
        let _ = fs::rename(&self.path, &first);
    }
}

impl Default for TelemetryStore {
    fn default() -> Self {
        Self::new()
    }
}

// ─────────────────────────────────────────────────────────────
// 流式聚合（GB 级不整载内存）
// ─────────────────────────────────────────────────────────────

/// 单工具聚合结果。
#[derive(Serialize, Clone, Debug, Default)]
pub struct ToolAgg {
    pub count: u64,
    pub err_count: u64,
    pub avg_ms: f64,
    pub p95_ms: f64,
    pub max_ms: f64,
    pub last_ts: f64,
}

/// 单循环心跳聚合结果。
#[derive(Serialize, Clone, Debug, Default)]
pub struct HbAgg {
    pub count: u64,
    pub last_ts: f64,
    pub last_cycle_ms: f64,
    pub avg_cycle_ms: f64,
    pub max_cycle_ms: f64,
}

/// 聚合报告。
#[derive(Serialize, Clone, Debug, Default)]
pub struct AggReport {
    pub total_calls: u64,
    pub total_err: u64,
    pub overall_err_rate: f64,
    pub overall_avg_ms: f64,
    pub overall_p95_ms: f64,
    pub overall_max_ms: f64,
    pub tools: HashMap<String, ToolAgg>,
    pub heartbeats: HashMap<String, HbAgg>,
    pub res_samples: u64,
    pub skipped_bad_lines: u64,
    pub scanned_bytes: u64,
}

fn percentile(sorted: &[u64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((sorted.len() as f64 - 1.0) * p).round() as usize;
    sorted[idx.min(sorted.len() - 1)] as f64
}

/// 流式聚合文件（可限制时间窗口 `since_ts`）。
/// 逐行解析，坏行跳过计数；每工具耗时样本上限 MAX_SAMPLES_PER_TOOL
/// （超出降采样：只保留偶数下标样本）。
pub fn aggregate_file(path: &Path, since_ts: Option<f64>) -> std::io::Result<AggReport> {
    let mut rep = AggReport::default();
    let mut samples: HashMap<String, Vec<u64>> = HashMap::new();
    let mut tool_total_ms: HashMap<String, u64> = HashMap::new();
    let mut total_ms_all: u64 = 0;
    let mut max_ms_all: u64 = 0;

    let f = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(rep); // 无文件 = 无数据，不是错误
        }
        Err(e) => return Err(e),
    };
    let reader = BufReader::new(f);
    for line in reader.lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => {
                rep.skipped_bad_lines += 1;
                continue;
            }
        };
        rep.scanned_bytes += line.len() as u64 + 1;
        if line.trim().is_empty() {
            continue;
        }
        let rec: Record = match serde_json::from_str(&line) {
            Ok(r) => r,
            Err(_) => {
                rep.skipped_bad_lines += 1;
                continue;
            }
        };
        if let Some(s) = since_ts {
            if rec.ts() < s {
                continue;
            }
        }
        match rec {
            Record::Tool { tool, wall_ms, status, ts, .. } => {
                let ms = (wall_ms * 1000.0).round() as u64; // 微秒
                rep.total_calls += 1;
                total_ms_all += ms;
                max_ms_all = max_ms_all.max(ms);
                if status == "error" {
                    rep.total_err += 1;
                }
                let agg = rep.tools.entry(tool.clone()).or_default();
                agg.count += 1;
                if status == "error" {
                    agg.err_count += 1;
                }
                agg.last_ts = ts;
                agg.max_ms = agg.max_ms.max(wall_ms);
                *tool_total_ms.entry(tool.clone()).or_default() += ms;
                let v = samples.entry(tool).or_default();
                if v.len() < MAX_SAMPLES_PER_TOOL || v.len().is_multiple_of(2) {
                    v.push(ms);
                }
            }
            Record::Hb { loop_name, cycle_ms, ts, .. } => {
                let hb = rep.heartbeats.entry(loop_name).or_default();
                hb.count += 1;
                hb.last_ts = ts;
                hb.last_cycle_ms = cycle_ms;
                hb.max_cycle_ms = hb.max_cycle_ms.max(cycle_ms);
                hb.avg_cycle_ms = hb.avg_cycle_ms
                    + (cycle_ms - hb.avg_cycle_ms) / hb.count as f64; // 在线均值
            }
            Record::Res { .. } => {
                rep.res_samples += 1;
            }
        }
    }

    rep.overall_avg_ms = if rep.total_calls > 0 {
        total_ms_all as f64 / rep.total_calls as f64 / 1000.0
    } else {
        0.0
    };
    rep.overall_max_ms = max_ms_all as f64 / 1000.0;
    rep.overall_err_rate = if rep.total_calls > 0 {
        rep.total_err as f64 / rep.total_calls as f64
    } else {
        0.0
    };
    // P95（逐工具，再取整体 = 全样本的 P95）
    let mut all: Vec<u64> = Vec::new();
    for (tool, v) in samples.iter_mut() {
        v.sort_unstable();
        let p95 = percentile(v, 0.95);
        if let Some(agg) = rep.tools.get_mut(tool) {
            agg.p95_ms = p95 / 1000.0;
            if let Some(total) = tool_total_ms.get(tool) {
                agg.avg_ms = if agg.count > 0 {
                    *total as f64 / agg.count as f64 / 1000.0
                } else {
                    0.0
                };
            }
        }
        all.extend(v.iter());
    }
    if !all.is_empty() {
        all.sort_unstable();
        rep.overall_p95_ms = percentile(&all, 0.95) / 1000.0;
    }
    Ok(rep)
}

// ─────────────────────────────────────────────────────────────
// 流式 tail（从文件尾部倒读 N 条，不整载文件）
// ─────────────────────────────────────────────────────────────

const TAIL_BLOCK: u64 = 64 * 1024;

/// 从文件尾部倒读最多 `n` 条记录（保持时间顺序）。
/// 大文件（GB 级）只读尾部块——Superluminal 式。
pub fn tail_file(path: &Path, n: usize) -> std::io::Result<Vec<Record>> {
    let mut raw: Vec<String> = Vec::new();
    if n == 0 {
        return Ok(Vec::new());
    }
    let mut f = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let len = f.metadata()?.len();
    if len == 0 {
        return Ok(Vec::new());
    }
    // 从尾部逐块倒读，按行切分收集
    let mut pos = len;
    let mut tail_buf: Vec<u8> = Vec::new();
    'outer: while pos > 0 {
        let start = pos.saturating_sub(TAIL_BLOCK);
        let block_len = (pos - start) as usize;
        let mut block = vec![0u8; block_len];
        f.seek(SeekFrom::Start(start))?;
        f.read_exact(&mut block)?;
        // 新块拼到已有内容前面（倒序收集）
        let mut combined = block;
        combined.extend_from_slice(&tail_buf);
        // 按 '\n' 切分：最后一段（可能不完整）保留
        let lines: Vec<&[u8]> = combined.split(|b| *b == b'\n').collect();
        // 从后往前取完整行
        let mut collected: Vec<String> = Vec::new();
        let mut carry: Vec<u8> = Vec::new();
        // 最后一段是到文件尾（完整）或跨块；split 的最后元素要么空（行尾\n）要么残留
        for (i, seg) in lines.iter().enumerate().rev() {
            if i == 0 && seg.is_empty() && lines.len() > 1 {
                continue; // 块首空段（紧贴前一块）
            }
            if i == lines.len() - 1 && !seg.is_empty() && start > 0 {
                // 末尾残留（可能是跨块的行）——留给下一轮（更早的块）拼接
                carry = seg.to_vec();
                continue;
            }
            if seg.is_empty() {
                continue;
            }
            if let Ok(s) = std::str::from_utf8(seg) {
                collected.push(s.to_string());
                if collected.len() >= n {
                    break;
                }
            }
        }
        for line in collected.into_iter().rev() {
            raw.push(line);
            if raw.len() >= n {
                break 'outer;
            }
        }
        tail_buf = carry;
        if tail_buf.is_empty() && raw.is_empty() {
            break; // 没有可解析行（空文件/全空行）
        }
        pos = start;
    }
    // 解析为 Record（坏行宽容跳过）
    let mut recs = Vec::with_capacity(raw.len());
    for line in raw {
        if let Ok(r) = serde_json::from_str::<Record>(&line) {
            recs.push(r);
        }
    }
    Ok(recs)
}

// ─────────────────────────────────────────────────────────────
// 测试
// ─────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn now() -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()
    }

    fn tmp_path(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("rx-telemetry-test-{}-{}", name, std::process::id()));
        let _ = fs::remove_file(&p);
        p
    }

    #[test]
    fn ring_buffer_cap_drops_oldest() {
        let mut s = TelemetryStore::with_path(tmp_path("cap")).with_cap(3);
        s.push(Record::tool_ok(1.0, "a", None, 1.0));
        s.push(Record::tool_ok(2.0, "b", None, 1.0));
        s.push(Record::tool_ok(3.0, "c", None, 1.0));
        s.push(Record::tool_ok(4.0, "d", None, 1.0));
        assert_eq!(s.buffered(), 3);
        // 最旧的 a 被挤出
        let _ = s.flush();
        let recs = tail_file(s.path(), 10).unwrap();
        assert_eq!(recs.len(), 3);
        let names: Vec<String> = recs
            .iter()
            .filter_map(|r| match r {
                Record::Tool { tool, .. } => Some(tool.clone()),
                _ => None,
            })
            .collect();
        assert_eq!(names, vec!["b", "c", "d"]);
    }

    #[test]
    fn flush_writes_jsonl_lines() {
        let p = tmp_path("flush");
        let mut s = TelemetryStore::with_path(p.clone());
        s.push(Record::tool_ok(now(), "bug_scan", Some("{\"path\":\"x\"}".into()), 12.5));
        s.push(Record::hb(now(), "daemon-self", 300_000.0));
        assert_eq!(s.flush().unwrap(), 2);
        let content = fs::read_to_string(&p).unwrap();
        let lines: Vec<&str> = content.lines().collect();
        assert_eq!(lines.len(), 2);
        // 每行都是合法 JSON 且 kind 正确
        let r1: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(r1["kind"], "tool");
        assert_eq!(r1["tool"], "bug_scan");
        assert_eq!(r1["wall_ms"], 12.5);
        let r2: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
        assert_eq!(r2["kind"], "hb");
        assert_eq!(r2["loop"], "daemon-self");
    }

    #[test]
    fn append_accumulates() {
        let p = tmp_path("append");
        let mut s = TelemetryStore::with_path(p.clone());
        s.push(Record::tool_ok(1.0, "a", None, 1.0));
        let _ = s.flush();
        s.push(Record::tool_ok(2.0, "b", None, 2.0));
        let _ = s.flush();
        assert_eq!(s.flushed(), 2);
        let recs = tail_file(&p, 10).unwrap();
        assert_eq!(recs.len(), 2);
    }

    #[test]
    fn rotate_when_oversize() {
        let p = tmp_path("rotate");
        let mut s = TelemetryStore::with_path(p.clone()).with_max_file(200);
        // 写多轮直到超过 200 字节触发轮转
        for _ in 0..50 {
            s.push(Record::tool_ok(now(), "tool_x", None, 1.0));
            let _ = s.flush();
        }
        // 轮转后主文件存在且 .1 存在
        assert!(p.exists());
        let first = p.with_extension("1.jsonl");
        assert!(first.exists(), "轮转备份应存在");
    }

    #[test]
    fn aggregate_stats() {
        let p = tmp_path("agg");
        let mut s = TelemetryStore::with_path(p.clone());
        // 20 次调用：15 ok 5 err；耗时 1..20ms
        for i in 0..20 {
            let rec = if i % 4 == 3 {
                Record::tool_err(now(), "bug_scan", (i + 1) as f64, "boom")
            } else {
                Record::tool_ok(now(), "bug_scan", None, (i + 1) as f64)
            };
            s.push(rec);
        }
        s.push(Record::hb(now(), "daemon-self", 300_000.0));
        s.push(Record::hb(now(), "daemon-self", 400_000.0));
        s.push(Record::Res { ts: now(), pid: 1, rss_kb: 100, cpu_pct: 0.5 });
        let _ = s.flush();

        let rep = aggregate_file(&p, None).unwrap();
        assert_eq!(rep.total_calls, 20);
        assert_eq!(rep.total_err, 5);
        assert_eq!(rep.overall_err_rate, 0.25);
        assert!(rep.overall_avg_ms > 9.0 && rep.overall_avg_ms < 12.0);
        assert_eq!(rep.overall_max_ms, 20.0);
        let t = rep.tools.get("bug_scan").unwrap();
        assert_eq!(t.count, 20);
        assert_eq!(t.err_count, 5);
        assert_eq!(t.max_ms, 20.0);
        assert!(t.avg_ms > 9.0 && t.avg_ms < 12.0, "avg_ms={}", t.avg_ms);
        let hb = rep.heartbeats.get("daemon-self").unwrap();
        assert_eq!(hb.count, 2);
        assert!(hb.avg_cycle_ms > 300_000.0 && hb.avg_cycle_ms < 400_000.0);
        assert_eq!(rep.res_samples, 1);
    }

    #[test]
    fn aggregate_window_filter() {
        let p = tmp_path("aggwin");
        let mut s = TelemetryStore::with_path(p.clone());
        s.push(Record::tool_ok(100.0, "old", None, 1.0));
        s.push(Record::tool_ok(200.0, "new", None, 2.0));
        let _ = s.flush();
        let rep = aggregate_file(&p, Some(150.0)).unwrap();
        assert_eq!(rep.total_calls, 1);
        assert!(rep.tools.contains_key("new"));
        assert!(!rep.tools.contains_key("old"));
    }

    #[test]
    fn tail_last_n() {
        let p = tmp_path("tail");
        let mut s = TelemetryStore::with_path(p.clone());
        for i in 0..50 {
            s.push(Record::tool_ok(now(), &format!("t{}", i), None, 1.0));
        }
        let _ = s.flush();
        let recs = tail_file(&p, 5).unwrap();
        assert_eq!(recs.len(), 5);
        let names: Vec<String> = recs
            .iter()
            .filter_map(|r| match r {
                Record::Tool { tool, .. } => Some(tool.clone()),
                _ => None,
            })
            .collect();
        assert_eq!(names, vec!["t45", "t46", "t47", "t48", "t49"]);
    }

    #[test]
    fn bad_lines_skipped() {
        let p = tmp_path("bad");
        fs::write(&p, "not-json\n{\"kind\":\"tool\",\"tool\":\"a\",\"ts\":1.0,\"wall_ms\":1.0,\"status\":\"ok\"}\ngarbage\n").unwrap();
        let rep = aggregate_file(&p, None).unwrap();
        assert_eq!(rep.total_calls, 1);
        assert!(rep.skipped_bad_lines >= 1);
    }

    #[test]
    fn missing_file_is_empty() {
        let p = tmp_path("missing");
        let _ = fs::remove_file(&p);
        let rep = aggregate_file(&p, None).unwrap();
        assert_eq!(rep.total_calls, 0);
        let recs = tail_file(&p, 5).unwrap();
        assert!(recs.is_empty());
    }

    #[test]
    fn record_roundtrip_kind_tags() {
        let ts = now();
        let rec = Record::tool_err(ts, "bug_locate", 3.5, "traceback too long");
        let line = rec.to_json_line();
        let back: Record = serde_json::from_str(&line).unwrap();
        match (&back, &rec) {
            (Record::Tool { ts: b_ts, status, err, tool, wall_ms, .. },
             Record::Tool { ts: a_ts, tool: a_tool, wall_ms: a_wall, .. }) => {
                // f64 JSON 往返有 ±1e-6 精度差——近似比较
                assert!((b_ts - a_ts).abs() < 1e-6, "ts 差过大: {b_ts} vs {a_ts}");
                assert_eq!(status, "error");
                assert_eq!(err.as_deref(), Some("traceback too long"));
                assert_eq!(tool, a_tool);
                assert_eq!(wall_ms, a_wall);
            }
            _ => panic!("kind tag 错误"),
        }
    }
}
