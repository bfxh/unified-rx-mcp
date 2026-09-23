//! astscan 子模块（S168 从 astscan.rs 拆出；纯搬移，未改语义）。
use super::*;

pub(crate) fn ctx_s(c: Ctx) -> &'static str {
    match c {
        Ctx::Load => "Load()",
        Ctx::Store => "Store()",
        Ctx::Del => "Del()",
    }
}

pub(crate) fn py_repr_str(sv: &str) -> String {
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

pub(crate) fn py_repr_bytes(b: &[u8]) -> String {
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

pub(crate) fn num_repr(t: &str) -> String {
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

pub(crate) fn cval_repr(c: &CVal) -> String {
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

pub(crate) fn d_call(n: &PyNode) -> String {
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

pub(crate) fn d_keyword(n: &PyNode) -> String {
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

pub(crate) fn d_arguments(n: &PyNode) -> String {
    let args: Vec<String> = n
        .children
        .iter()
        .filter(|c| c.kind == "arg")
        .map(|a| format!("arg(arg={})", py_repr_str(&a.name)))
        .collect();
    format!("arguments(args=[{}])", args.join(", "))
}

pub(crate) fn d_dict(n: &PyNode) -> String {
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

pub(crate) fn d_slice(n: &PyNode) -> String {
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

pub(crate) fn dump_expr(n: &PyNode) -> String {
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
        "Call" => d_call(n),
        "keyword" => d_keyword(n),
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
        "arguments" => d_arguments(n),
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
        "Dict" => d_dict(n),
        "Slice" => d_slice(n),
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

pub(crate) fn scan_python(src: &str, fp: &str) -> Vec<Value> {
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
        } else if n.kind == "Constant"
            && let CVal::Str(v) = &n.cval
                && let Some((mtext, _)) = find_secret(v) {
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
        for c in &n.children {
            q.push_back(c);
        }
    }
    issues
}

// ---------- secret 形状（\b(sk-…|gh…_|AKIA…)\b 的手写等价） ----------

pub(crate) fn is_py_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// 左most 匹配；组内按 alternation 序、{n,} 贪婪最长 + 回溯到边界成立处。
/// 返回（匹配文本, 起始字符位）。
pub(crate) fn find_secret(v: &str) -> Option<(String, usize)> {
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
            && let Some(m) = greedy_bounded(&cs, p + 4, 30, |c| c.is_ascii_alphanumeric()) {
                return Some((cs[p..m].iter().collect(), p));
            }
        // AKIA…：[0-9A-Z]{16}
        if cs[p] == 'A'
            && p + 4 <= n
            && cs[p + 1] == 'K'
            && cs[p + 2] == 'I'
            && cs[p + 3] == 'A'
            && let Some(m) =
                greedy_bounded(&cs, p + 4, 16, |c: char| c.is_ascii_digit() || c.is_ascii_uppercase())
            {
                return Some((cs[p..m].iter().collect(), p));
            }
    }
    None
}

