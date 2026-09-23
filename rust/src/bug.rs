//! bug —— bug_scan 原生实现（S83）：Python 迷你 AST 规则 + Rust 生产规则 + 通用正则。
//!
//! 与 tools/scan.py::_scan_python/_scan_rust/_scan_generic 逐条对齐（对照实验为删码依据）：
//! - _scan_python：两趟 BFS（ast.walk 等价，children 即 ASDL 字段序）；defined 收集
//!   含三处怪癖（Lambda 漏 vararg/kwarg、ClassDef bases 无条件收、with 元组靠全局
//!   Store 收集兜底）；issues 判定只认 Load 上下文 Name / 裸 Call(eval|exec|compile) /
//!   裸 except / 内建遮蔽（first-occurrence 序 + last-occurrence 行号）
//! - _scan_rust：8 条规则全手写匹配（正则怪癖逐一复刻：expect 的 \s* 过引号、
//!   \b 词边界、(?<=[\w)\]]) 左环视=前字节为词字符或 ')'、indexing2 的 80 字符
//!   内容窗 + as 后缀、注释行跳过只作用于本表）；bevy 8 条随后、无注释闸；
//!   测试降级按 tests 目录 / *_test.rs / #[cfg(test)] 行号三通道
//! - _scan_generic：assert 无左边界（myassert True 也命中——保真）、eval 的
//!   (?<![.\w]) 成员调用排除、execSync 靠交替回溯胜出
//! - 聚合：by_rule/by_severity 首现序（保序 Obj）、排序 (severity, file, line)
//!   稳定排序（缺失 severity 落 info 档）、名额只计代码文件、OSError 仍计名额

use crate::json::Value;
use crate::pyast::{self, Ctx, PyNode};
use crate::scan::{iter_files, lang_of, read_text};
use std::collections::{HashSet, VecDeque};
use std::path::Path;

// S83：内嵌 3.14 的 dir(builtins) 快照（160 名）——双解释器共用同一口径，
// 比 Python 版"运行时取宿主 builtins"更一致（3.11 仅缺 PythonFinalizationError /
// _IncompleteInputError 两个新名，语料不触碰）。
pub(crate) const BUILTINS: &[&str] = &[
    "ArithmeticError", "AssertionError", "AttributeError", "BaseException",
    "BaseExceptionGroup", "BlockingIOError", "BrokenPipeError", "BufferError", "BytesWarning",
    "ChildProcessError", "ConnectionAbortedError", "ConnectionError", "ConnectionRefusedError",
    "ConnectionResetError", "DeprecationWarning", "EOFError", "Ellipsis", "EncodingWarning",
    "EnvironmentError", "Exception", "ExceptionGroup", "False", "FileExistsError",
    "FileNotFoundError", "FloatingPointError", "FutureWarning", "GeneratorExit", "IOError",
    "ImportError", "ImportWarning", "IndentationError", "IndexError", "InterruptedError",
    "IsADirectoryError", "KeyError", "KeyboardInterrupt", "LookupError", "MemoryError",
    "ModuleNotFoundError", "NameError", "None", "NotADirectoryError", "NotImplemented",
    "NotImplementedError", "OSError", "OverflowError", "PendingDeprecationWarning",
    "PermissionError", "ProcessLookupError", "PythonFinalizationError", "RecursionError",
    "ReferenceError", "ResourceWarning", "RuntimeError", "RuntimeWarning",
    "StopAsyncIteration", "StopIteration", "SyntaxError", "SyntaxWarning", "SystemError",
    "SystemExit", "TabError", "TimeoutError", "True", "TypeError", "UnboundLocalError",
    "UnicodeDecodeError", "UnicodeEncodeError", "UnicodeError", "UnicodeTranslateError",
    "UnicodeWarning", "UserWarning", "ValueError", "Warning", "WindowsError",
    "ZeroDivisionError", "_IncompleteInputError", "__build_class__", "__debug__", "__doc__",
    "__import__", "__loader__", "__name__", "__package__", "__spec__", "abs", "aiter", "all",
    "anext", "any", "ascii", "bin", "bool", "breakpoint", "bytearray", "bytes", "callable",
    "chr", "classmethod", "compile", "complex", "copyright", "credits", "delattr", "dict",
    "dir", "divmod", "enumerate", "eval", "exec", "exit", "filter", "float", "format",
    "frozenset", "getattr", "globals", "hasattr", "hash", "help", "hex", "id", "input", "int",
    "isinstance", "issubclass", "iter", "len", "license", "list", "locals", "map", "max",
    "memoryview", "min", "next", "object", "oct", "open", "ord", "pow", "print", "property",
    "quit", "range", "repr", "reversed", "round", "set", "setattr", "slice", "sorted",
    "staticmethod", "str", "sum", "super", "tuple", "type", "vars", "zip",
];

pub(crate) const SPECIAL: &[&str] = &[
    "self", "cls", "super", "_", "__file__", "__name__", "__doc__", "__package__", "__loader__",
    "__spec__", "__builtins__", "__cached__", "__annotations__", "__all__", "__path__",
    "__main__",
];

#[derive(Clone, Debug)]
pub(crate) struct Issue {
    pub(crate) line: usize,
    pub(crate) rule: &'static str,
    pub(crate) msg: String,
    pub(crate) file: String,
    pub(crate) sev: Option<&'static str>,
    pub(crate) kind: Option<&'static str>,
}

impl Issue {
    /// 键序与 Python 版字典字面量一致：line, rule, msg, file[, severity, kind]
    pub(crate) fn to_value(&self) -> Value {
        let mut pairs = vec![
            ("line".to_string(), Value::Int(self.line as i128)),
            ("rule".to_string(), Value::Str(self.rule.to_string())),
            ("msg".to_string(), Value::Str(self.msg.clone())),
            ("file".to_string(), Value::Str(self.file.clone())),
        ];
        if let Some(s) = self.sev {
            pairs.push(("severity".to_string(), Value::Str(s.to_string())));
        }
        if let Some(k) = self.kind {
            pairs.push(("kind".to_string(), Value::Str(k.to_string())));
        }
        Value::Obj(pairs)
    }
}

pub(crate) fn is_word(c: u8) -> bool {
    c == b'_' || c.is_ascii_alphanumeric()
}

pub(crate) fn count_newlines_before(src: &str, pos: usize) -> usize {
    src.as_bytes()[..pos].iter().filter(|&&c| c == b'\n').count()
}

// ---------- 聚合 ----------

pub fn bug_scan(root: &str, max_files: usize) -> Value {
    if !Path::new(root).exists() {
        return Value::Obj(vec![("error".to_string(), Value::Str(format!("路径不存在: {}", root)))]);
    }
    let files = iter_files(root, max_files);
    // S153：分块并行 + 有序收集（逐文件独立；出口稳定排序 → 合并顺序无关）
    let (mut issues, files_scanned) = scan_files(&files);
    // 首现序计数（Python dict 插入序等价）
    let mut by_rule: Vec<(String, i128)> = Vec::new();
    let mut by_sev: Vec<(String, i128)> = Vec::new();
    let bump = |acc: &mut Vec<(String, i128)>, key: &str| {
        if let Some(e) = acc.iter_mut().find(|(k, _)| k == key) {
            e.1 += 1;
        } else {
            acc.push((key.to_string(), 1));
        }
    };
    for i in &issues {
        bump(&mut by_rule, i.rule);
        bump(&mut by_sev, i.sev.unwrap_or("info"));
    }
    // 稳定排序（Rust sort_by 稳定 = Python list.sort）：severity 缺失落 info 档
    let rank = |s: Option<&str>| match s {
        Some("high") => 0usize,
        Some("med") => 1,
        Some("low") => 2,
        _ => 3,
    };
    issues.sort_by(|a, b| {
        rank(a.sev)
            .cmp(&rank(b.sev))
            .then_with(|| a.file.cmp(&b.file))
            .then_with(|| a.line.cmp(&b.line))
    });
    Value::Obj(vec![
        ("files".to_string(), Value::Int(files_scanned as i128)),
        ("total".to_string(), Value::Int(issues.len() as i128)),
        (
            "by_rule".to_string(),
            Value::Obj(by_rule.into_iter().map(|(k, v)| (k, Value::Int(v))).collect()),
        ),
        (
            "by_severity".to_string(),
            Value::Obj(by_sev.into_iter().map(|(k, v)| (k, Value::Int(v))).collect()),
        ),
        ("issues".to_string(), Value::Arr(issues.iter().map(|i| i.to_value()).collect())),
    ])
}

// ---------- Python 迷你 AST 规则 ----------


// ── S168：按域拆出的子模块（子目录 bug/）
mod py;
mod rust;
mod phys;
mod generic;
pub(crate) use self::{py::*, rust::*, phys::*, generic::*};   // S168：子模块条目再导出（子模块 use super::* 即可见）

/// S153：单文件扫描（原循环体逐字搬入）。返回 (issues, 是否计入 files_scanned)。
pub(crate) fn scan_one(fp: &str) -> (Vec<Issue>, bool) {
    let lang = lang_of(fp);
    if lang.is_empty() {
        return (Vec::new(), false);        // 非代码文件：不计数直接跳过（Python 版同）
    }
    // OSError 仍占名额（与 Python 先计数后打开一致）
    let Some(src) = read_text(Path::new(fp)) else { return (Vec::new(), true) };
    let issues = match lang {
        "python" => scan_python(&src, fp),
        "rust" => scan_rust(&src, fp),
        _ => scan_generic(&src, fp),
    };
    (issues, true)
}

/// S153：分块并行（**S167：线程数＝用满可用并行度**；文件 < 8 走串行，与旧版逐字节同）。
pub(crate) fn scan_files(files: &[String]) -> (Vec<Issue>, usize) {
    let n = crate::par::par_degree(0); // S167：0 = 用满可用并行度（按机器来，不再固定 8）
    if files.len() < 8 || n <= 1 {
        let mut iss = Vec::new();
        let mut cnt = 0usize;
        for fp in files {
            let (i2, c) = scan_one(fp);
            iss.extend(i2);
            if c { cnt += 1 }
        }
        return (iss, cnt);
    }
    let chunk = files.len().div_ceil(n);
    let mut iss = Vec::new();
    let mut cnt = 0usize;
    std::thread::scope(|s| {
        let handles: Vec<_> = files.chunks(chunk).map(|c| {
            s.spawn(move || {
                let mut i2 = Vec::new();
                let mut c2 = 0usize;
                for fp in c {
                    let (x, cc) = scan_one(fp);
                    i2.extend(x);
                    if cc { c2 += 1 }
                }
                (i2, c2)
            })
        }).collect();
        for h in handles {
            let (mut i2, c2) = h.join().unwrap_or_default();
            iss.append(&mut i2);
            cnt += c2;
        }
    });
    (iss, cnt)
}
