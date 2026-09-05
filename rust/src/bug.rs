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
const BUILTINS: &[&str] = &[
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

const SPECIAL: &[&str] = &[
    "self", "cls", "super", "_", "__file__", "__name__", "__doc__", "__package__", "__loader__",
    "__spec__", "__builtins__", "__cached__", "__annotations__", "__all__", "__path__",
    "__main__",
];

#[derive(Clone, Debug)]
struct Issue {
    line: usize,
    rule: &'static str,
    msg: String,
    file: String,
    sev: Option<&'static str>,
    kind: Option<&'static str>,
}

impl Issue {
    /// 键序与 Python 版字典字面量一致：line, rule, msg, file[, severity, kind]
    fn to_value(&self) -> Value {
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

fn is_word(c: u8) -> bool {
    c == b'_' || c.is_ascii_alphanumeric()
}

fn count_newlines_before(src: &str, pos: usize) -> usize {
    src.as_bytes()[..pos].iter().filter(|&&c| c == b'\n').count()
}

// ---------- 聚合 ----------

pub fn bug_scan(root: &str, max_files: usize) -> Value {
    if !Path::new(root).exists() {
        return Value::Obj(vec![("error".to_string(), Value::Str(format!("路径不存在: {}", root)))]);
    }
    let files = iter_files(root, max_files);
    let mut issues: Vec<Issue> = Vec::new();
    let mut files_scanned = 0usize;
    for fp in &files {
        // 单文件直传可能非代码文件：Python 版不计数直接跳过
        let lang = lang_of(fp);
        if lang.is_empty() {
            continue;
        }
        files_scanned += 1;
        // OSError 仍占名额（与 Python 先计数后打开一致）
        let Some(src) = read_text(Path::new(fp)) else { continue };
        match lang {
            "python" => issues.extend(scan_python(&src, fp)),
            "rust" => issues.extend(scan_rust(&src, fp)),
            _ => issues.extend(scan_generic(&src, fp)),
        }
    }
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

fn scan_python(src: &str, path: &str) -> Vec<Issue> {
    let tree = match pyast::parse_module(src) {
        Ok(t) => t,
        Err(e) => {
            return vec![Issue {
                line: e.line,
                rule: "syntax_error",
                msg: format!("语法错误: {}", e.msg),
                file: path.to_string(),
                sev: None,
                kind: None,
            }];
        }
    };
    let mut defined: HashSet<String> = HashSet::new();
    // imported：first-occurrence 序 + last-occurrence 行号（Python dict 语义）
    let mut imported: Vec<(String, usize)> = Vec::new();

    // 走一：定义收集
    let mut q: VecDeque<&PyNode> = VecDeque::new();
    q.push_back(&tree);
    while let Some(n) = q.pop_front() {
        for c in &n.children {
            q.push_back(c);
        }
        match n.kind {
            "FunctionDef" | "AsyncFunctionDef" => {
                defined.insert(n.name.clone());
                if let Some(args) = n.children.first() {
                    for a in &args.children {
                        if matches!(a.kind, "arg" | "vararg" | "kwarg") {
                            defined.insert(a.name.clone());
                        }
                    }
                }
            }
            "ClassDef" => {
                defined.insert(n.name.clone());
                // 怪癖保真：bases 里的 Name 无条件入 defined（Load 也收）；keywords 不收
                for b in n.children.iter().take(n.aux) {
                    collect_names(b, &mut defined);
                }
            }
            "Name" if n.ctx == Ctx::Store => {
                defined.insert(n.name.clone());
            }
            "Import" => {
                for a in &n.children {
                    let key = if !a.name2.is_empty() {
                        a.name2.clone()
                    } else {
                        a.name.split('.').next().unwrap_or("").to_string()
                    };
                    upsert_import(&mut imported, key, n.line);
                }
            }
            "ImportFrom" => {
                // oracle 契约：绑定/遮蔽检查都用 asname（a.asname or a.name）
                for a in &n.children {
                    let key = if !a.name2.is_empty() {
                        a.name2.clone()
                    } else {
                        a.name.clone()
                    };
                    upsert_import(&mut imported, key, n.line);
                }
            }
            "ExceptHandler" if !n.name.is_empty() => {
                defined.insert(n.name.clone());
            }
            "Lambda" => {
                // 怪癖保真：只收 args+kwonly（vararg/kwarg 不算定义）
                // → `lambda *a: a` 报"未定义变量 'a'"，与 Python 版一致
                if let Some(args) = n.children.first() {
                    for a in &args.children {
                        if a.kind == "arg" {
                            defined.insert(a.name.clone());
                        }
                    }
                }
            }
            "Global" | "Nonlocal" => {
                for s in &n.names {
                    defined.insert(s.clone());
                }
            }
            _ => {}
        }
    }
    for (k, _) in &imported {
        defined.insert(k.clone());
    }
    defined.extend(BUILTINS.iter().map(|s| s.to_string()));
    defined.extend(SPECIAL.iter().map(|s| s.to_string()));

    // 走二：问题点（同树同序 BFS——同文件同行 tie 的次序依赖它）
    let mut issues: Vec<Issue> = Vec::new();
    let mut q: VecDeque<&PyNode> = VecDeque::new();
    q.push_back(&tree);
    while let Some(n) = q.pop_front() {
        for c in &n.children {
            q.push_back(c);
        }
        if n.kind == "ExceptHandler" && n.aux == 0 {
            issues.push(Issue {
                line: n.line,
                rule: "bare_except",
                msg: "裸 except（吞掉所有异常）".to_string(),
                file: path.to_string(),
                sev: None,
                kind: None,
            });
        }
        if n.kind == "Call" {
            if let Some(f) = n.children.first() {
                if f.kind == "Name" && matches!(f.name.as_str(), "eval" | "exec" | "compile") {
                    // 只查裸 Name 调用：re.compile 等 Attribute 成员调用天然排除（S61 教训）
                    let hot = f.name == "eval" || f.name == "exec";
                    issues.push(Issue {
                        line: n.line,
                        rule: "eval_exec",
                        msg: format!("python 动态执行 {}()——注入面（裸调用）", f.name),
                        file: path.to_string(),
                        sev: Some(if hot { "high" } else { "med" }),
                        kind: Some(if hot { "definite" } else { "clue" }),
                    });
                }
            }
        }
        if n.kind == "Name" && n.ctx == Ctx::Load && !defined.contains(&n.name) {
            issues.push(Issue {
                line: n.line,
                rule: "undefined_name",
                msg: format!("未定义变量 '{}'", n.name),
                file: path.to_string(),
                sev: None,
                kind: None,
            });
        }
    }
    // 导入遮蔽内建
    for (name, lineno) in &imported {
        if BUILTINS.contains(&name.as_str()) {
            issues.push(Issue {
                line: *lineno,
                rule: "redefined_import",
                msg: format!("导入 '{}' 遮蔽内建名", name),
                file: path.to_string(),
                sev: None,
                kind: None,
            });
        }
    }
    issues
}

fn collect_names(n: &PyNode, out: &mut HashSet<String>) {
    let mut q: VecDeque<&PyNode> = VecDeque::new();
    q.push_back(n);
    while let Some(x) = q.pop_front() {
        for c in &x.children {
            q.push_back(c);
        }
        if x.kind == "Name" {
            out.insert(x.name.clone());
        }
    }
}

fn upsert_import(imported: &mut Vec<(String, usize)>, key: String, line: usize) {
    if let Some(e) = imported.iter_mut().find(|(k, _)| *k == key) {
        e.1 = line;
    } else {
        imported.push((key, line));
    }
}

// ---------- Rust 生产规则 ----------

fn scan_rust(src: &str, path: &str) -> Vec<Issue> {
    let mut issues: Vec<Issue> = Vec::new();
    let lines: Vec<&str> = src.split('\n').collect();
    // 命中行是整行注释则跳过——只作用于 _RUST_RULES 表（bevy 无此闸，与 Python 一致）
    let push_rule = |issues: &mut Vec<Issue>, pos: usize, rule: &'static str, msg: &str| {
        let line = count_newlines_before(src, pos) + 1;
        let text = lines.get(line - 1).copied().unwrap_or("").trim();
        if text.starts_with("//") {
            return;
        }
        issues.push(Issue {
            line,
            rule,
            msg: msg.to_string(),
            file: path.to_string(),
            sev: Some("info"),
            kind: Some("clue"),
        });
    };

    // unwrap：\.unwrap\(\)
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find(".unwrap()") {
        let p = cur + rel;
        push_rule(&mut issues, p, "unwrap", "unwrap()——None/Err 时 panic（线索：确认有 ?/match 兜底即可忽略）");
        cur = p + 9;
    }
    // expect：\.expect\(\s*\"（\s 可跨行）
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find(".expect(") {
        let p = cur + rel;
        let mut j = p + 8;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if src.as_bytes().get(j) == Some(&b'"') {
            push_rule(&mut issues, p, "expect", "expect()——带消息 panic（线索）");
            cur = j + 1;
        } else {
            cur = p + 1;
        }
    }
    // panic / unreachable：\b 词边界 + '!' 字面量
    for (lit, rule, msg) in [
        ("panic!(", "panic", "panic!()——直接崩溃"),
        ("unreachable!(", "unreachable", "unreachable!()——到达即 bug"),
    ] {
        let mut cur = 0usize;
        while let Some(rel) = src[cur..].find(lit) {
            let p = cur + rel;
            if p == 0 || !is_word(src.as_bytes()[p - 1]) {
                let line = count_newlines_before(src, p) + 1;
                let text = lines.get(line - 1).copied().unwrap_or("").trim();
                if !text.starts_with("//") {
                    issues.push(Issue {
                        line,
                        rule,
                        msg: msg.to_string(),
                        file: path.to_string(),
                        sev: Some("high"),
                        kind: Some("definite"),
                    });
                }
                cur = p + lit.len();
            } else {
                cur = p + 1;
            }
        }
    }
    // todo_unimplemented：\b(todo!|unimplemented!)\(——单趟两候选取更靠前者
    {
        const TODO_MSG: &str = "todo!/unimplemented!()——未实现即崩溃";
        let mut cur = 0usize;
        loop {
            let a = src[cur..].find("todo!(").map(|r| cur + r);
            let b = src[cur..].find("unimplemented!(").map(|r| cur + r);
            let p = match (a, b) {
                (Some(x), Some(y)) => x.min(y),
                (Some(x), None) => x,
                (None, Some(y)) => y,
                _ => break,
            };
            if p == 0 || !is_word(src.as_bytes()[p - 1]) {
                let line = count_newlines_before(src, p) + 1;
                let text = lines.get(line - 1).copied().unwrap_or("").trim();
                if !text.starts_with("//") {
                    issues.push(Issue {
                        line,
                        rule: "todo_unimplemented",
                        msg: TODO_MSG.to_string(),
                        file: path.to_string(),
                        sev: Some("high"),
                        kind: Some("definite"),
                    });
                }
                cur = p + if src[p..].starts_with("unimplemented!(") { 15 } else { 6 };
            } else {
                cur = p + 1;
            }
        }
    }
    // as_cast：\bas\s+(i64|i32|u64|u32|f64|f32|usize|isize)\b
    {
        const TYPES: [&str; 8] = ["i64", "i32", "u64", "u32", "f64", "f32", "usize", "isize"];
        let mut cur = 0usize;
        loop {
            let Some(rel) = src[cur..].find("as") else { break };
            let p = cur + rel;
            cur = p + 1;
            if p != 0 && is_word(src.as_bytes()[p - 1]) {
                continue; // \b
            }
            let mut j = p + 2;
            let mut ws = 0usize;
            while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
                j += 1;
                ws += 1;
            }
            if ws == 0 {
                continue;
            }
            let Some(t) = TYPES.iter().find(|t| src[j..].starts_with(**t)) else { continue };
            let after = src.as_bytes().get(j + t.len());
            if after.map_or(true, |c| !is_word(*c)) {
                push_rule(&mut issues, p, "as_cast", "as 类型转换——截断/精度丢失（线索：建议 try_from）");
                cur = p + 2 + ws + t.len();
            }
        }
    }
    // indexing / indexing2：(?<=[\w)\]]) 前环视——左字节是词字符、')' 或 ']'
    {
        let mut cur = 0usize;
        while let Some(rel) = src[cur..].find('[') {
            let p = cur + rel;
            cur = p + 1;
            let prev_ok = p > 0
                && (is_word(src.as_bytes()[p - 1])
                    || src.as_bytes()[p - 1] == b')'
                    || src.as_bytes()[p - 1] == b']');
            if !prev_ok {
                continue;
            }
            let b = src.as_bytes();
            // 规则 7：单个标识符 [A-Za-z_][A-Za-z0-9_]*
            let mut j = p + 1;
            let ident = matches!(b.get(j), Some(c) if c.is_ascii_alphabetic() || *c == b'_');
            if ident {
                j += 1;
                while matches!(b.get(j), Some(c) if c.is_ascii_alphanumeric() || *c == b'_') {
                    j += 1;
                }
                if b.get(j) == Some(&b']') {
                    push_rule(&mut issues, p, "indexing", "索引访问——越界即 panic（线索：建议 .get()）");
                    cur = j + 1;
                    continue;
                }
            }
            // 规则 8：[^\]\[\n]{0,80}\bas\s+(usize|isize|i64|i32|u64|u32)\s*\]
            let mut k = p + 1;
            let mut cnt = 0usize;
            while let Some(&c) = b.get(k) {
                if c == b']' || c == b'[' || c == b'\n' || cnt >= 80 {
                    break;
                }
                k += 1;
                cnt += 1;
            }
            if b.get(k) == Some(&b']') && as_cast_suffix(&src[p + 1..k]) {
                push_rule(&mut issues, p, "indexing", "索引访问（含 as 转换）——越界即 panic（线索：建议 .get()）");
                cur = k + 1;
            }
        }
    }

    // ---- Bevy 规则（无注释行闸；kind 统一 clue）----
    let push_bevy = |issues: &mut Vec<Issue>, pos: usize, rule: &'static str, msg: &str, sev: &'static str| {
        issues.push(Issue {
            line: count_newlines_before(src, pos) + 1,
            rule,
            msg: msg.to_string(),
            file: path.to_string(),
            sev: Some(sev),
            kind: Some("clue"),
        });
    };
    // bevy_old_system（字面量含 '('——.add_systems( 不命中）
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find(".add_system(") {
        let p = cur + rel;
        push_bevy(&mut issues, p, "bevy_old_system", "add_system 旧 API——用 .add_systems（迁移线索）", "info");
        cur = p + 12;
    }
    // bevy_old_startup
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find(".add_startup_system(") {
        let p = cur + rel;
        push_bevy(&mut issues, p, "bevy_old_startup", "add_startup_system 旧 API——用 .add_systems(Startup, ...)（迁移线索）", "info");
        cur = p + 20;
    }
    // bevy_event_iter：EventReader<[^>]+>\.iter\(（[^>]+ 跨行）
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("EventReader<") {
        let p = cur + rel;
        cur = p + 1;
        let content_start = p + 12;
        let Some(grel) = src[content_start..].find('>') else { continue };
        let gt = content_start + grel;
        if gt == content_start {
            continue; // [^>]+ 至少 1 字符
        }
        if src[gt + 1..].starts_with(".iter(") {
            push_bevy(&mut issues, p, "bevy_event_iter", "EventReader.iter 旧 API——用 .read()（迁移线索）", "info");
            cur = gt + 7;
        }
    }
    // bevy_text_old：TextBundle\s*\{（\s 跨行）
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("TextBundle") {
        let p = cur + rel;
        let mut j = p + 10;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if src.as_bytes().get(j) == Some(&b'{') {
            push_bevy(&mut issues, p, "bevy_text_old", "TextBundle 旧式——用 Text::new（迁移线索）", "info");
            cur = j + 1;
        } else {
            cur = p + 1;
        }
    }
    // bevy_query_single
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find(".single()") {
        let p = cur + rel;
        push_bevy(&mut issues, p, "bevy_query_single", "query.single() 自 Bevy 0.16 起返回 Result——Err 静默失败是逻辑雷（用 let Ok = .. else return 兜）；.single().unwrap() 才会 panic（09-05 VoxelForge 11 处甄别：全部正确 else-return，零真险）（线索）", "low");
        cur = p + 9;
    }
    // bevy_phys_locked_axes_bits
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("LockedAxes::from_bits(") {
        let p = cur + rel;
        let mut j = p + 22;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if src[j..].starts_with("0b") {
            push_bevy(&mut issues, p, "bevy_phys_locked_axes_bits", "LockedAxes 魔数位——位序易错（VoxelForge 0b000_101 曾误读为锁平移），用具名位常量 ROTATION_X/TRANSLATION_* 核对", "info");
            cur = j + 2;
        } else {
            cur = p + 1;
        }
    }
    // bevy_phys_static_with_velocity：双分支受限惰性匹配（见 try_static_velocity）
    {
        let mut cur = 0usize;
        while let Some(rel) = src[cur..].find("spawn") {
            let p = cur + rel;
            match try_static_velocity(src, p) {
                Some(end) => {
                    push_bevy(&mut issues, p, "bevy_phys_static_with_velocity", "spawn 元组里 RigidBody::Static 携带速度/受力组件——Static 体不响应力与速度，写了不生效（::ZERO 冗余不报；VoxelForge 09-05 甄别：matches! 判断与测试 fixture 为误报源；S74 两个分支都锚 spawn 元组——前一条 spawn 的速度逗号 + 200 字符内另一条 Static spawn 不再跨语句误连）", "low");
                    cur = end;
                }
                None => cur = p + 1,
            }
        }
    }
    // bevy_phys_manual_support_force：apply_force_at_point\(\s*Vec3::Y\b
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("apply_force_at_point(") {
        let p = cur + rel;
        let mut j = p + 21;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if src[j..].starts_with("Vec3::Y") {
            let after = src.as_bytes().get(j + 7);
            if after.map_or(true, |c| !is_word(*c)) {
                push_bevy(&mut issues, p, "bevy_phys_manual_support_force", "手写竖直支撑/弹簧力（Vec3::Y × f）——多轮/多执行器各自封顶≠总和有界：四轮同压可叠到 3×车重持续弹起（VoxelForge 09-04 四轮弹跳床案），须有整车总力预算", "med");
                cur = j + 7;
                continue;
            }
        }
        cur = p + 1;
    }

    // ---- 测试代码降级：文件级（tests 目录 / *_test.rs）+ 行级（#[cfg(test)] 起）----
    let norm = path.replace('\\', "/").replace("_tmp/", "");
    let is_test_file =
        norm.ends_with("_test.rs") || contains_tests_segment(&norm);
    let mut test_start_line: Option<usize> = None;
    if !is_test_file {
        test_start_line = find_cfg_test_line(src);
    }
    for i in issues.iter_mut() {
        let in_test =
            is_test_file || test_start_line.map_or(false, |t| i.line >= t);
        if in_test && matches!(i.rule, "unwrap" | "expect" | "as_cast" | "indexing") {
            i.sev = Some("low");
            i.kind = Some("clue");
            i.msg.push_str("（测试代码，降级）");
        } else if in_test && i.rule == "panic" {
            i.sev = Some("low");
            i.kind = Some("clue");
            i.msg.push_str("（测试上下文，通常为断言用途，降级）");
        }
    }
    issues
}

/// 规则 8 的尾部校验：^.*\bas\s+(usize|isize|i64|i32|u64|u32)\s*$（'as' 带词边界）
fn as_cast_suffix(inner: &str) -> bool {
    let nb = inner.as_bytes();
    for (pos, _) in inner.match_indices("as") {
        if pos != 0 && is_word(nb[pos - 1]) {
            continue;
        }
        let mut j = pos + 2;
        let mut ws = 0usize;
        while matches!(nb.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
            ws += 1;
        }
        if ws == 0 {
            continue;
        }
        let mut matched = false;
        for t in ["usize", "isize", "i64", "i32", "u64", "u32"] {
            if inner[j..].starts_with(t) {
                j += t.len();
                matched = true;
                break;
            }
        }
        if !matched {
            continue;
        }
        while matches!(nb.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if j == inner.len() {
            return true;
        }
    }
    false
}

/// /tests 段检测（re.search(r"/tests(?:/|$)") 等价）
fn contains_tests_segment(norm: &str) -> bool {
    let mut from = 0usize;
    while let Some(rel) = norm[from..].find("/tests") {
        let p = from + rel;
        match norm.as_bytes().get(p + 6) {
            None => return true,
            Some(b'/') => return true,
            _ => from = p + 1,
        }
    }
    false
}

/// ^#\[\s*cfg\s*\(\s*test\s*\)\s*\]（MULTILINE）：返回属性行号（首处）
fn find_cfg_test_line(src: &str) -> Option<usize> {
    for (idx, line) in src.split('\n').enumerate() {
        let b = line.as_bytes();
        let mut j = 0usize;
        if !line.starts_with("#[") {
            continue;
        }
        j += 2;
        while matches!(b.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if !line[j..].starts_with("cfg") {
            continue;
        }
        j += 3;
        while matches!(b.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if b.get(j) != Some(&b'(') {
            continue;
        }
        j += 1;
        while matches!(b.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if !line[j..].starts_with("test") {
            continue;
        }
        j += 4;
        while matches!(b.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if b.get(j) != Some(&b')') {
            continue;
        }
        j += 1;
        while matches!(b.get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        if b.get(j) == Some(&b']') {
            return Some(idx + 1);
        }
    }
    None
}

// ---------- bevy_phys_static_with_velocity：双分支受限惰性匹配 ----------

/// spawn \s* \( \s* \(? \s* UNITS{0,160} 惰性 → 分支 A：RigidBody::Static \s* , [200字符]惰性 MARKER
///                                    分支 B：MARKER \s* , [200字符]惰性 RigidBody::Static \s* ,
/// 返回 match 终点（finditer 非重叠的游标依据）。
/// \(? 贪婪优先吃掉第二个 '('（整条元组直落单元起点），失败回溯不吃——
/// 单元从第二个 '(' 起（括号组整体算一个单元）。
fn try_static_velocity(src: &str, p: usize) -> Option<usize> {
    let mut i = p + 5;
    i = skip_ws(src, i);
    if src.as_bytes().get(i) != Some(&b'(') {
        return None;
    }
    let i1 = skip_ws(src, i + 1);
    if src.as_bytes().get(i1) == Some(&b'(') {
        let i2 = skip_ws(src, i1 + 1);
        if let Some(end) = units_then(src, i2, true) {
            return Some(end);
        }
        if let Some(end) = units_then(src, i2, false) {
            return Some(end);
        }
    }
    if let Some(end) = units_then(src, i1, true) {
        return Some(end);
    }
    units_then(src, i1, false)
}

fn units_then(src: &str, start: usize, branch_a: bool) -> Option<usize> {
    let mut j = start;
    let mut count = 0usize;
    loop {
        if branch_a {
            if src[j..].starts_with("RigidBody::Static") {
                let k = skip_ws(src, j + "RigidBody::Static".len());
                if src.as_bytes().get(k) == Some(&b',') {
                    if let Some(end) = scan_for_marker(src, k + 1, 200) {
                        return Some(end);
                    }
                }
            }
        } else if let Some(mend) = marker_at(src, j) {
            let k = skip_ws(src, mend);
            if src.as_bytes().get(k) == Some(&b',') {
                if let Some(end) = scan_for_rigid(src, k + 1, 200) {
                    return Some(end);
                }
            }
        }
        if count >= 160 {
            return None;
        }
        match unit_step(src, j) {
            Some(j2) => {
                j = j2;
                count += 1;
            }
            None => return None,
        }
    }
}

/// 单元 (?:[^);]|\([^)]*\))：非 )/; 字符，或一层括号组（内层无 ')'）
/// 按字符步进（Python [^);] 计字符；按字节会踩进 CJK 多字节序列中间）
fn unit_step(src: &str, j: usize) -> Option<usize> {
    let c = src[j..].chars().next()?;
    match c {
        ')' | ';' => None,
        '(' => {
            let close = src[j + 1..].find(')')?;
            Some(j + 1 + close + 1)
        }
        _ => Some(j + c.len_utf8()),
    }
}

/// MARKER = LinearVelocity(?!::ZERO) | ExternalForce | AngularVelocity(?!::ZERO)
/// 返回 marker 文本终点；::ZERO 的两兄弟在当前位置失配后由调用方前进 1 字符（回溯等价）
fn marker_at(src: &str, pos: usize) -> Option<usize> {
    for (name, zero_ok) in [("LinearVelocity", false), ("ExternalForce", true), ("AngularVelocity", false)] {
        if src[pos..].starts_with(name) {
            let end = pos + name.len();
            if zero_ok || !src[end..].starts_with("::ZERO") {
                return Some(end);
            }
        }
    }
    None
}

/// [\s\S]{0,200}?MARKER：按字符计数（Python {n} 计字符不计字节）
fn scan_for_marker(src: &str, from: usize, cap: usize) -> Option<usize> {
    let mut pos = from;
    let mut cnt = 0usize;
    loop {
        if let Some(end) = marker_at(src, pos) {
            return Some(end);
        }
        if cnt >= cap {
            return None;
        }
        match src[pos..].chars().next() {
            Some(c) => {
                pos += c.len_utf8();
                cnt += 1;
            }
            None => return None,
        }
    }
}

/// [\s\S]{0,200}?RigidBody::Static\s*,
fn scan_for_rigid(src: &str, from: usize, cap: usize) -> Option<usize> {
    let mut pos = from;
    let mut cnt = 0usize;
    loop {
        if src[pos..].starts_with("RigidBody::Static") {
            let k = skip_ws(src, pos + "RigidBody::Static".len());
            if src.as_bytes().get(k) == Some(&b',') {
                return Some(k + 1);
            }
        }
        if cnt >= cap {
            return None;
        }
        match src[pos..].chars().next() {
            Some(c) => {
                pos += c.len_utf8();
                cnt += 1;
            }
            None => return None,
        }
    }
}

fn skip_ws(src: &str, mut i: usize) -> usize {
    while matches!(src.as_bytes().get(i), Some(c) if c.is_ascii_whitespace()) {
        i += 1;
    }
    i
}

// ---------- 通用正则规则 ----------

fn scan_generic(src: &str, path: &str) -> Vec<Issue> {
    let mut issues: Vec<Issue> = Vec::new();
    let push = |issues: &mut Vec<Issue>, pos: usize, rule: &'static str, msg: &str| {
        issues.push(Issue {
            line: count_newlines_before(src, pos) + 1,
            rule,
            msg: msg.to_string(),
            file: path.to_string(),
            sev: Some("med"),
            kind: Some("clue"),
        });
    };
    // assert_always_true：assert\s+True\b——无左侧边界（保真：myassert True 也命中）
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("assert") {
        let p = cur + rel;
        let mut j = p + 6;
        let mut ws = 0usize;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
            ws += 1;
        }
        if ws >= 1 && src[j..].starts_with("True") {
            let after = src.as_bytes().get(j + 4);
            if after.map_or(true, |c| !is_word(*c)) {
                push(&mut issues, p, "assert_always_true", "恒真断言（永远通过，无意义）");
                cur = j + 4;
                continue;
            }
        }
        cur = p + 1;
    }
    // equal_float：==\s*\d+\.\d+
    let mut cur = 0usize;
    while let Some(rel) = src[cur..].find("==") {
        let p = cur + rel;
        let mut j = p + 2;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_whitespace()) {
            j += 1;
        }
        let d0 = j;
        while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_digit()) {
            j += 1;
        }
        if j > d0 && src.as_bytes().get(j) == Some(&b'.') {
            j += 1;
            let d1 = j;
            while matches!(src.as_bytes().get(j), Some(c) if c.is_ascii_digit()) {
                j += 1;
            }
            if j > d1 {
                push(&mut issues, p, "equal_float", "浮点相等比较（精度风险）");
                cur = j;
                continue;
            }
        }
        cur = p + 1;
    }
    // eval_exec：(?<![.\w])(eval|exec|execSync)\s*\(——成员调用 .exec( 排除（S61 教训）
    let mut cur = 0usize;
    loop {
        let cand = ["eval", "exec", "execSync"]
            .iter()
            .filter_map(|name| src[cur..].find(name).map(|r| cur + r))
            .min();
        let Some(p) = cand else { break };
        let prev_ok = p == 0
            || {
                let c = src.as_bytes()[p - 1];
                c != b'.' && !is_word(c)
            };
        if prev_ok {
            // 交替序 eval → exec → execSync（execSync 在 exec 失配后回溯胜出）
            let mut hit = None;
            for name in ["eval", "exec", "execSync"] {
                if src[p..].starts_with(name) {
                    let j = skip_ws(src, p + name.len());
                    if src.as_bytes().get(j) == Some(&b'(') {
                        hit = Some(j + 1);
                        break;
                    }
                }
            }
            if let Some(end) = hit {
                push(&mut issues, p, "eval_exec", "eval/exec 动态执行（安全风险）");
                cur = end;
                continue;
            }
        }
        cur = p + 1;
    }
    issues
}
