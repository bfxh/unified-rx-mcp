//! taint —— Rust 污点引擎（S78，VULN-HUNTING P1-a；S128 跨文件链）。
//!
//! 从"模式匹配"升级到"来源→汇点"浅数据流：Python 子集词法器（三引号/f-string/
//! 续行/缩进）+ 函数域污点传播 + 同文件一跳跨函数（实参→形参、污染返回值→调用点）。
//! S128 追加跨文件链：全扫描集唯一名连边 + nameres 调用图解析结果增强（别名
//! from x import y as z / 模块属性调用不再靠文本名），链证据 origin 随跳保留。
//!
//! 模型假设（对 MCP 工具箱尤其成立）：函数参数 = 攻击者可控来源（宿主传参）；
//! 另认 sys.argv / input() / os.getenv / os.environ / sys.stdin / request.* / .recv(。
//! 汇点三类：exec（eval/exec/compile/os.system/os.popen/subprocess.*/pickle.loads…，
//! severity=high）、path（open/os.remove/rename/makedirs/…/shutil.*/os.walk，
//! severity=med）、sql（.execute，severity=med）。
//! 净化器：basename/secure_filename/int()/float()/_fs_resolve（S73 修复方式即净化器）
//! 及 `<var>.name` 取文件名；净化区内的污点不再计数。
//!
//! `naive` 模式：跳过污点判定，凡汇点实参/接收者含非字面量标识符就报——作为
//! "模式匹配基线"供 S73 重放对比（验收：污点版命中 ≤ 基线一半且真问题一条不漏）。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::json;
use crate::nameres;

// ---------------------------------------------------------------- 发现

#[derive(Clone, Debug)]
pub struct Finding {
    pub file: String,
    pub line: usize,
    pub sink: String,
    pub var: String,
    pub source_line: usize,
    pub source_kind: String,
    pub flow: String,     // direct / interproc / cross（S128：跨文件链）
    pub severity: String, // high / med
    pub kind: String,     // definite / clue / naive
    pub origin: Option<String>, // S128：跨文件链证据（flow=cross 时必有）
}

#[derive(Default)]
pub struct ScanResult {
    pub files_scanned: usize,
    pub findings: Vec<Finding>,
    pub errors: Vec<String>,
    /// S128：因全局同名多义而放弃连边的函数名数（如实计数，0 = 全部可解析）
    pub cross_skipped_ambiguous: usize,
}

impl Finding {
    pub(crate) fn to_value(&self) -> json::Value {
        let mut pairs: Vec<(String, json::Value)> = vec![
            ("file".into(), json::Value::Str(self.file.clone())),
            ("line".into(), json::Value::Int(self.line as i128)),
            ("sink".into(), json::Value::Str(self.sink.clone())),
            ("var".into(), json::Value::Str(self.var.clone())),
            ("source_line".into(), json::Value::Int(self.source_line as i128)),
            ("source_kind".into(), json::Value::Str(self.source_kind.clone())),
            ("flow".into(), json::Value::Str(self.flow.clone())),
            ("severity".into(), json::Value::Str(self.severity.clone())),
            ("kind".into(), json::Value::Str(self.kind.clone())),
        ];
        if let Some(o) = &self.origin {
            pairs.push(("origin".into(), json::Value::Str(o.clone())));
        }
        json::Value::Obj(pairs)
    }
}

/// 小工具：从 (名, 值) 数组造保序对象。
pub(crate) fn obj<const N: usize>(pairs: [(&str, json::Value); N]) -> json::Value {
    json::Value::Obj(pairs.iter().map(|(k, v)| (k.to_string(), v.clone())).collect())
}

// ---------------------------------------------------------------- 词法器

#[derive(Clone, Debug, PartialEq)]
pub(crate) enum Tk {
    Id(String),
    Num,
    Str,
    Op(String),
    Newline,
    Indent,
    Dedent,
}

#[derive(Clone, Debug)]
pub(crate) struct Tok {
    pub(crate) tk: Tk,
    pub(crate) line: usize,
}

pub(crate) fn is_opener(s: &str) -> bool {
    s == "(" || s == "[" || s == "{"
}

pub(crate) fn is_closer(s: &str) -> bool {
    s == ")" || s == "]" || s == "}"
}

pub(crate) const OP3: [&str; 5] = ["**=", "//=", "...", ">>=", "<<="];
pub(crate) const OP2: [&str; 19] = ["**", "//", "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=",
    "%=", "&=", "|=", "^=", "->", "<<", ">>", ":=", "@="];


// ── S168：按域拆出的子模块（子目录 taint/）
mod lex;
mod types;
mod analyzer_scan;
mod analyzer_flow;
mod xfile;
pub(crate) use self::{lex::*, types::*, xfile::*};   // S168：子模块条目再导出（impl-only 的 analyzer_flow/analyzer_scan 不进 glob——会 unused）

pub(crate) fn walk_py(root: &Path, out: &mut Vec<PathBuf>) {
    let mut stack = vec![root.to_path_buf()];
    let skip = [".git", "__pycache__", ".pytest_cache", "venv", ".venv",
        "node_modules", "target", ".codegraph", "backups"];
    while let Some(d) = stack.pop() {
        let rd = match std::fs::read_dir(&d) {
            Ok(r) => r,
            Err(_) => continue,
        };
        for e in rd.flatten() {
            let p = e.path();
            let name = e.file_name().to_string_lossy().to_string();
            if p.is_dir() {
                if !skip.contains(&name.as_str()) {
                    stack.push(p);
                }
            } else if name.ends_with(".py") {
                out.push(p);
            }
        }
    }
}

/// 扫描目录/单文件（S128 起跨文件链默认开启）。root 必须已过沙盒（CLI 层负责）。
pub fn scan_path(root: &Path, naive: bool) -> ScanResult {
    scan_path_opts(root, naive, true)
}

/// S128：cross=false 关闭跨文件传播（逐字节回到 S78 语义，供 A/B 回归对照）。
pub fn scan_path_opts(root: &Path, naive: bool, cross: bool) -> ScanResult {
    let mut files: Vec<PathBuf> = Vec::new();
    if root.is_file() {
        files.push(root.to_path_buf());
    } else {
        walk_py(root, &mut files);
        files.sort();
    }
    let mut res = ScanResult {
        files_scanned: files.len(),
        ..Default::default()
    };
    // S128：先全量分析（保留各文件 Analyzer 供跨文件传播改污点表），
    // 传播完再统一产出 findings——单文件时代的"分析完即弃"不再够用。
    // S153：逐文件分析**分块并行**（分析独立、传播后置）——分块为连续切片，
    // 按块序拼回 = 文件序，errors 同序；输出与串行逐字节同（金标准锁死）。
    let (mut units, mut errs) = analyze_files(&files, root, naive);
    res.errors.append(&mut errs);
    if cross && !naive {
        let dbg = std::env::var("UNIFIED_RX_DEBUG_TIMING").is_ok();
        let t0 = std::time::Instant::now();
        let targets = callgraph_targets(root);
        let t1 = std::time::Instant::now();
        res.cross_skipped_ambiguous = cross_file_propagate(&mut units, &targets);
        if dbg {
            eprintln!("TIMING callgraph={}ms propagate={}ms",
                      t0.elapsed().as_millis(), t1.elapsed().as_millis());
        }
    }
    for a in &units {
        res.findings.extend(a.pass3_findings());
    }
    res
}

/// S153：单文件分析（原循环体逐字搬入）。返回 (Analyzer 可选, 错误串可选)。
pub(crate) fn analyze_one(f: &Path, root: &Path, naive: bool) -> (Option<Analyzer>, Option<String>) {
    let bytes = match crate::rcache::read(f) {
        Ok(b) => b,
        Err(e) => return (None, Some(format!("{}: {}", f.display(), e))),
    };
    let src = String::from_utf8_lossy(&bytes).to_string();
    let display = if root.is_file() {
        root.file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| root.to_string_lossy().to_string())
    } else {
        f.strip_prefix(root).unwrap_or(f).to_string_lossy().replace('\\', "/")
    };
    let mut a = Analyzer::new(&display, &src, naive);
    a.pass1();
    a.pass2_interproc();
    (Some(a), None)
}

/// S153：分块并行 + 有序收集（**S167：线程数＝用满可用并行度**；文件 < 4 走串行）。
pub(crate) fn analyze_files(files: &[PathBuf], root: &Path, naive: bool)
    -> (Vec<Analyzer>, Vec<String>) {
    let n = crate::par::par_degree(0); // S167：0 = 用满可用并行度（按机器来，不再固定 8）
    if files.len() < 4 || n <= 1 {
        let mut us = Vec::new();
        let mut es = Vec::new();
        for f in files {
            let (u, e) = analyze_one(f, root, naive);
            if let Some(u) = u { us.push(u) }
            if let Some(e) = e { es.push(e) }
        }
        return (us, es);
    }
    let chunk = files.len().div_ceil(n);
    let mut us = Vec::new();
    let mut es = Vec::new();
    std::thread::scope(|s| {
        let handles: Vec<_> = files.chunks(chunk).map(|c| {
            s.spawn(move || {
                let mut u2 = Vec::new();
                let mut e2 = Vec::new();
                for f in c {
                    let (u, e) = analyze_one(f, root, naive);
                    if let Some(u) = u { u2.push(u) }
                    if let Some(e) = e { e2.push(e) }
                }
                (u2, e2)
            })
        }).collect();
        for h in handles {
            let (mut u2, mut e2) = h.join().unwrap_or_default();
            us.append(&mut u2);
            es.append(&mut e2);
        }
    });
    (us, es)
}

/// 结果 → json::Value（零依赖序列化出口）。
pub fn result_to_json(r: &ScanResult) -> json::Value {
    let cross_n = r.findings.iter().filter(|f| f.flow == "cross").count();
    obj([
        ("files_scanned", json::Value::Int(r.files_scanned as i128)),
        (
            "findings",
            json::Value::Arr(r.findings.iter().map(|f| f.to_value()).collect()),
        ),
        (
            "errors",
            json::Value::Arr(r.errors.iter().map(|e| json::Value::Str(e.clone())).collect()),
        ),
        // S128：跨文件链计数 + 多义放弃计数（如实可复核）
        ("cross_file_findings", json::Value::Int(cross_n as i128)),
        (
            "cross_skipped_ambiguous",
            json::Value::Int(r.cross_skipped_ambiguous as i128),
        ),
    ])
}
