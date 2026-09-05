//! ast_scan 原生实现（S84）——结构化扫描域：Python 真 AST / JS 词法掩码+调用面 /
//! Rust 词法掩码+结构化信号 + S16 跨文件可达性。
//!
//! 与 tools/astscan.py 逐条对齐（对照实验后 Python 侧退役为薄壳）：
//! - Python：pyast::parse_module（S83 迷你解析器，S84 扩展 col/字符串值/算子名），
//!   ast.walk 等价 BFS；Call 节点的 col = 链首 token（CPython 口径）
//! - JS：手写状态机掩码（char 口径保长度）+ 调用面提取（_CALL_RE 手写等价：
//!   前视非 [.\w$]、可选 new\s+、链 seg(.seg)*、\s*、'('），finditer 不重叠续扫
//! - Rust：掩码 + 每行结构信号 + fn 花括号归属 + risky_fns 排序 + S16 reach
//! - 输出键序严格按 Python dict 字面量序（json::Value::Obj 保序）
//!
//! 已知偏离（语料与真实仓库不触发，见 S84 对照实验）：
//! - 旧 Python 的 _mask_rust 遇 `r#` 后非引号（Rust 原始标识符，如 r#type）会
//!   死循环——Rust 侧修复为 i += 1 继续扫描（仓库内无此形态，oracle 不受影响）
//! - f-string 字面量段不是独立 Constant 节点（pyast 只保留内插区域）→
//!   secret_literal 对 f-string 文本段不触发
//! - 恶意/畸形转义（b"\u…"、截断 \x）的 SyntaxError msg 与 CPython 的
//!   带位置前缀版本不同；超大整型/浮点 dump 用原文回退
//! - dump_expr 未覆盖的节点型（comprehension/DictComp 等）回退为类型名——
//!   规则只在 func 为 shell 属性链时才 dump，真实代码链上是 Name/Attribute/Subscript

use crate::json::Value;
use crate::pyast::{parse_module, Ctx, CVal, PyNode};
use crate::scan::read_text;
use std::collections::VecDeque;
use std::path::Path;

pub const AST_MAX_FILES: usize = 200;
const EXT_SET: [&str; 5] = [".py", ".js", ".mjs", ".cjs", ".rs"];
const SKIP_DIRS: [&str; 5] = ["node_modules", ".git", "__pycache__", ".venv", "target"];
const PY_SINKS_NAME: [&str; 3] = ["eval", "exec", "compile"];
const PY_ATTR_SHELL: [&str; 4] = ["system", "popen", "Popen", "spawnSync"];
const JS_SINKS_BARE: [&str; 3] = ["eval", "exec", "execSync"];

fn s(v: &str) -> Value {
    Value::Str(v.to_string())
}
fn i(v: usize) -> Value {
    Value::Int(v as i128)
}
fn o(kvs: Vec<(&str, Value)>) -> Value {
    Value::Obj(kvs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}
fn err_obj(msg: &str) -> Value {
    o(vec![("error", s(msg))])
}

// ---------- 遍历（os.walk 等价：额度在 yield 目录元组时检查） ----------

/// os.path.splitext 等价（扩展名小写；点文件无扩展名）。
fn splitext_ext(name: &str) -> String {
    match name.rfind('.') {
        None | Some(0) => String::new(),
        Some(k) => name[k..].to_ascii_lowercase(),
    }
}

fn join_name(dir: &Path, name: &str) -> String {
    let mut s = dir.to_string_lossy().into_owned();
    if !s.ends_with('\\') && !s.ends_with('/') {
        s.push('\\');
    }
    s.push_str(name);
    s
}

fn walk_dir(dir: &Path, out: &mut Vec<String>, max: usize) {
    if out.len() >= max {
        return; // Python: for dp,... in os.walk: if len(targets) >= max: break
    }
    let rd = match std::fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => return,
    };
    let mut files: Vec<String> = Vec::new();
    let mut dirs: Vec<String> = Vec::new();
    for e in rd.filter_map(|e| e.ok()) {
        let name = e.file_name().to_string_lossy().into_owned();
        let Ok(ft) = e.file_type() else { continue };
        // os.walk 语义：符号链接解引用定类（断链跳过）
        let is_dir = if ft.is_symlink() {
            match std::fs::metadata(e.path()) {
                Ok(m) => m.is_dir(),
                Err(_) => continue,
            }
        } else {
            ft.is_dir()
        };
        if is_dir {
            dirs.push(name);
        } else {
            files.push(name);
        }
    }
    let by_upcase = |a: &String, b: &String| {
        a.to_uppercase().cmp(&b.to_uppercase()).then_with(|| a.cmp(b))
    };
    files.sort_by(by_upcase);
    dirs.sort_by(by_upcase);
    for name in files {
        if EXT_SET.contains(&splitext_ext(&name).as_str()) && out.len() < max {
            out.push(join_name(dir, &name));
        }
    }
    for name in dirs {
        if SKIP_DIRS.contains(&name.as_str()) {
            continue;
        }
        walk_dir(&dir.join(&name), out, max);
    }
}

// ---------- os.path 等价（relpath / dirname） ----------

fn abs_norm(p: &str) -> String {
    let mut s = p.replace('/', "\\");
    let rooted = (s.len() >= 2 && s.as_bytes()[1] == b':') || s.starts_with("\\\\");
    if !rooted {
        let cwd = std::env::current_dir()
            .map(|d| d.to_string_lossy().replace('/', "\\"))
            .unwrap_or_default();
        s = format!("{}\\{}", cwd.trim_end_matches('\\'), s);
    }
    let (prefix, rest): (String, Vec<String>) = if s.starts_with("\\\\") {
        let segs: Vec<&str> = s.split('\\').filter(|x| !x.is_empty()).collect();
        if segs.len() >= 2 {
            (
                format!("\\\\{}\\{}", segs[0], segs[1]),
                segs[2..].iter().map(|x| x.to_string()).collect(),
            )
        } else {
            (s.clone(), Vec::new())
        }
    } else if s.len() >= 2 && s.as_bytes()[1] == b':' {
        (
            s[..2].to_string(),
            s[2..].split('\\').filter(|x| !x.is_empty()).map(|x| x.to_string()).collect(),
        )
    } else {
        (String::new(), s.split('\\').filter(|x| !x.is_empty()).map(|x| x.to_string()).collect())
    };
    let mut parts: Vec<String> = Vec::new();
    for seg in rest {
        match seg.as_str() {
            "." => {}
            ".." => {
                parts.pop();
            }
            x => parts.push(x.to_string()),
        }
    }
    if prefix.is_empty() {
        parts.join("\\")
    } else {
        format!("{}\\{}", prefix, parts.join("\\"))
    }
}

/// os.path.relpath 等价（盘内后缀为主路径；跨树回退 .. 拼接）。
fn relpath(fp: &str, base: &str) -> String {
    let f = abs_norm(fp);
    let b = abs_norm(base);
    if f == b {
        return ".".into();
    }
    let bp = format!("{}\\", b);
    if f.starts_with(&bp) {
        return f[bp.len()..].to_string();
    }
    let fs: Vec<&str> = f.split('\\').collect();
    let bs: Vec<&str> = b.split('\\').collect();
    let mut k = 0;
    while k < fs.len() && k < bs.len() && fs[k].eq_ignore_ascii_case(&bs[k]) {
        k += 1;
    }
    let mut out: Vec<String> = vec!["..".into(); bs.len() - k];
    out.extend(fs[k..].iter().map(|x| x.to_string()));
    out.join("\\")
}

fn py_dirname(p: &str) -> String {
    match p.rfind(['\\', '/']) {
        None => String::new(),
        Some(k) => p[..k].to_string(),
    }
}

// ---------- dump_expr（ast.dump 等价，None/空列表字段省略） ----------

fn ctx_s(c: Ctx) -> &'static str {
    match c {
        Ctx::Load => "Load()",
        Ctx::Store => "Store()",
        Ctx::Del => "Del()",
    }
}

fn py_repr_str(sv: &str) -> String {
    let has_sq = sv.contains('\'');
    let has_dq = sv.contains('"');
    let q = if has_sq && !has_dq { '"' } else { '\'' };
    let mut out = String::new();
    out.push(q);
    for c in sv.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

fn py_repr_bytes(b: &[u8]) -> String {
    let mut out = String::from("b'");
    for &c in b {
        match c {
            b'\\' => out.push_str("\\\\"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            b'\t' => out.push_str("\\t"),
            b'\'' => out.push_str("\\'"),
            0x20..=0x7e => out.push(c as char),
            _ => out.push_str(&format!("\\x{:02x}", c)),
        }
    }
    out.push('\'');
    out
}

fn num_repr(t: &str) -> String {
    let cleaned = t.replace('_', "");
    let lower = cleaned.to_ascii_lowercase();
    let (radix, rest) = if let Some(r) = lower.strip_prefix("0x") {
        (16, r)
    } else if let Some(r) = lower.strip_prefix("0o") {
        (8, r)
    } else if let Some(r) = lower.strip_prefix("0b") {
        (2, r)
    } else {
        (0, "")
    };
    if radix != 0 {
        return match i128::from_str_radix(rest, radix) {
            Ok(v) => v.to_string(),
            Err(_) => cleaned, // 超出 i128：原文回退（CPython 会全位打印）
        };
    }
    if cleaned.contains(['.', 'e', 'E', 'j', 'J']) {
        if let Some(base) = cleaned.strip_suffix(['j', 'J']) {
            if let Ok(v) = base.parse::<f64>() {
                return format!("{v}j");
            }
        } else if let Ok(v) = cleaned.parse::<f64>() {
            let mut out = format!("{v}");
            // Rust Display 不带小数点时补 .0（1e3 → Python 1000.0）
            if !out.contains('.') && !out.contains('e') && !out.contains("inf") {
                out.push_str(".0");
            }
            return out;
        }
        return cleaned;
    }
    match cleaned.parse::<i128>() {
        Ok(v) => v.to_string(),
        Err(_) => cleaned,
    }
}

fn cval_repr(c: &CVal) -> String {
    match c {
        CVal::NoneC => "None".into(),
        CVal::Bool(true) => "True".into(),
        CVal::Bool(false) => "False".into(),
        CVal::EllipsisC => "Ellipsis".into(),
        CVal::Str(v) => py_repr_str(v),
        CVal::Bytes(b) => py_repr_bytes(b),
        CVal::Num(t) => num_repr(t),
    }
}

fn dump_expr(n: &PyNode) -> String {
    match n.kind {
        "Name" => format!("Name(id={}, ctx={})", py_repr_str(&n.name), ctx_s(n.ctx)),
        "Constant" => format!("Constant(value={})", cval_repr(&n.cval)),
        "Attribute" => format!(
            "Attribute(value={}, attr={}, ctx={})",
            dump_expr(&n.children[0]),
            py_repr_str(&n.name),
            ctx_s(n.ctx)
        ),
        "Subscript" => format!(
            "Subscript(value={}, slice={}, ctx={})",
            dump_expr(&n.children[0]),
            dump_expr(&n.children[1]),
            ctx_s(n.ctx)
        ),
        "Call" => {
            let func = &n.children[0];
            let args: Vec<&PyNode> =
                n.children[1..].iter().filter(|c| c.kind != "keyword").collect();
            let kws: Vec<&PyNode> =
                n.children[1..].iter().filter(|c| c.kind == "keyword").collect();
            let mut out = format!("Call(func={}", dump_expr(func));
            if !args.is_empty() {
                out.push_str(", args=[");
                out.push_str(
                    &args.iter().map(|a| dump_expr(a)).collect::<Vec<_>>().join(", "),
                );
                out.push(']');
            }
            if !kws.is_empty() {
                out.push_str(", keywords=[");
                out.push_str(
                    &kws.iter().map(|a| dump_expr(a)).collect::<Vec<_>>().join(", "),
                );
                out.push(']');
            }
            out.push(')');
            out
        }
        "keyword" => {
            if n.name.is_empty() {
                format!("keyword(value={})", dump_expr(&n.children[0]))
            } else {
                format!(
                    "keyword(arg={}, value={})",
                    py_repr_str(&n.name),
                    dump_expr(&n.children[0])
                )
            }
        }
        "BinOp" => format!(
            "BinOp(left={}, op={}(), right={})",
            dump_expr(&n.children[0]),
            n.name,
            dump_expr(&n.children[1])
        ),
        "UnaryOp" => format!(
            "UnaryOp(op={}(), operand={})",
            n.name,
            dump_expr(&n.children[0])
        ),
        "BoolOp" => format!(
            "BoolOp(op={}(), values=[{}])",
            n.name,
            n.children.iter().map(dump_expr).collect::<Vec<_>>().join(", ")
        ),
        "Compare" => format!(
            "Compare(left={}, ops=[{}], comparators=[{}])",
            dump_expr(&n.children[0]),
            n.names.iter().map(|x| format!("{x}()")).collect::<Vec<_>>().join(", "),
            n.children[1..]
                .iter()
                .map(dump_expr)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        "IfExp" => format!(
            "IfExp(body={}, test={}, orelse={})",
            dump_expr(&n.children[1]),
            dump_expr(&n.children[0]),
            dump_expr(&n.children[2])
        ),
        "Lambda" => format!(
            "Lambda(args={}, body={})",
            dump_expr(&n.children[0]),
            dump_expr(&n.children[1])
        ),
        // Lambda 链上不可能出现 arguments 之外的字段全量形态，够用即可
        "arguments" => {
            let args: Vec<String> = n
                .children
                .iter()
                .filter(|c| c.kind == "arg")
                .map(|a| format!("arg(arg={})", py_repr_str(&a.name)))
                .collect();
            format!("arguments(args=[{}])", args.join(", "))
        }
        "Starred" => format!(
            "Starred(value={}, ctx={})",
            dump_expr(&n.children[0]),
            ctx_s(n.ctx)
        ),
        "Tuple" | "List" => {
            let mut out = format!("{0}(", n.kind);
            if !n.children.is_empty() {
                out.push_str(&format!(
                    "elts=[{}], ",
                    n.children.iter().map(dump_expr).collect::<Vec<_>>().join(", ")
                ));
            }
            out.push_str(&format!("ctx={})", ctx_s(n.ctx)));
            out
        }
        "Set" => format!(
            "Set(elts=[{}])",
            n.children.iter().map(dump_expr).collect::<Vec<_>>().join(", ")
        ),
        "Dict" => {
            if n.children.is_empty() {
                return "Dict()".into();
            }
            let (keys, values): (Vec<String>, Vec<String>) = if n.aux > 0 {
                (
                    n.children[..n.aux].iter().map(dump_expr).collect(),
                    n.children[n.aux..].iter().map(dump_expr).collect(),
                )
            } else {
                // {**a, ...}：键位全 None（列表内 None 保留）
                (
                    vec!["None".to_string(); n.children.len()],
                    n.children.iter().map(dump_expr).collect(),
                )
            };
            format!("Dict(keys=[{}], values=[{}])", keys.join(", "), values.join(", "))
        }
        "Slice" => {
            let mut parts: Vec<String> = Vec::new();
            let mut it = n.children.iter();
            if n.aux & 1 != 0 {
                parts.push(format!("lower={}", dump_expr(it.next().unwrap())));
            }
            if n.aux & 2 != 0 {
                parts.push(format!("upper={}", dump_expr(it.next().unwrap())));
            }
            if n.aux & 4 != 0 {
                parts.push(format!("step={}", dump_expr(it.next().unwrap())));
            }
            format!("Slice({})", parts.join(", "))
        }
        "JoinedStr" => {
            if n.children.is_empty() {
                "JoinedStr()".into()
            } else {
                format!(
                    "JoinedStr(values=[{}])",
                    n.children.iter().map(dump_expr).collect::<Vec<_>>().join(", ")
                )
            }
        }
        "FormattedValue" => {
            format!("FormattedValue(value={}, conversion=-1)", dump_expr(&n.children[0]))
        }
        _ => n.kind.to_string(),
    }
}

// ---------- Python：真 AST 规则 ----------

fn scan_python(src: &str, fp: &str) -> Vec<Value> {
    let tree = match parse_module(src) {
        Ok(t) => t,
        Err(e) => {
            return vec![o(vec![
                ("file", s(fp)),
                ("line", i(e.line)),
                ("col", i(0)),
                ("rule", s("syntax_error")),
                ("detail", s(&format!("AST 解析失败: {}", e.msg))),
                ("unit", s("module")),
            ])];
        }
    };
    let mut issues = Vec::new();
    let mut q: VecDeque<&PyNode> = VecDeque::new();
    q.push_back(&tree);
    while let Some(n) = q.pop_front() {
        if n.kind == "Call" {
            let func = &n.children[0];
            if func.kind == "Name" && PY_SINKS_NAME.contains(&func.name.as_str()) {
                // S13 准确率分级：首个位置参数是常量=静态可判（info）；空参数 all() 恒真
                let args: Vec<&PyNode> =
                    n.children[1..].iter().filter(|c| c.kind != "keyword").collect();
                let kind = if args.is_empty() || args[0].kind == "Constant" {
                    "literal"
                } else {
                    "dynamic"
                };
                issues.push(o(vec![
                    ("file", s(fp)),
                    ("line", i(n.line)),
                    ("col", i(n.col)),
                    ("rule", s("py_dynamic_exec")),
                    ("callee", s(&func.name)),
                    ("arg_kind", s(kind)),
                    ("unit", s("call")),
                    ("severity", s(if kind == "literal" { "info" } else { "med" })),
                ]));
            } else if func.kind == "Attribute"
                && PY_ATTR_SHELL.contains(&func.name.as_str())
            {
                let callee: String = dump_expr(func).chars().take(60).collect();
                issues.push(o(vec![
                    ("file", s(fp)),
                    ("line", i(n.line)),
                    ("col", i(n.col)),
                    ("rule", s("shell_like_call")),
                    ("callee", s(&callee)),
                    ("unit", s("call")),
                ]));
            }
        } else if n.kind == "Constant" {
            if let CVal::Str(v) = &n.cval {
                if let Some((mtext, _)) = find_secret(v) {
                    let head: String = mtext.chars().take(6).collect();
                    issues.push(o(vec![
                        ("file", s(fp)),
                        ("line", i(n.line)),
                        ("col", i(n.col)),
                        ("rule", s("secret_literal")),
                        ("detail", s(&format!("{}***len={}", head, mtext.chars().count()))),
                        ("unit", s("const")),
                    ]));
                }
            }
        }
        for c in &n.children {
            q.push_back(c);
        }
    }
    issues
}

// ---------- secret 形状（\b(sk-…|gh…_|AKIA…)\b 的手写等价） ----------

fn is_py_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// 左most 匹配；组内按 alternation 序、{n,} 贪婪最长 + 回溯到边界成立处。
/// 返回（匹配文本, 起始字符位）。
fn find_secret(v: &str) -> Option<(String, usize)> {
    let cs: Vec<char> = v.chars().collect();
    let n = cs.len();
    for p in 0..n {
        // \b 在组首：前一字符必须非单词（组首字符恒为单词字符）
        if p > 0 && is_py_word(cs[p - 1]) {
            continue;
        }
        // sk-…：[A-Za-z0-9_-]{20,}
        if cs[p] == 's' && p + 3 <= n && cs[p + 1] == 'k' && cs[p + 2] == '-' {
            let in_class =
                |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '-';
            if let Some(m) = greedy_bounded(&cs, p + 3, 20, in_class) {
                return Some((cs[p..m].iter().collect(), p));
            }
        }
        // gh[pousr]_…：[A-Za-z0-9]{30,}
        if cs[p] == 'g'
            && p + 4 <= n
            && cs[p + 1] == 'h'
            && matches!(cs[p + 2], 'p' | 'o' | 'u' | 's' | 'r')
            && cs[p + 3] == '_'
        {
            if let Some(m) = greedy_bounded(&cs, p + 4, 30, |c| c.is_ascii_alphanumeric()) {
                return Some((cs[p..m].iter().collect(), p));
            }
        }
        // AKIA…：[0-9A-Z]{16}
        if cs[p] == 'A'
            && p + 4 <= n
            && cs[p + 1] == 'K'
            && cs[p + 2] == 'I'
            && cs[p + 3] == 'A'
        {
            if let Some(m) =
                greedy_bounded(&cs, p + 4, 16, |c| c.is_ascii_digit() || ('A'..='Z').contains(&c))
            {
                return Some((cs[p..m].iter().collect(), p));
            }
        }
    }
    None
}

/// 从 k 起取字符类最长游程（≥min），自最长向短回溯找 \b 成立的终点。
fn greedy_bounded(cs: &[char], k: usize, min: usize, in_class: impl Fn(char) -> bool) -> Option<usize> {
    let n = cs.len();
    let mut run = 0usize;
    while k + run < n && in_class(cs[k + run]) {
        run += 1;
    }
    if run < min {
        return None;
    }
    for len in (min..=run).rev() {
        let e = k + len;
        let before = is_py_word(cs[e - 1]);
        let after = if e < n { is_py_word(cs[e]) } else { false };
        if before != after {
            return Some(e);
        }
    }
    None
}

// ---------- JS：词法掩码（char 口径保长度） ----------

fn is_js_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}

fn mask_js(src: &str) -> (Vec<char>, usize, usize, usize) {
    let cs: Vec<char> = src.chars().collect();
    let n = cs.len();
    let mut out = cs.clone();
    let (mut strings, mut templates, mut comments) = (0usize, 0usize, 0usize);
    struct Tpl {
        brace: usize,
        in_code: bool,
    }
    let mut stack: Vec<Tpl> = Vec::new();
    let mut i = 0usize;
    while i < n {
        let c = cs[i];
        let nxt = if i + 1 < n { cs[i + 1] } else { '\0' };
        // 模板文本区
        if let Some(top) = stack.last() {
            if !top.in_code {
                if c == '\\' && i + 1 < n {
                    out[i] = ' ';
                    out[i + 1] = ' ';
                    i += 2;
                    continue;
                }
                if c == '$' && nxt == '{' {
                    let top = stack.last_mut().unwrap();
                    top.in_code = true;
                    top.brace = 0;
                    i += 2;
                    continue;
                }
                if c == '`' {
                    out[i] = ' ';
                    stack.pop();
                    i += 1;
                    continue;
                }
                out[i] = ' ';
                i += 1;
                continue;
            }
        }
        if c == '/' && nxt == '/' {
            let mut j = i;
            while j < n && cs[j] != '\n' {
                j += 1;
            }
            for k in out.iter_mut().take(j).skip(i) {
                *k = ' ';
            }
            comments += 1;
            i = j;
        } else if c == '/' && nxt == '*' {
            let mut j = i + 2;
            let mut e = n;
            while j + 1 < n {
                if cs[j] == '*' && cs[j + 1] == '/' {
                    e = j + 2;
                    break;
                }
                j += 1;
            }
            for k in out.iter_mut().take(e).skip(i) {
                *k = ' ';
            }
            comments += 1;
            i = e;
        } else if c == '"' || c == '\'' {
            let mut j = i + 1;
            while j < n {
                if cs[j] == '\\' {
                    j += 2;
                    continue;
                }
                if cs[j] == c || cs[j] == '\n' {
                    break;
                }
                j += 1;
            }
            let e = (j + 1).min(n);
            for k in out.iter_mut().take(e).skip(i) {
                *k = ' ';
            }
            strings += 1;
            i = e;
        } else if c == '`' {
            out[i] = ' ';
            stack.push(Tpl { brace: 0, in_code: false });
            templates += 1;
            i += 1;
        } else if let Some(top) = stack.last_mut() {
            // top.in_code：插值内是真代码
            if c == '{' {
                top.brace += 1;
            } else if c == '}' {
                if top.brace == 0 {
                    top.in_code = false; // '}' 本身保留：插值边界结构
                } else {
                    top.brace -= 1;
                }
            }
            i += 1;
        } else {
            i += 1;
        }
    }
    (out, strings, templates, comments)
}

// ---------- JS：调用面提取（_CALL_RE 手写等价） ----------

fn is_js_chain_start(c: char) -> bool {
    c.is_ascii_alphabetic() || c == '_' || c == '$'
}
fn is_js_chain_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_' || c == '$'
}

/// 尝试在 i 匹配 (new\s+)?链\s*\(；返回 (is_new, 链, 匹配结束位)。
/// 正则回溯语义：new 组先试、失败回退空组。
fn try_call_at(cs: &[char], i: usize) -> Option<(bool, String, usize)> {
    if let Some(r) = attempt_call(cs, i, true) {
        return Some(r);
    }
    attempt_call(cs, i, false)
}

fn attempt_call(cs: &[char], i: usize, allow_new: bool) -> Option<(bool, String, usize)> {
    let n = cs.len();
    let mut j = i;
    let mut is_new = false;
    if allow_new
        && j + 3 < n
        && cs[j] == 'n'
        && cs[j + 1] == 'e'
        && cs[j + 2] == 'w'
        && is_js_space(cs[j + 3])
    {
        is_new = true;
        j += 3;
        while j < n && is_js_space(cs[j]) {
            j += 1;
        }
    }
    let read_seg = |k: usize| -> Option<usize> {
        if k >= n || !is_js_chain_start(cs[k]) {
            return None;
        }
        let mut e = k + 1;
        while e < n && is_js_chain_char(cs[e]) {
            e += 1;
        }
        Some(e)
    };
    let end0 = read_seg(j)?;
    let mut chain: String = cs[j..end0].iter().collect();
    j = end0;
    while j < n && cs[j] == '.' {
        match read_seg(j + 1) {
            Some(e) => {
                chain.push('.');
                chain.push_str(&cs[j + 1..e].iter().collect::<String>());
                j = e;
            }
            None => break,
        }
    }
    while j < n && is_js_space(cs[j]) {
        j += 1;
    }
    if j < n && cs[j] == '(' {
        Some((is_new, chain, j + 1))
    } else {
        None
    }
}

fn scan_js_calls(masked: &[char], fp: &str) -> (Vec<Value>, usize) {
    let n = masked.len();
    let mut line_starts: Vec<usize> = vec![0];
    for (k, &c) in masked.iter().enumerate() {
        if c == '\n' {
            line_starts.push(k + 1);
        }
    }
    let pos_of = |off: usize| -> (usize, usize) {
        // bisect_right(line_starts, off) - 1
        let mut lo = 0usize;
        let mut hi = line_starts.len();
        while lo < hi {
            let mid = (lo + hi) / 2;
            if line_starts[mid] <= off {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        let li = lo - 1;
        (li + 1, off - line_starts[li])
    };
    let mut issues = Vec::new();
    let mut total = 0usize;
    let mut cur = 0usize;
    while cur < n {
        // (?<![.\w$])：前一字符不得是点/单词符/$
        if cur > 0 && (masked[cur - 1] == '.' || masked[cur - 1] == '$' || is_py_word(masked[cur - 1])) {
            cur += 1;
            continue;
        }
        if let Some((is_new, chain, end)) = try_call_at(masked, cur) {
            total += 1;
            // （旧实现的括号平衡游标是死代码——匹配结果只用于统计与分类，略去）
            if is_new && chain == "Function" {
                let (ln, co) = pos_of(cur);
                issues.push(o(vec![
                    ("file", s(fp)),
                    ("line", i(ln)),
                    ("col", i(co)),
                    ("rule", s("js_new_function")),
                    ("callee", s("new Function(...)")),
                    ("span_args", Value::Null),
                    ("unit", s("call")),
                ]));
            } else if !chain.contains('.')
                && JS_SINKS_BARE.contains(&chain.as_str())
            {
                let (ln, co) = pos_of(cur);
                issues.push(o(vec![
                    ("file", s(fp)),
                    ("line", i(ln)),
                    ("col", i(co)),
                    ("rule", s("js_dynamic_exec")),
                    ("callee", s(&chain)),
                    ("unit", s("call")),
                ]));
            }
            cur = end; // finditer 语义：从匹配末尾续扫（嵌套调用因此单独计）
        } else {
            cur += 1;
        }
    }
    (issues, total)
}

// ---------- Rust：词法掩码 + 结构化信号 ----------

fn mask_rust(src: &str) -> (Vec<char>, usize, usize) {
    let cs: Vec<char> = src.chars().collect();
    let n = cs.len();
    let mut out = cs.clone();
    let (mut strings, mut comments) = (0usize, 0usize);
    let mut i = 0usize;
    while i < n {
        let c = cs[i];
        let nxt = if i + 1 < n { cs[i + 1] } else { '\0' };
        if c == '/' && nxt == '/' {
            let mut j = i;
            while j < n && cs[j] != '\n' {
                j += 1;
            }
            for k in out.iter_mut().take(j).skip(i) {
                *k = ' ';
            }
            comments += 1;
            i = j;
        } else if c == '/' && nxt == '*' {
            let mut j = i + 2;
            let mut e = n;
            while j + 1 < n {
                if cs[j] == '*' && cs[j + 1] == '/' {
                    e = j + 2;
                    break;
                }
                j += 1;
            }
            for k in out.iter_mut().take(e).skip(i) {
                *k = ' ';
            }
            comments += 1;
            i = e;
        } else if c == 'r' && (nxt == '"' || nxt == '#') {
            // 原始字符串 r"..." / r#"..."#（简易：# 计数）。旧 Python 在 r# 后
            // 非引号（原始标识符 r#type）会死循环——这里 i += 1 继续扫（S84 修复）
            let mut j = i + 1;
            let mut hashes = 0usize;
            while j < n && cs[j] == '#' {
                hashes += 1;
                j += 1;
            }
            if j < n && cs[j] == '"' {
                j += 1;
                while j < n {
                    if cs[j] == '"' {
                        let k = j + 1;
                        if (0..hashes).all(|h| k + h < n && cs[k + h] == '#') {
                            break;
                        }
                    }
                    j += 1;
                }
                let e = (j + 1).min(n);
                for k in out.iter_mut().take(e).skip(i) {
                    *k = ' ';
                }
                strings += 1;
                i = e;
                continue;
            }
            i += 1;
        } else if c == '"' {
            let mut j = i + 1;
            while j < n {
                if cs[j] == '\\' {
                    j += 2;
                    continue;
                }
                if cs[j] == c || cs[j] == '\n' {
                    break;
                }
                j += 1;
            }
            let e = (j + 1).min(n);
            for k in out.iter_mut().take(e).skip(i) {
                *k = ' ';
            }
            strings += 1;
            i = e;
        } else if c == '\'' {
            // char 字面量（短闭合）——生命周期 'a 无紧跟闭合引号则放过
            let mut j = i + 1;
            let mut closed = false;
            while j < n && j < i + 5 {
                if cs[j] == '\'' {
                    closed = true;
                    break;
                }
                if cs[j] == '\\' {
                    j += 2;
                    continue;
                }
                j += 1;
            }
            if closed {
                let e = j + 1;
                for k in out.iter_mut().take(e).skip(i) {
                    *k = ' ';
                }
                strings += 1;
                i = e;
            } else {
                i += 1;
            }
        } else {
            i += 1;
        }
    }
    (out, strings, comments)
}

fn is_rust_word(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

const PANIC_NAMES: [&str; 6] =
    ["unwrap", "expect", "panic!", "unreachable!", "todo!", "unimplemented!"];

fn mod_test_attr_search(s: &str) -> bool {
    // #\[cfg\s*\(\s*test\s*\)\s*\]
    let cs: Vec<char> = s.chars().collect();
    let n = cs.len();
    let mut i = 0usize;
    while i + 5 <= n {
        if cs[i] == '#' && cs[i + 1] == '[' && cs[i + 2] == 'c' && cs[i + 3] == 'f'
            && cs[i + 4] == 'g'
        {
            let mut j = i + 5;
            while j < n && is_js_space(cs[j]) {
                j += 1;
            }
            if j < n && cs[j] == '(' {
                j += 1;
                while j < n && is_js_space(cs[j]) {
                    j += 1;
                }
                if cs[j..].starts_with(&['t', 'e', 's', 't']) {
                    j += 4;
                    while j < n && is_js_space(cs[j]) {
                        j += 1;
                    }
                    if j < n && cs[j] == ')' {
                        j += 1;
                        while j < n && is_js_space(cs[j]) {
                            j += 1;
                        }
                        if j < n && cs[j] == ']' {
                            return true;
                        }
                    }
                }
            }
        }
        i += 1;
    }
    false
}

/// \bfn\s+(ident) 的首个匹配（Python \s 口径）。
fn fn_re_search(ln: &str) -> Option<String> {
    let cs: Vec<char> = ln.chars().collect();
    let n = cs.len();
    let mut i = 0usize;
    while i + 2 <= n {
        if cs[i] == 'f' && cs[i + 1] == 'n' && (i == 0 || !is_py_word(cs[i - 1])) {
            let mut j = i + 2;
            if j >= n || !is_js_space(cs[j]) {
                i += 1;
                continue;
            }
            while j < n && is_js_space(cs[j]) {
                j += 1;
            }
            if j < n && (cs[j].is_ascii_alphabetic() || cs[j] == '_') {
                let mut e = j + 1;
                while e < n && is_rust_word(cs[e]) {
                    e += 1;
                }
                return Some(cs[j..e].iter().collect());
            }
        }
        i += 1;
    }
    None
}

/// \bunsafe\b 的全部命中数（\b 用 Python \w 口径）。
fn unsafe_count_line(ln: &str) -> usize {
    let cs: Vec<char> = ln.chars().collect();
    let n = cs.len();
    let mut cnt = 0usize;
    let mut i = 0usize;
    while i + 6 <= n {
        if cs[i] == 'u'
            && cs[i + 1] == 'n'
            && cs[i + 2] == 's'
            && cs[i + 3] == 'a'
            && cs[i + 4] == 'f'
            && cs[i + 5] == 'e'
            && (i == 0 || !is_py_word(cs[i - 1]))
            && (i + 6 >= n || !is_py_word(cs[i + 6]))
        {
            cnt += 1;
            i += 6;
        } else {
            i += 1;
        }
    }
    cnt
}

/// _PANIC_CALL_RE：\b(?:\.\s*)?(名字表)\s*[(!] —— 点形优先（greedy ?），
/// 名字表按 alternation 序回溯。返回 (名字, group0) 列表。
/// \b 的两种成立语境（实测 CPython 语义）：
/// - 名字形：位置 i 是名字首字符（ASCII 字母）且前一字符非词字符；
///   `x .unwrap()` 的 'u'（前是 '.'）也成立，group0 不含点
/// - 点形：位置 i 是 '.' 且前一字符恰是词字符（\b 在词↔点间成立），
///   group0 含点（`v.unwrap(` → ".unwrap("）；行首 '.' 不成立
fn panic_call_finditer(ln: &str) -> Vec<(String, String)> {
    let cs: Vec<char> = ln.chars().collect();
    let n = cs.len();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < n {
        let c = cs[i];
        let name_gate = c.is_ascii_alphabetic() && (i == 0 || !is_py_word(cs[i - 1]));
        let dot_gate = c == '.' && i > 0 && is_py_word(cs[i - 1]);
        if !name_gate && !dot_gate {
            i += 1;
            continue;
        }
        let j = if c == '.' {
            let mut jj = i + 1;
            while jj < n && is_js_space(cs[jj]) {
                jj += 1;
            }
            jj
        } else {
            i
        };
        let mut matched: Option<(usize, &'static str)> = None;
        for name in PANIC_NAMES {
            let nc: Vec<char> = name.chars().collect();
            if j + nc.len() <= n && cs[j..j + nc.len()] == nc[..] {
                let mut k = j + nc.len();
                while k < n && is_js_space(cs[k]) {
                    k += 1;
                }
                if k < n && (cs[k] == '(' || cs[k] == '!') {
                    matched = Some((k + 1, name));
                    break;
                }
            }
        }
        if let Some((end, name)) = matched {
            out.push((name.to_string(), cs[i..end].iter().collect::<String>()));
            i = end;
        } else {
            i += 1;
        }
    }
    out
}

struct RustMeta {
    fn_count: usize,
    unsafe_count: usize,
    risky: Vec<Value>,
    fns: Vec<String>,
}

fn scan_rust_struct(masked: &[char], fp: &str) -> (Vec<Value>, RustMeta) {
    let text: String = masked.iter().collect();
    let lines: Vec<&str> = text.split('\n').collect();
    let mut issues = Vec::new();
    let mut fn_names: Vec<String> = Vec::new();
    let mut unsafe_blocks: Vec<usize> = Vec::new();
    let mut in_test_mod = false;
    let mut test_mod_depth: i64 = -1;
    let mut brace_depth: i64 = 0;
    let mut fn_stack: Vec<(i64, String)> = Vec::new();
    let mut fn_risk: Vec<(String, i128, i128)> = Vec::new(); // (name, unwrap, unsafe)

    for (idx, ln) in lines.iter().enumerate() {
        let idx = idx + 1;
        let stripped = ln.trim();
        if !in_test_mod && mod_test_attr_search(stripped) {
            in_test_mod = true;
            test_mod_depth = brace_depth;
        } else if in_test_mod && stripped.starts_with('}') && brace_depth <= test_mod_depth {
            in_test_mod = false;
        }
        // 先压栈：本行命中才能归属到本 fn
        if ln.contains('{') {
            if let Some(name) = fn_re_search(ln) {
                fn_stack.push((brace_depth, name.clone()));
                fn_names.push(name);
            }
        }
        if !in_test_mod {
            let uc = unsafe_count_line(ln);
            for _ in 0..uc {
                unsafe_blocks.push(idx);
                let owner =
                    fn_stack.last().map(|f| f.1.clone()).unwrap_or_else(|| "<toplevel>".into());
                let e = match fn_risk.iter_mut().find(|f| f.0 == owner) {
                    Some(e) => e,
                    None => {
                        fn_risk.push((owner.clone(), 0, 0));
                        fn_risk.last_mut().unwrap()
                    }
                };
                e.2 += 1;
                issues.push(o(vec![
                    ("file", s(fp)),
                    ("line", i(idx)),
                    ("col", i(0)),
                    ("rule", s("rust_unsafe")),
                    ("detail", s("unsafe 块（设计信号，需人工评估不变量）")),
                    ("unit", s("call")),
                    ("fn", s(&owner)),
                ]));
            }
            for (name, g0) in panic_call_finditer(ln) {
                let rule = if matches!(
                    name.as_str(),
                    "panic!" | "unreachable!" | "todo!" | "unimplemented!"
                ) {
                    "rust_panic_macro"
                } else {
                    "rust_unwrap_expect"
                };
                let owner =
                    fn_stack.last().map(|f| f.1.clone()).unwrap_or_else(|| "<toplevel>".into());
                let e = match fn_risk.iter_mut().find(|f| f.0 == owner) {
                    Some(e) => e,
                    None => {
                        fn_risk.push((owner.clone(), 0, 0));
                        fn_risk.last_mut().unwrap()
                    }
                };
                if rule == "rust_unsafe" {
                    e.2 += 1;
                } else if rule == "rust_unwrap_expect" {
                    e.1 += 1;
                }
                let detail: String = g0.chars().take(40).collect();
                issues.push(o(vec![
                    ("file", s(fp)),
                    ("line", i(idx)),
                    ("col", i(0)),
                    ("rule", s(rule)),
                    ("detail", s(&detail)),
                    ("unit", s("call")),
                    ("fn", s(&owner)),
                ]));
            }
        }
        let new_depth =
            brace_depth + ln.matches('{').count() as i64 - ln.matches('}').count() as i64;
        while let Some(top) = fn_stack.last() {
            if top.0 >= new_depth {
                fn_stack.pop();
            } else {
                break;
            }
        }
        brace_depth = new_depth;
    }

    let mut risky: Vec<(String, i128, i128)> =
        fn_risk.iter().filter(|f| f.1 > 0 || f.2 > 0).cloned().collect();
    risky.sort_by_key(|f| -(f.1 * 2 + f.2 * 8));
    let risky: Vec<Value> = risky
        .into_iter()
        .take(12)
        .map(|f| o(vec![("fn", s(&f.0)), ("unwrap", Value::Int(f.1)), ("unsafe", Value::Int(f.2))]))
        .collect();
    (
        issues,
        RustMeta {
            fn_count: fn_names.len(),
            unsafe_count: unsafe_blocks.len(),
            risky,
            fns: fn_names.into_iter().take(40).collect(),
        },
    )
}

// ---------- S16：Rust 跨文件引用可达性 ----------

const RUST_KEYWORDS: [&str; 41] = [
    "fn", "let", "if", "else", "match", "return", "mod", "pub", "use", "impl",
    "self", "Self", "struct", "enum", "trait", "for", "in", "while", "loop",
    "const", "static", "type", "where", "as", "mut", "ref", "move", "dyn",
    "unsafe", "crate", "super", "true", "false", "assert", "assert_eq",
    "unsafe_fn", "async", "await", "box", "extern", "macro_rules",
];

/// \b(ident)\b 的极大 ASCII 词游程；前后边界按 Python \w 口径（unicode 字母也算词）。
fn ident_finditer(ln: &str) -> Vec<(String, usize)> {
    let cs: Vec<char> = ln.chars().collect();
    let n = cs.len();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < n {
        if (cs[i].is_ascii_alphabetic() || cs[i] == '_') && (i == 0 || !is_py_word(cs[i - 1])) {
            let mut e = i + 1;
            while e < n && is_rust_word(cs[e]) {
                e += 1;
            }
            if e >= n || !is_py_word(cs[e]) {
                out.push((cs[i..e].iter().collect(), i));
            }
            i = e;
            continue;
        }
        i += 1;
    }
    out
}

/// 行内存在 \bfn\s+name\b（定义行判定第一半）。
fn line_has_fn_def(ln: &str, name: &str) -> bool {
    let cs: Vec<char> = ln.chars().collect();
    let nc: Vec<char> = name.chars().collect();
    let n = cs.len();
    let mut i = 0usize;
    while i + 2 <= n {
        if cs[i] == 'f' && cs[i + 1] == 'n' && (i == 0 || !is_py_word(cs[i - 1])) {
            let mut j = i + 2;
            if j >= n || !is_js_space(cs[j]) {
                i += 1;
                continue;
            }
            while j < n && is_js_space(cs[j]) {
                j += 1;
            }
            if j + nc.len() <= n && cs[j..j + nc.len()] == nc[..] {
                let e = j + nc.len();
                if e >= n || !is_py_word(cs[e]) {
                    return true;
                }
            }
        }
        i += 1;
    }
    false
}

struct RustDef {
    name: String,
    file: String,
    line: usize,
    test: bool,
}

fn rust_defs_and_refs(masked: &[char], fp: &str, is_test_file: bool) -> (Vec<RustDef>, Vec<(String, i128, i128)>) {
    let text: String = masked.iter().collect();
    let lines: Vec<&str> = text.split('\n').collect();
    let mut defs: Vec<RustDef> = Vec::new();
    let mut refs: Vec<(String, i128, i128)> = Vec::new(); // (name, prod, test)
    let mut in_test_mod = false;
    let mut test_mod_depth: i64 = -1;
    let mut brace_depth: i64 = 0;
    for (idx, ln) in lines.iter().enumerate() {
        let idx = idx + 1;
        let stripped = ln.trim();
        if !in_test_mod && mod_test_attr_search(stripped) {
            in_test_mod = true;
            test_mod_depth = brace_depth;
        } else if in_test_mod && stripped.starts_with('}') && brace_depth <= test_mod_depth {
            in_test_mod = false;
        }
        let ctx_test = is_test_file || in_test_mod;
        // 定义：finditer 全部命中
        {
            let cs: Vec<char> = ln.chars().collect();
            let n = cs.len();
            let mut i = 0usize;
            while i + 2 <= n {
                if cs[i] == 'f' && cs[i + 1] == 'n' && (i == 0 || !is_py_word(cs[i - 1])) {
                    let mut j = i + 2;
                    if j >= n || !is_js_space(cs[j]) {
                        i += 1;
                        continue;
                    }
                    while j < n && is_js_space(cs[j]) {
                        j += 1;
                    }
                    if j < n && (cs[j].is_ascii_alphabetic() || cs[j] == '_') {
                        let mut e = j + 1;
                        while e < n && is_rust_word(cs[e]) {
                            e += 1;
                        }
                        defs.push(RustDef {
                            name: cs[j..e].iter().collect(),
                            file: fp.to_string(),
                            line: idx,
                            test: ctx_test,
                        });
                        i = e;
                        continue;
                    }
                }
                i += 1;
            }
        }
        // 引用（排除定义处本身：行内存在 fn\s+name 且 ident 在最后那个 "fn " 之后）
        for (name, mstart) in ident_finditer(ln) {
            if RUST_KEYWORDS.contains(&name.as_str()) {
                continue;
            }
            let char_pos = ln.chars().take(mstart).count();
            let rfind_fn = {
                // rfind("fn ", 0, m.start())
                let head: String = ln.chars().take(char_pos).collect();
                head.rfind("fn ")
            };
            let is_def_here = line_has_fn_def(ln, &name)
                && matches!(rfind_fn, Some(p) if char_pos > p);
            if is_def_here {
                continue;
            }
            match refs.iter_mut().find(|r| r.0 == name) {
                Some(r) => {
                    if ctx_test {
                        r.2 += 1;
                    } else {
                        r.1 += 1;
                    }
                }
                None => refs.push((name, if ctx_test { 0 } else { 1 }, if ctx_test { 1 } else { 0 })),
            }
        }
        brace_depth += ln.matches('{').count() as i64 - ln.matches('}').count() as i64;
    }
    (defs, refs)
}

struct ReachResult {
    lmap: Vec<(String, Vec<(String, usize, &'static str)>)>, // fn -> [(file, line, reach)]
    helpers: Vec<Value>,
}

fn rust_reach(rs_sources: &[(String, String, bool)]) -> ReachResult {
    let mut all_defs: Vec<RustDef> = Vec::new();
    let mut merged: Vec<(String, i128, i128)> = Vec::new();
    for (fp, src, is_test_dir) in rs_sources {
        let (masked, _, _) = mask_rust(src);
        let (defs, refs) = rust_defs_and_refs(&masked, fp, *is_test_dir);
        all_defs.extend(defs);
        for (name, prod, test) in refs {
            match merged.iter_mut().find(|m| m.0 == name) {
                Some(m) => {
                    m.1 += prod;
                    m.2 += test;
                }
                None => merged.push((name, prod, test)),
            }
        }
    }
    let mut lmap: Vec<(String, Vec<(String, usize, &'static str)>)> = Vec::new();
    let mut helpers: Vec<Value> = Vec::new();
    for d in &all_defs {
        if d.test {
            continue;
        }
        let (prod, test) = match merged.iter().find(|m| m.0 == d.name) {
            Some(m) => (m.1, m.2),
            None => (0, 0),
        };
        let v: &'static str = if prod > 0 {
            "prod"
        } else if test > 0 {
            helpers.push(o(vec![
                ("fn", s(&d.name)),
                ("file", s(&d.file)),
                ("line", i(d.line)),
            ]));
            "test_only"
        } else {
            "unreferenced"
        };
        match lmap.iter_mut().find(|l| l.0 == d.name) {
            Some(l) => l.1.push((d.file.clone(), d.line, v)),
            None => lmap.push((d.name.clone(), vec![(d.file.clone(), d.line, v)])),
        }
    }
    ReachResult { lmap, helpers }
}

// ---------- 编排 ----------

pub fn ast_scan(path: &str, max_files: usize) -> Value {
    let p = Path::new(path);
    if !p.exists() {
        return err_obj(&format!("路径不存在: {path}"));
    }
    let mut targets: Vec<String> = Vec::new();
    if p.is_file() {
        targets.push(path.to_string());
    } else {
        walk_dir(p, &mut targets, max_files);
    }
    if targets.is_empty() {
        return err_obj("无可扫目标（仅支持 .py/.js/.mjs/.cjs/.rs）");
    }
    let base = if p.is_dir() { path.to_string() } else { py_dirname(path) };

    let mut all_issues: Vec<Value> = Vec::new();
    let mut per_unit: Vec<Value> = Vec::new();
    let mut rs_sources: Vec<(String, String, bool)> = Vec::new();
    for fp in &targets {
        let Some(raw) = read_text(Path::new(fp)) else {
            continue; // OSError → 静默跳过（与旧实现一致）
        };
        // Python open(..., "r") 的 universal newlines 契约：\r\n → \n、孤立 \r → \n。
        // CRLF 文件否则在字符串掩码里 \ 先吞 \r、真 \n 反而截断字符串，行号全盘漂移。
        let src = raw.replace("\r\n", "\n").replace('\r', "\n");
        let fp_rel = relpath(fp, &base);
        let lines = src.matches('\n').count() + 1;
        if fp.ends_with(".py") {
            let issues = scan_python(&src, &fp_rel);
            per_unit.push(o(vec![
                ("file", s(&fp_rel)),
                ("lang", s("python")),
                ("lines", i(lines)),
            ]));
            all_issues.extend(issues);
        } else if fp.ends_with(".rs") {
            let (masked, mstrings, mcomments) = mask_rust(&src);
            let (issues, meta) = scan_rust_struct(&masked, &fp_rel);
            // is_test_dir：fp_rel 路径组件（除文件名）含 tests/tests.*，或全路径含 \tests\
            let is_test_dir = {
                let fp_norm = fp_rel.replace('\\', "/");
                let comps: Vec<&str> = fp_norm.split('/').collect();
                comps[..comps.len().saturating_sub(1)]
                    .iter()
                    .any(|pp| *pp == "tests" || pp.starts_with("tests."))
                    || fp.contains("\\tests\\")
                    || fp.contains("/tests/")
            };
            rs_sources.push((fp_rel.clone(), src.clone(), is_test_dir));
            per_unit.push(o(vec![
                ("file", s(&fp_rel)),
                ("lang", s("rust")),
                ("lines", i(lines)),
                ("strings_masked", i(mstrings)),
                ("comments_masked", i(mcomments)),
                ("fn_count", i(meta.fn_count)),
                ("unsafe_count", i(meta.unsafe_count)),
                ("risky_fns", Value::Arr(meta.risky)),
                ("fns", Value::Arr(meta.fns.iter().map(|f| s(f)).collect())),
            ]));
            all_issues.extend(issues);
        } else {
            // 注意与旧实现一致的怪癖：单文件直扫不经扩展名过滤，.txt 也走 JS 管线；
            // 目录模式下 ".PY"（大写）能进 targets 但 endswith(".py") 不成立 → JS 管线
            let (masked, st_strings, st_templates, st_comments) = mask_js(&src);
            let (issues, calls_total) = scan_js_calls(&masked, &fp_rel);
            per_unit.push(o(vec![
                ("file", s(&fp_rel)),
                ("lang", s("javascript")),
                ("lines", i(lines)),
                ("strings_masked", i(st_strings)),
                ("templates_masked", i(st_templates)),
                ("comments_masked", i(st_comments)),
                ("calls_total", i(calls_total)),
            ]));
            all_issues.extend(issues);
        }
    }

    let mut reach_summary: Value = Value::Null;
    if !rs_sources.is_empty() {
        let ReachResult { lmap, helpers } = rust_reach(&rs_sources);
        // (file, fn) → reach 标注（仅三条 rust 规则；键序追加在 fn 之后）
        for it in all_issues.iter_mut() {
            let Value::Obj(kv) = it else { continue };
            let file = kv.iter().find(|(k, _)| k == "file").map(|(_, v)| match v {
                Value::Str(x) => x.clone(),
                _ => String::new(),
            });
            let fnv = kv.iter().find(|(k, _)| k == "fn").map(|(_, v)| match v {
                Value::Str(x) => x.clone(),
                _ => String::new(),
            });
            let rule = kv.iter().find(|(k, _)| k == "rule").map(|(_, v)| match v {
                Value::Str(x) => x.clone(),
                _ => String::new(),
            });
            if let (Some(file), Some(fnv), Some(rule)) = (file, fnv, rule) {
                if matches!(
                    rule.as_str(),
                    "rust_unwrap_expect" | "rust_panic_macro" | "rust_unsafe"
                ) {
                    if let Some((_, lst)) = lmap.iter().find(|(k, _)| *k == fnv) {
                        if let Some((_, _, v)) =
                            lst.iter().find(|(f, _, _)| *f == file)
                        {
                            kv.push(("reach".into(), s(v)));
                        }
                    }
                }
            }
        }
        let mut c_prod = 0i128;
        let mut c_unref = 0i128;
        for (_, lst) in &lmap {
            for (_, _, v) in lst {
                match *v {
                    "prod" => c_prod += 1,
                    "unreferenced" => c_unref += 1,
                    _ => {}
                }
            }
        }
        let mut entries: Vec<Value> = Vec::new();
        for (k, lst) in &lmap {
            for (f, l, v) in lst {
                entries.push(o(vec![
                    ("fn", s(k)),
                    ("file", s(f)),
                    ("line", i(*l)),
                    ("reach", s(v)),
                ]));
            }
        }
        entries.sort_by_key(|e| {
            let Value::Obj(kv) = e else { return (0u8, 0u8, String::new()) };
            let get = |key: &str| -> String {
                kv.iter()
                    .find(|(k, _)| k == key)
                    .map(|(_, v)| match v {
                        Value::Str(x) => x.clone(),
                        _ => String::new(),
                    })
                    .unwrap_or_default()
            };
            let reach = get("reach");
            let file = get("file");
            (
                (reach != "test_only") as u8,
                (reach != "unreferenced") as u8,
                file,
            )
        });
        entries.truncate(60);
        let defs_evaluated: usize = lmap.iter().map(|(_, lst)| lst.len()).sum();
        reach_summary = o(vec![
            ("defs_evaluated", i(defs_evaluated)),
            (
                "by_reach",
                o(vec![
                    ("prod", Value::Int(c_prod)),
                    ("test_only", Value::Int(helpers.len() as i128)),
                    ("unreferenced", Value::Int(c_unref)),
                ]),
            ),
            (
                "test_only_helpers",
                Value::Arr(helpers.into_iter().take(30).collect()),
            ),
            ("entries", Value::Arr(entries)),
        ]);
    }

    let mut by_rule: Vec<(String, i128)> = Vec::new();
    for it in &all_issues {
        let Value::Obj(kv) = it else { continue };
        let Some((_, Value::Str(rule))) = kv.iter().find(|(k, _)| k == "rule") else {
            continue;
        };
        match by_rule.iter_mut().find(|(k, _)| k == rule) {
            Some(e) => e.1 += 1,
            None => by_rule.push((rule.clone(), 1)),
        }
    }
    let total = all_issues.len();
    // files = 截断前的 units 数（Python: len(per_unit)，units 只是 [:200] 视图）
    let files_count = per_unit.len();
    let mut units = per_unit;
    units.truncate(200);
    o(vec![
        ("files", i(files_count)),
        ("total", i(total)),
        ("by_rule", Value::Obj(by_rule.into_iter().map(|(k, v)| (k, Value::Int(v))).collect())),
        ("issues", Value::Arr(all_issues)),
        ("units", Value::Arr(units)),
        (
            "layer_note",
            s("layer=structural（token/call 级）；上层聚合请基于 issues 自行收敛"),
        ),
        ("rust_reach", reach_summary),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relpath_suffix_and_dot() {
        assert_eq!(relpath("D:\\x\\a\\b.py", "D:\\x"), "a\\b.py");
        assert_eq!(relpath("D:\\x\\b.py", "D:\\x\\b.py"), ".");
    }

    #[test]
    fn secret_shape_matches_and_masks_detail() {
        let key = format!("sk-{}", "a".repeat(30));
        let (m, _) = find_secret(&key).unwrap();
        assert_eq!(m, key);
        // 词边界：前缀粘连不算
        assert!(find_secret(&format!("x{key}")).is_none());
        // gh / AKIA 形
        assert!(find_secret(&format!("ghp_{}", "a".repeat(30))).is_some());
        assert!(find_secret(&format!("AKIA{}", "A".repeat(16))).is_some());
        assert!(find_secret("AKIA123").is_none());
    }

    #[test]
    fn python_rules_cols_and_kinds() {
        let issues = scan_python("eval(\"1+1\")\nexec(user)\n", "t.py");
        let kinds: Vec<&str> = issues
            .iter()
            .map(|v| match v {
                Value::Obj(kv) => kv
                    .iter()
                    .find(|(k, _)| k == "arg_kind")
                    .map(|(_, v)| match v {
                        Value::Str(x) => x.as_str(),
                        _ => "",
                    })
                    .unwrap_or(""),
                _ => "",
            })
            .collect();
        assert_eq!(kinds, vec!["literal", "dynamic"]);
        // col = 链首：(eval)(x) 的 Call col 指到 '('（col=4），os.system 在 col=0
        let issues2 = scan_python("y = (eval)(x)\nos.system(cmd)\n", "t.py");
        let cols: Vec<i128> = issues2
            .iter()
            .map(|v| match v {
                Value::Obj(kv) => kv
                    .iter()
                    .find(|(k, _)| k == "col")
                    .map(|(_, v)| match v {
                        Value::Int(x) => *x,
                        _ => -1,
                    })
                    .unwrap_or(-1),
                _ => -1,
            })
            .collect();
        // shell_like 的 callee 是 ast.dump 形态
        let callee = match &issues2[1] {
            Value::Obj(kv) => match &kv.iter().find(|(k, _)| k == "callee").unwrap().1 {
                Value::Str(x) => x.clone(),
                _ => String::new(),
            },
            _ => String::new(),
        };
        assert_eq!(
            callee,
            // ast.dump(fn)[:60]：60 字符截断（Python 侧口径）
            "Attribute(value=Name(id='os', ctx=Load()), attr='system', ct"
        );
        assert_eq!(cols, vec![4, 0]); // Call col 以链首为准
    }

    #[test]
    fn js_pipeline_mask_and_calls() {
        let src = "const a = \"eval(x)\";\nexec(cmd);\nconst f = new Function(\"return 1\");\n";
        let (masked, st, tp, cm) = mask_js(src);
        assert_eq!(st, 2);
        assert_eq!(tp, 0);
        assert_eq!(cm, 0);
        assert!(masked.iter().collect::<String>().contains("exec(cmd)"));
        let (issues, total) = scan_js_calls(&masked, "t.js");
        // 掩码后字符串内容不计：exec(cmd) + new Function( 共 2 个调用面
        assert_eq!(total, 2);
        let rules: Vec<String> = issues
            .iter()
            .map(|v| match v {
                Value::Obj(kv) => kv
                    .iter()
                    .find(|(k, _)| k == "rule")
                    .map(|(_, v)| match v {
                        Value::Str(x) => x.clone(),
                        _ => String::new(),
                    })
                    .unwrap_or_default(),
                _ => String::new(),
            })
            .collect();
        assert_eq!(rules, vec!["js_dynamic_exec", "js_new_function"]);
    }

    #[test]
    fn rust_struct_and_reach_smoke() {
        let src = "fn dirty() {\n    x.unwrap();\n    unsafe { z() }\n}\n";
        let (masked, _, _) = mask_rust(src);
        let (issues, meta) = scan_rust_struct(&masked, "t.rs");
        assert_eq!(meta.fn_count, 1);
        assert_eq!(meta.unsafe_count, 1);
        assert_eq!(issues.len(), 2);
        let fns: Vec<String> = issues
            .iter()
            .map(|v| match v {
                Value::Obj(kv) => kv
                    .iter()
                    .find(|(k, _)| k == "fn")
                    .map(|(_, v)| match v {
                        Value::Str(x) => x.clone(),
                        _ => String::new(),
                    })
                    .unwrap_or_default(),
                _ => String::new(),
            })
            .collect();
        assert!(fns.iter().all(|f| f == "dirty"));
    }
}
