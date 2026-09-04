//! taint —— Rust 污点引擎（S78，VULN-HUNTING P1-a）。
//!
//! 从"模式匹配"升级到"来源→汇点"浅数据流：Python 子集词法器（三引号/f-string/
//! 续行/缩进）+ 函数域污点传播 + 同文件一跳跨函数（实参→形参、污染返回值→调用点）。
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

// ---------------------------------------------------------------- 发现

#[derive(Clone, Debug)]
pub struct Finding {
    pub file: String,
    pub line: usize,
    pub sink: String,
    pub var: String,
    pub source_line: usize,
    pub source_kind: String,
    pub flow: String,     // direct / interproc
    pub severity: String, // high / med
    pub kind: String,     // definite / clue / naive
}

#[derive(Default)]
pub struct ScanResult {
    pub files_scanned: usize,
    pub findings: Vec<Finding>,
    pub errors: Vec<String>,
}

impl Finding {
    fn to_value(&self) -> json::Value {
        obj([
            ("file", json::Value::Str(self.file.clone())),
            ("line", json::Value::Int(self.line as i128)),
            ("sink", json::Value::Str(self.sink.clone())),
            ("var", json::Value::Str(self.var.clone())),
            ("source_line", json::Value::Int(self.source_line as i128)),
            ("source_kind", json::Value::Str(self.source_kind.clone())),
            ("flow", json::Value::Str(self.flow.clone())),
            ("severity", json::Value::Str(self.severity.clone())),
            ("kind", json::Value::Str(self.kind.clone())),
        ])
    }
}

/// 小工具：从 (名, 值) 数组造保序对象。
fn obj<const N: usize>(pairs: [(&str, json::Value); N]) -> json::Value {
    json::Value::Obj(pairs.iter().map(|(k, v)| (k.to_string(), v.clone())).collect())
}

// ---------------------------------------------------------------- 词法器

#[derive(Clone, Debug, PartialEq)]
enum Tk {
    Id(String),
    Num,
    Str,
    Op(String),
    Newline,
    Indent,
    Dedent,
}

#[derive(Clone, Debug)]
struct Tok {
    tk: Tk,
    line: usize,
}

fn is_opener(s: &str) -> bool {
    s == "(" || s == "[" || s == "{"
}

fn is_closer(s: &str) -> bool {
    s == ")" || s == "]" || s == "}"
}

const OP3: [&str; 5] = ["**=", "//=", "...", ">>=", "<<="];
const OP2: [&str; 19] = ["**", "//", "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=",
    "%=", "&=", "|=", "^=", "->", "<<", ">>", ":=", "@="];

/// 词法器：字符流 → 令牌流。缩进只在括号深度 0 时生效；反斜杠续行跳过；
/// f-string 的 {expr} 内层发普通令牌（污点可见），:spec / !conv 跳过。
fn lex(src: &str) -> Vec<Tok> {
    let cs: Vec<char> = src.chars().collect();
    let mut out: Vec<Tok> = Vec::new();
    let mut i = 0usize;
    let mut line = 1usize;
    let mut depth = 0usize;
    let mut indents: Vec<usize> = vec![0];
    let mut at_bol = true;

    while i < cs.len() {
        if at_bol && depth == 0 {
            // 行首：量缩进；空行/纯注释行不参与缩进逻辑
            let mut j = i;
            let mut col = 0usize;
            while j < cs.len() && (cs[j] == ' ' || cs[j] == '\t') {
                col += if cs[j] == '\t' { 8 - col % 8 } else { 1 };
                j += 1;
            }
            if j >= cs.len() {
                break;
            }
            if cs[j] == '\n' || cs[j] == '\r' || cs[j] == '#' {
                i = j;
                at_bol = false;
                continue; // 空行/注释行：主分发处理
            }
            let top = *indents.last().unwrap();
            if col > top {
                indents.push(col);
                out.push(Tok { tk: Tk::Indent, line });
            } else {
                while *indents.last().unwrap() > col {
                    indents.pop();
                    out.push(Tok { tk: Tk::Dedent, line });
                }
            }
            i = j;
            at_bol = false;
            continue;
        }

        let c = cs[i];
        match c {
            '\r' => i += 1,
            '\n' => {
                line += 1;
                i += 1;
                if depth == 0 {
                    out.push(Tok { tk: Tk::Newline, line });
                    at_bol = true;
                }
            }
            '\\' if i + 1 < cs.len() && cs[i + 1] == '\n' => {
                i += 2;
                line += 1;
            }
            ' ' | '\t' => i += 1,
            '#' => {
                while i < cs.len() && cs[i] != '\n' {
                    i += 1;
                }
            }
            '\'' | '"' => {
                let (ni, nl) = lex_string(&cs, i, &mut out, line, false, false);
                i = ni;
                line = nl;
            }
            c if c.is_ascii_alphabetic() || c == '_' => {
                let start = i;
                while i < cs.len() && (cs[i].is_ascii_alphanumeric() || cs[i] == '_') {
                    i += 1;
                }
                let word: String = cs[start..i].iter().collect();
                // 字符串前缀：r/b/u/f 的非空组合且紧跟引号
                if i < cs.len()
                    && (cs[i] == '\'' || cs[i] == '"')
                    && !word.is_empty()
                    && word.chars().all(|ch| matches!(ch, 'r'|'R'|'b'|'B'|'u'|'U'|'f'|'F'))
                {
                    let is_raw = word.contains('r') || word.contains('R');
                    let is_f = word.contains('f') || word.contains('F');
                    let (ni, nl) = lex_string(&cs, i, &mut out, line, is_raw, is_f);
                    i = ni;
                    line = nl;
                } else {
                    out.push(Tok { tk: Tk::Id(word), line });
                }
            }
            c if c.is_ascii_digit() => {
                while i < cs.len()
                    && (cs[i].is_ascii_alphanumeric() || cs[i] == '.' || cs[i] == '_')
                {
                    i += 1;
                }
                out.push(Tok { tk: Tk::Num, line });
            }
            _ => {
                let three: String = cs[i..(i + 3).min(cs.len())].iter().collect();
                let two: String = cs[i..(i + 2).min(cs.len())].iter().collect();
                if cs.len() - i >= 3 && OP3.contains(&three.as_str()) {
                    out.push(Tok { tk: Tk::Op(three), line });
                    i += 3;
                } else if cs.len() - i >= 2 && OP2.contains(&two.as_str()) {
                    out.push(Tok { tk: Tk::Op(two), line });
                    i += 2;
                } else {
                    if is_opener(&c.to_string()) {
                        depth += 1;
                    } else if is_closer(&c.to_string()) && depth > 0 {
                        depth -= 1;
                    }
                    out.push(Tok { tk: Tk::Op(c.to_string()), line });
                    i += 1;
                }
            }
        }
    }
    while indents.len() > 1 {
        indents.pop();
        out.push(Tok { tk: Tk::Dedent, line });
    }
    out
}

/// 字符串字面量消费（含三引号/raw/f-string 插值）。返回新索引。
/// f-string：{expr} 内层发 Id/Num/Op 令牌（行号取当前行）；:spec 进入后跳过到配对 }。
/// 返回 (消耗后的索引, 结束行号)。行号必须回传主循环——多行字符串（模块
/// docstring 等）内部换行不计入会让后续所有行号漂移（S73 重放首测实锤）。
fn lex_string(cs: &[char], mut i: usize, out: &mut Vec<Tok>, line: usize,
              is_raw: bool, is_f: bool) -> (usize, usize) {
    let quote = cs[i];
    let triple = i + 2 < cs.len() && cs[i + 1] == quote && cs[i + 2] == quote;
    i += if triple { 3 } else { 1 };
    let mut cur = line;
    let mut brace = 0usize; // f-string 插值深度
    let mut spec = false;   // 进了格式说明符
    loop {
        if i >= cs.len() {
            break;
        }
        let c = cs[i];
        if c == '\n' {
            cur += 1;
            i += 1;
            if !triple {
                break; // 单引号串未闭合到行尾：容忍
            }
            continue;
        }
        if c == quote {
            if triple {
                if i + 2 < cs.len() && cs[i + 1] == quote && cs[i + 2] == quote {
                    i += 3;
                    break;
                }
                i += 1;
                continue;
            }
            i += 1;
            break;
        }
        if c == '\\' && i + 1 < cs.len() {
            if cs[i + 1] == '\n' {
                cur += 1;
                i += 2;
                continue;
            }
            if !is_raw {
                i += 2;
                continue;
            }
            // raw 串：反斜杠保留在值里，但仍阻止紧随的引号终止字符串
            // （Python 语义 r"...\"..." 不在此收口；漏掉会把正则里的 \" 当
            // 终止符，整文件词法失配——S73 重放 appaudit.py 零命中实锤）
            if cs[i + 1] == quote {
                i += 2;
                continue;
            }
            i += 1;
            continue;
        }
        if is_f {
            if c == '{' {
                brace += 1;
                spec = false;
                i += 1;
                continue;
            }
            if c == '}' {
                if brace > 0 {
                    brace -= 1;
                    if brace == 0 {
                        spec = false;
                    }
                }
                i += 1;
                continue;
            }
            if brace > 0 {
                if spec {
                    i += 1; // 格式说明符内容不参与污点
                    continue;
                }
                if c == ':' && brace == 1 {
                    spec = true;
                    i += 1;
                    continue;
                }
                if c == '!' && i + 1 < cs.len() && cs[i + 1].is_ascii_alphabetic() {
                    i += 2; // !r/!s/!a 转换
                    continue;
                }
                if c.is_ascii_alphabetic() || c == '_' {
                    let st = i;
                    while i < cs.len() && (cs[i].is_ascii_alphanumeric() || cs[i] == '_') {
                        i += 1;
                    }
                    out.push(Tok { tk: Tk::Id(cs[st..i].iter().collect()), line: cur });
                    continue;
                }
                if c.is_ascii_digit() {
                    out.push(Tok { tk: Tk::Num, line: cur });
                    i += 1;
                    continue;
                }
                if !c.is_whitespace() {
                    out.push(Tok { tk: Tk::Op(c.to_string()), line: cur });
                }
                i += 1;
                continue;
            }
        }
        i += 1;
    }
    out.push(Tok { tk: Tk::Str, line: cur });
    (i, cur)
}

// ---------------------------------------------------------------- 分析器

const SANITIZERS: [&str; 5] = ["basename", "secure_filename", "int", "float", "_fs_resolve"];
const SOURCE_DOTTED: [&str; 4] = ["sys.argv", "os.environ", "sys.stdin", "sys.stdin.buffer"];
const SOURCE_CALLS: [&str; 3] = ["input", "os.getenv", "raw_input"];
const EXEC_SINKS: [&str; 10] = [
    "eval", "exec", "compile", "os.system", "os.popen", "pickle.loads", "pickle.load",
    "yaml.load", "yaml.unsafe_load", "marshal.loads",
];
const PATH_SINKS: [&str; 26] = [
    "open", "io.open", "os.remove", "os.unlink", "os.rename", "os.renames", "os.makedirs",
    "os.mkdir", "os.rmdir", "os.truncate", "os.chmod", "os.chown", "os.stat", "os.lstat",
    "os.listdir", "os.scandir", "os.walk", "shutil.copy", "shutil.copy2", "shutil.copyfile",
    "shutil.copytree", "shutil.move", "shutil.rmtree", "shutil.make_archive",
    "tarfile.open", "zipfile.ZipFile",
];
const METHOD_SINKS: [&str; 14] = [
    "open", "read_text", "read_bytes", "write_text", "write_bytes", "unlink", "mkdir",
    "rmdir", "rename", "replace", "touch", "glob", "rglob", "iterdir",
];
const AUG_OPS: [&str; 12] = ["+=", "-=", "*=", "/=", "//=", "**=", "%=", "&=", "|=", "^=", ">>=", "<<="];
const KEYWORDS: [&str; 30] = [
    "def", "class", "return", "if", "else", "elif", "for", "while", "with", "as", "import",
    "from", "lambda", "try", "except", "finally", "raise", "in", "not", "and", "or", "is",
    "None", "True", "False", "pass", "break", "continue", "global", "yield",
];

#[derive(Clone, Debug)]
struct TSrc {
    line: usize,
    kind: String,
    interproc: bool,
    definite: bool, // 入口可达（@tool 入口形参 / 宿主数据源）= 实锤；内部形参流 = clue
}

#[derive(Clone, Debug)]
struct Hit {
    var: String,
    line: usize,
    kind: String,
    interproc: bool,
    definite: bool,
}

struct Scope {
    name: String,
    indent: usize,
    parent: Option<usize>,
    params: Vec<String>,
    taint: HashMap<String, TSrc>,
    rets: Vec<(usize, usize, usize)>, // (起, 止, 行)
    ret_tainted: bool,
    entry: bool, // @tool 装饰 = MCP 宿主可达入口
}

#[derive(Clone)]
struct ArgSlice {
    start: usize,
    end: usize,
    kw: Option<String>,
}

#[derive(Clone)]
struct CallRec {
    callee: String,       // 点路径如 os.remove；方法形式存属性名
    method: bool,         // true = .attr( 形式（有接收者）
    recv: (usize, usize), // 接收者 token 区间（method 时有效）
    args: Vec<ArgSlice>,
    line: usize,
    scope: usize,
    lhs: Vec<String>, // 语句级赋值目标（x = f(...) 记录 x）
}

struct Analyzer {
    toks: Vec<Tok>,
    scopes: Vec<Scope>,
    calls: Vec<CallRec>,
    file: String,
    naive: bool,
}

impl Analyzer {
    fn new(file: &str, src: &str, naive: bool) -> Self {
        Analyzer {
            toks: lex(src),
            scopes: vec![Scope {
                name: "<module>".into(),
                indent: 0,
                parent: None,
                params: Vec::new(),
                taint: HashMap::new(),
                rets: Vec::new(),
                ret_tainted: false,
                entry: false,
            }],
            calls: Vec::new(),
            file: file.to_string(),
            naive,
        }
    }

    fn lookup(&self, scope: usize, var: &str) -> Option<TSrc> {
        let mut s = Some(scope);
        while let Some(si) = s {
            let sc = &self.scopes[si];
            if let Some(t) = sc.taint.get(var) {
                return Some(t.clone());
            }
            s = sc.parent;
        }
        None
    }

    /// 一遍走查：作用域栈 + 语句扫描（赋值/for/with/def/调用记录）。
    fn pass1(&mut self) {
        let mut scope_stack: Vec<usize> = vec![0];
        let mut ind_level = 0usize;
        // (name, params, def 行缩进层级, def 行号, 入口)
        let mut pending_def: Option<(String, Vec<String>, usize, usize, bool)> = None;
        let mut i = 0usize;

        while i < self.toks.len() {
            // 先取判别（避免同时借 self.toks 与 &mut self）
            let disc = match &self.toks[i].tk {
                Tk::Indent => 0,
                Tk::Dedent => 1,
                Tk::Newline => 2,
                Tk::Id(w) if w == "def" => 3,
                _ => 4,
            };
            match disc {
                0 => {
                    // Indent：若有挂起的 def，则开新作用域
                    ind_level += 1;
                    if let Some((name, params, indent, dline, entry)) = pending_def.take() {
                        let parent = *scope_stack.last().unwrap();
                        let id = self.scopes.len();
                        let mut taint = HashMap::new();
                        for p in &params {
                            // 形参即来源（MCP 威胁模型）；入口函数的形参才是宿主可控实锤
                            taint.insert(p.clone(), TSrc {
                                line: dline, kind: "param".into(),
                                interproc: false, definite: entry,
                            });
                        }
                        self.scopes.push(Scope {
                            name, indent, parent: Some(parent), params,
                            taint, rets: Vec::new(), ret_tainted: false, entry,
                        });
                        scope_stack.push(id);
                    }
                    i += 1;
                }
                1 => {
                    ind_level = ind_level.saturating_sub(1);
                    // 缩进回到某作用域 def 行层级及以下 → 该作用域体结束
                    while scope_stack.len() > 1
                        && self.scopes[*scope_stack.last().unwrap()].indent >= ind_level
                    {
                        scope_stack.pop();
                    }
                    i += 1;
                }
                2 => {
                    i += 1;
                }
                3 => {
                    let line = self.toks[i].line;
                    let mut j = i + 1;
                    let mut name = String::new();
                    if let Some(Tk::Id(n)) = self.toks.get(j).map(|t| &t.tk) {
                        name = n.clone();
                        j += 1;
                    }
                    let mut params = Vec::new();
                    let mut one_liner = false;
                    if j < self.toks.len() && self.toks[j].tk == Tk::Op("(".into()) {
                        j += 1;
                        let mut expect = true;
                        while j < self.toks.len() && self.toks[j].tk != Tk::Op(")".into()) {
                            match &self.toks[j].tk {
                                Tk::Id(p) => {
                                    if expect {
                                        params.push(p.clone());
                                    }
                                    expect = false;
                                }
                                Tk::Op(o) if o == "," => expect = true,
                                _ => {}
                            }
                            j += 1;
                        }
                        j += 1; // ')'
                    }
                    // 冒号后到行尾：有实际令牌 = 单行函数体（不建作用域，容忍）
                    let mut saw_body = false;
                    while j < self.toks.len() && self.toks[j].tk != Tk::Newline {
                        if self.toks[j].tk != Tk::Op(":".into()) {
                            saw_body = true;
                        }
                        j += 1;
                    }
                    if saw_body {
                        one_liner = true;
                    }
                    // 入口识别：def 前的装饰器段（跨行 dict 也越过）里出现 tool(...
                    // 即 @tool 装饰 = MCP 宿主可达边界（S73 分诊"暴露面"的机器化）。
                    // 注意先跳过装饰器行与 def 之间的行分隔 Newline（depth 0），
                    // 否则第一步就停——entry 恒 false（S73 重放实测踩坑）
                    let mut entry = false;
                    {
                        let mut b = i;
                        let mut depth = 0usize;
                        let mut skipped_bol = false;
                        while b > 0 {
                            b -= 1;
                            match &self.toks[b].tk {
                                Tk::Newline if depth == 0 => {
                                    if skipped_bol {
                                        break;
                                    }
                                    skipped_bol = true;
                                }
                                Tk::Op(o) => {
                                    if is_closer(o) {
                                        depth += 1;
                                    } else if is_opener(o) {
                                        if depth == 0 {
                                            break;
                                        }
                                        depth -= 1;
                                    }
                                }
                                Tk::Id(w) if depth == 0 && w == "tool" => {
                                    entry = true;
                                    break;
                                }
                                _ => {}
                            }
                        }
                    }
                    if one_liner {
                        pending_def = None;
                    } else {
                        pending_def = Some((name, params, ind_level, line, entry));
                    }
                    i = j;
                }
                _ => {
                    // 语句窗口：到本行行尾 / 缩进边界
                    let start = i;
                    let mut j = i;
                    while j < self.toks.len()
                        && self.toks[j].tk != Tk::Newline
                        && self.toks[j].tk != Tk::Indent
                        && self.toks[j].tk != Tk::Dedent
                    {
                        j += 1;
                    }
                    let scope = *scope_stack.last().unwrap();
                    let stmt_line = self.toks[start].line;
                    self.scan_stmt(scope, start, j, stmt_line);
                    i = j.max(start + 1);
                }
            }
        }
    }

    /// 单语句：找赋值/for/with + 记录全部调用 + 传播污点。
    fn scan_stmt(&mut self, scope: usize, start: usize, end: usize, line: usize) {
        if start >= end {
            return;
        }
        // 语句内括号深度重算（词法器的深度是跨行全局的）
        let mut depth = 0usize;
        let mut eq_at: Option<usize> = None;
        let mut aug_at: Option<usize> = None;
        let mut for_at: Option<usize> = None;
        let mut in_at: Option<usize> = None;
        let mut with_as: Option<usize> = None;
        for k in start..end {
            match &self.toks[k].tk {
                Tk::Op(o) => {
                    if is_opener(o) {
                        depth += 1;
                    } else if is_closer(o) {
                        depth = depth.saturating_sub(1);
                    } else if depth == 0 {
                        if o == "=" && eq_at.is_none() {
                            eq_at = Some(k);
                        } else if AUG_OPS.contains(&o.as_str()) && aug_at.is_none() {
                            aug_at = Some(k);
                        }
                    }
                }
                Tk::Id(w) if depth == 0 => {
                    if w == "for" && for_at.is_none() {
                        for_at = Some(k);
                    } else if w == "in" && for_at.is_some() && in_at.is_none() {
                        in_at = Some(k);
                    } else if w == "as" && with_as.is_none() {
                        with_as = Some(k);
                    }
                }
                _ => {}
            }
        }
        // 1) 调用记录（先记，赋值目标随后补挂）
        let calls_before = self.calls.len();
        self.collect_calls(scope, start, end);
        // 2) 赋值传播
        if let Some(k) = eq_at.or(aug_at) {
            let targets = self.lhs_targets(start, k);
            // 右值以 .name / .stem 收尾 → 取出的就是净化后的值，整条右值不算污点
            // （Path(user).name、os.path.basename 链同理；只挡使用点挡不住赋值点）
            let rhs_ends_sanitized = end >= start + 2
                && matches!(&self.toks[end - 1].tk, Tk::Id(w) if w == "name" || w == "stem")
                && matches!(&self.toks[end - 2].tk, Tk::Op(o) if o == ".");
            if !targets.is_empty() && !rhs_ends_sanitized {
                if let Some(hit) = self.expr_taint(scope, k + 1, end) {
                    for t in &targets {
                        self.scopes[scope].taint.insert(t.clone(), TSrc {
                            line: hit.line, kind: hit.kind.clone(),
                            interproc: hit.interproc, definite: hit.definite,
                        });
                    }
                }
            }
            // x = f(...)：调用点挂上赋值目标（供污染返回值传播）
            for c in &mut self.calls[calls_before..] {
                if c.scope == scope {
                    c.lhs = targets.clone();
                }
            }
            return;
        }
        // 3) for x in expr:
        if let (Some(f), Some(n)) = (for_at, in_at) {
            let targets = self.lhs_targets(f + 1, n);
            if !targets.is_empty() {
                if let Some(hit) = self.expr_taint(scope, n + 1, end) {
                    for t in &targets {
                        self.scopes[scope].taint.insert(t.clone(), TSrc {
                            line: hit.line, kind: hit.kind.clone(),
                            interproc: hit.interproc, definite: hit.definite,
                        });
                    }
                }
            }
            return;
        }
        // 4) with expr as var:
        if let Some(a) = with_as {
            let targets = self.lhs_targets(a + 1, end);
            if !targets.is_empty() {
                if let Some(hit) = self.expr_taint(scope, start, a) {
                    for t in &targets {
                        self.scopes[scope].taint.insert(t.clone(), TSrc {
                            line: hit.line, kind: hit.kind.clone(),
                            interproc: hit.interproc, definite: hit.definite,
                        });
                    }
                }
            }
            return;
        }
        // 5) return expr（记录供跨函数返回传播）
        if let Tk::Id(w) = &self.toks[start].tk {
            if w == "return" && start + 1 < end {
                self.scopes[scope].rets.push((start + 1, end, line));
            }
        }
    }

    /// 赋值/for/with 目标：区间内的标识符（跳过属性位/关键字/括号内的 kwarg 名）。
    /// 注意 `x = ...` 的目标后跟 `=` 是赋值目标不是 kwarg——kwarg 名只在括号内出现，
    /// 深度必须 >0 才跳过（S78 首测实锤：无深度判断时所有赋值目标被吞，污点全断）。
    fn lhs_targets(&self, start: usize, end: usize) -> Vec<String> {
        let mut out = Vec::new();
        let mut depth = 0usize;
        for k in start..end.min(self.toks.len()) {
            if let Tk::Op(o) = &self.toks[k].tk {
                if is_opener(o) {
                    depth += 1;
                } else if is_closer(o) {
                    depth = depth.saturating_sub(1);
                }
                continue;
            }
            if let Tk::Id(w) = &self.toks[k].tk {
                if KEYWORDS.contains(&w.as_str()) {
                    continue;
                }
                let prev_attr = k > 0 && matches!(&self.toks[k - 1].tk, Tk::Op(o) if o == ".");
                let kw_name = depth > 0
                    && matches!(self.toks.get(k + 1).map(|t| &t.tk), Some(Tk::Op(o)) if o == "=");
                if !prev_attr && !kw_name {
                    out.push(w.clone());
                }
            }
        }
        out
    }

    /// 收集语句内全部调用（点路径 + 方法形式；深入嵌套实参）。
    fn collect_calls(&mut self, scope: usize, start: usize, end: usize) {
        let mut k = start;
        while k < end {
            // 方法形式：... . attr (
            if matches!(&self.toks[k].tk, Tk::Op(o) if o == ".") {
                let attr_is = matches!(self.toks.get(k + 1).map(|t| &t.tk), Some(Tk::Id(_)));
                let paren_is = matches!(self.toks.get(k + 2).map(|t| &t.tk), Some(Tk::Op(o)) if o == "(");
                if attr_is && paren_is {
                    let Tk::Id(attr) = &self.toks[k + 1].tk else { unreachable!() };
                    let attr = attr.clone();
                    let recv = self.recv_start(start, k);
                    let (args, _close) = self.parse_args(k + 2, end);
                    self.calls.push(CallRec {
                        callee: attr,
                        method: true,
                        recv: (recv, k),
                        args,
                        line: self.toks[k + 1].line,
                        scope,
                        lhs: Vec::new(),
                    });
                    k += 3; // 越过 . attr (，继续深入实参
                    continue;
                }
            }
            // 点路径调用：Id . Id ... （前一个令牌是 . 的交给方法分支）
            if let Tk::Id(_) = &self.toks[k].tk {
                let prev_dot = k > start && matches!(&self.toks[k - 1].tk, Tk::Op(o) if o == ".");
                if !prev_dot {
                    let mut parts: Vec<String> = Vec::new();
                    let mut j = k;
                    loop {
                        match self.toks.get(j).map(|t| &t.tk) {
                            Some(Tk::Id(w)) => {
                                parts.push(w.clone());
                                j += 1;
                                if matches!(self.toks.get(j).map(|t| &t.tk), Some(Tk::Op(o)) if o == ".")
                                    && matches!(self.toks.get(j + 1).map(|t| &t.tk), Some(Tk::Id(_)))
                                {
                                    j += 1; // 吃掉 '.'，下轮吃 Id
                                } else {
                                    break;
                                }
                            }
                            _ => break,
                        }
                    }
                    if j < end && matches!(self.toks.get(j).map(|t| &t.tk), Some(Tk::Op(o)) if o == "(") {
                        let (args, _close) = self.parse_args(j, end);
                        self.calls.push(CallRec {
                            callee: parts.join("."),
                            method: false,
                            recv: (0, 0),
                            args: args.clone(),
                            line: self.toks[k].line,
                            scope,
                            lhs: Vec::new(),
                        });
                        // 同时补记方法形式（callee=末段，接收者=前面的链）：
                        // p.write_text(x) / conn.execute(sql) 的汇点在末段，
                        // 只记全路径会整条漏掉（S78 首测实锤 .write_text 丢失）
                        if parts.len() >= 2 && j >= 2 {
                            self.calls.push(CallRec {
                                callee: parts.last().unwrap().clone(),
                                method: true,
                                recv: (k, j - 2),
                                args,
                                line: self.toks[k].line,
                                scope,
                                lhs: Vec::new(),
                            });
                        }
                        k = j + 1; // 越过 '('，继续深入实参
                        continue;
                    }
                }
            }
            k += 1;
        }
    }

    /// 从点号往回找接收者表达式起点（p.attr / a.b.attr / f(x).attr / a[0].attr）。
    fn recv_start(&self, stmt_start: usize, dot: usize) -> usize {
        let mut j = dot;
        while j > stmt_start {
            let prev = j - 1;
            match &self.toks[prev].tk {
                Tk::Id(_) | Tk::Num | Tk::Str => j = prev,
                Tk::Op(o) if o == "." => j = prev,
                Tk::Op(o) if is_closer(o) => {
                    let mut d = 0usize;
                    let mut m = prev;
                    loop {
                        match &self.toks[m].tk {
                            Tk::Op(o2) if is_closer(o2) => d += 1,
                            Tk::Op(o2) if is_opener(o2) => {
                                d -= 1;
                                if d == 0 {
                                    break;
                                }
                            }
                            _ => {}
                        }
                        if m == stmt_start {
                            break;
                        }
                        m -= 1;
                    }
                    j = m;
                }
                _ => break,
            }
        }
        j
    }

    /// 解析调用实参：顶层逗号分片 + 关键字名。返回 (分片, 右括号下标)。
    fn parse_args(&self, open: usize, end: usize) -> (Vec<ArgSlice>, usize) {
        let mut args: Vec<ArgSlice> = Vec::new();
        let mut depth = 0usize;
        let mut cur: Option<ArgSlice> = None;
        let mut k = open + 1;
        while k < end {
            match &self.toks[k].tk {
                Tk::Op(o) if is_opener(o) => {
                    depth += 1;
                    let c = cur.get_or_insert(ArgSlice { start: k, end: k, kw: None });
                    c.end = k + 1;
                }
                Tk::Op(o) if is_closer(o) => {
                    if depth == 0 {
                        if let Some(mut a) = cur.take() {
                            a.end = k;
                            self.arg_kw(&mut a);
                            if a.end > a.start {
                                args.push(a);
                            }
                        }
                        return (args, k);
                    }
                    depth -= 1;
                    let c = cur.get_or_insert(ArgSlice { start: k, end: k, kw: None });
                    c.end = k + 1;
                }
                Tk::Op(o) if o == "," && depth == 0 => {
                    if let Some(mut a) = cur.take() {
                        a.end = k;
                        self.arg_kw(&mut a);
                        if a.end > a.start {
                            args.push(a);
                        }
                    }
                }
                _ => {
                    let c = cur.get_or_insert(ArgSlice { start: k, end: k, kw: None });
                    c.end = k + 1;
                }
            }
            k += 1;
        }
        if let Some(mut a) = cur.take() {
            a.end = end;
            self.arg_kw(&mut a);
            if a.end > a.start {
                args.push(a);
            }
        }
        (args, end)
    }

    /// 实参分片形如 `name=expr` 时记下关键字名并剥掉。
    fn arg_kw(&self, a: &mut ArgSlice) {
        if a.end - a.start >= 2 {
            let is_kw = matches!(&self.toks[a.start].tk, Tk::Id(_))
                && matches!(&self.toks[a.start + 1].tk, Tk::Op(o) if o == "=");
            if is_kw {
                if let Tk::Id(n) = &self.toks[a.start].tk {
                    a.kw = Some(n.clone());
                }
                a.start += 2;
            }
        }
    }

    /// 从 k 起取点路径（Id . Id ...），返回 (路径, 消耗后下一未看下标)。
    fn dotted_at(&self, k: usize, _end: usize) -> Option<(String, usize)> {
        let mut parts: Vec<String> = Vec::new();
        let mut j = k;
        loop {
            match self.toks.get(j).map(|t| &t.tk) {
                Some(Tk::Id(w)) => {
                    parts.push(w.clone());
                    j += 1;
                    if matches!(self.toks.get(j).map(|t| &t.tk), Some(Tk::Op(o)) if o == ".")
                        && matches!(self.toks.get(j + 1).map(|t| &t.tk), Some(Tk::Id(_)))
                    {
                        j += 1;
                    } else {
                        break;
                    }
                }
                _ => break,
            }
        }
        if parts.len() >= 2 {
            Some((parts.join("."), j))
        } else {
            None
        }
    }

    /// `(` 的配对 `)` 下标。
    fn match_close(&self, open: usize, end: usize) -> Option<usize> {
        let mut depth = 0usize;
        for k in open..end {
            if let Tk::Op(o) = &self.toks[k].tk {
                if is_opener(o) {
                    depth += 1;
                } else if is_closer(o) {
                    depth -= 1;
                    if depth == 0 {
                        return Some(k);
                    }
                }
            }
        }
        None
    }

    /// 表达式污点判定：来源 / 污点变量 / 净化区。返回第一个未净化命中。
    fn expr_taint(&self, scope: usize, start: usize, end: usize) -> Option<Hit> {
        if self.naive || start >= end {
            return None;
        }
        // 净化区：SANITIZERS 调用的括号内部
        let mut zones: Vec<(usize, usize)> = Vec::new();
        for k in start..end {
            if let Tk::Id(w) = &self.toks[k].tk {
                if SANITIZERS.contains(&w.as_str()) {
                    if matches!(self.toks.get(k + 1).map(|t| &t.tk), Some(Tk::Op(o)) if o == "(") {
                        if let Some(cl) = self.match_close(k + 1, end) {
                            zones.push((k + 2, cl));
                        }
                    }
                }
            }
        }
        let in_zone = |x: usize| zones.iter().any(|(a, b)| x >= *a && x < *b);
        let mut k = start;
        while k < end {
            match &self.toks[k].tk {
                Tk::Id(w) => {
                    // 属性名不参与污点；var.name/stem 是净化
                    if k > start && matches!(&self.toks[k - 1].tk, Tk::Op(o) if o == ".") {
                        k += 1;
                        continue;
                    }
                    // 关键字实参名（x=...）跳过
                    if matches!(self.toks.get(k + 1).map(|t| &t.tk), Some(Tk::Op(o)) if o == "=")
                        && !KEYWORDS.contains(&w.as_str())
                        && SANITIZERS.iter().all(|s| s != w)
                    {
                        k += 1;
                        continue;
                    }
                    // var.name / var.stem：净化（该 var 的这次出现不算）
                    if (w == "name" || w == "stem") && k >= start + 2 {
                        if let Tk::Id(base) = &self.toks[k - 2].tk {
                            if self.lookup(scope, base).is_some() {
                                k += 1;
                                continue;
                            }
                        }
                    }
                    // 点路径来源
                    if let Some((dotted, next)) = self.dotted_at(k, end) {
                        let is_call = matches!(
                            self.toks.get(next).map(|t| &t.tk),
                            Some(Tk::Op(o)) if o == "("
                        );
                        let src = if dotted == "sys.argv" || dotted.starts_with("sys.argv.") {
                            Some("argv")
                        } else if SOURCE_DOTTED
                            .iter()
                            .any(|s| dotted == *s || dotted.starts_with(&format!("{}.", s)))
                        {
                            Some("env")
                        } else if dotted.starts_with("request.") {
                            Some("request")
                        } else if is_call && SOURCE_CALLS.contains(&dotted.as_str()) {
                            Some("input")
                        } else {
                            None
                        };
                        if let Some(kind) = src {
                            if !in_zone(k) {
                                return Some(Hit {
                                    var: dotted,
                                    line: self.toks[k].line,
                                    kind: kind.to_string(),
                                    interproc: false,
                                    definite: true,
                                });
                            }
                        }
                        // 链尾 .name/.stem：属性访问取出的就是净化值，整条链不算污点
                        if dotted.ends_with(".name") || dotted.ends_with(".stem") {
                            k = next;
                            continue;
                        }
                        // 非来源链：基变量污染则整条链的值视为污染
                        // （p.write_text 的接收者、tainted.strip() 等——dotted_at 已吃掉
                        // 整条链，不在这里查基变量接收者污点就永远轮不到）
                        if !in_zone(k) {
                            if let Some(base) = dotted.split('.').next() {
                                if let Some(t) = self.lookup(scope, base) {
                                    return Some(Hit {
                                        var: base.to_string(),
                                        line: t.line,
                                        kind: t.kind,
                                        interproc: t.interproc,
                                        definite: t.definite,
                                    });
                                }
                            }
                        }
                        k = next;
                        continue;
                    }
                    // 单段来源调用：input(...) / raw_input(...)（无点路径可走）
                    if (w == "input" || w == "raw_input")
                        && matches!(self.toks.get(k + 1).map(|t| &t.tk), Some(Tk::Op(o)) if o == "(")
                        && !in_zone(k)
                    {
                        return Some(Hit {
                            var: w.clone(),
                            line: self.toks[k].line,
                            kind: "input".into(),
                            interproc: false,
                            definite: true,
                        });
                    }
                    // 网络来源 .recv(
                    if (w == "recv" || w == "recvfrom")
                        && matches!(self.toks.get(k + 1).map(|t| &t.tk), Some(Tk::Op(o)) if o == "(")
                    {
                        return Some(Hit {
                            var: w.clone(),
                            line: self.toks[k].line,
                            kind: "net".into(),
                            interproc: false,
                            definite: true,
                        });
                    }
                    // 裸 argv（from sys import argv）
                    if w == "argv" && !in_zone(k) {
                        return Some(Hit {
                            var: "argv".into(),
                            line: self.toks[k].line,
                            kind: "argv".into(),
                            interproc: false,
                            definite: true,
                        });
                    }
                    // 污点变量
                    if !in_zone(k) {
                        if let Some(t) = self.lookup(scope, w) {
                            return Some(Hit {
                                var: w.clone(),
                                line: t.line,
                                kind: t.kind,
                                interproc: t.interproc,
                                definite: t.definite,
                            });
                        }
                    }
                    k += 1;
                }
                _ => k += 1,
            }
        }
        None
    }

    // ---- 跨函数一跳：实参→形参、污染返回值→调用点（不动点 ≤3 轮）

    fn pass2_interproc(&mut self) {
        let mut fns: HashMap<String, usize> = HashMap::new();
        for (id, sc) in self.scopes.iter().enumerate() {
            if id > 0 {
                fns.insert(sc.name.clone(), id);
            }
        }
        for _round in 0..3 {
            // 返回值污点重算（先算后写，避免借用冲突）
            let mut ret_flags = vec![false; self.scopes.len()];
            for id in 0..self.scopes.len() {
                let rets = self.scopes[id].rets.clone();
                for (s, e, _) in &rets {
                    if self.expr_taint(id, *s, *e).is_some() {
                        ret_flags[id] = true;
                        break;
                    }
                }
            }
            for (id, f) in ret_flags.iter().enumerate() {
                self.scopes[id].ret_tainted = *f;
            }
            let mut seeds: Vec<(usize, String, TSrc)> = Vec::new();
            for call in &self.calls {
                let base = call.callee.rsplit('.').next().unwrap_or("").to_string();
                if SANITIZERS.contains(&base.as_str()) {
                    continue; // 净化器调用不吃污点、不吐污点
                }
                if let Some(&fid) = fns.get(&base) {
                    // 实参 → 形参（按位置 / 按关键字名）
                    for (ai, a) in call.args.iter().enumerate() {
                        if let Some(hit) = self.expr_taint(call.scope, a.start, a.end) {
                            let pname = match &a.kw {
                                Some(kw) => Some(kw.clone()),
                                None => self.scopes[fid].params.get(ai).cloned(),
                            };
                            if let Some(p) = pname {
                                seeds.push((fid, p, TSrc {
                                    line: hit.line, kind: hit.kind, interproc: true,
                                    definite: hit.definite,
                                }));
                            }
                        }
                    }
                    // 污染返回值 → 调用点赋值目标
                    if self.scopes[fid].ret_tainted {
                        for t in &call.lhs {
                            seeds.push((call.scope, t.clone(), TSrc {
                                line: call.line, kind: "ret".into(), interproc: true,
                                definite: self.scopes[fid].entry,
                            }));
                        }
                    }
                }
            }
            if seeds.is_empty() {
                break;
            }
            for (scope, var, src) in seeds {
                // 只升级不降级：入口实锤（arg 带实锤）覆盖内部形参的普通来源
                let upgrade = match self.scopes[scope].taint.get(&var) {
                    Some(cur) => src.definite && !cur.definite,
                    None => true,
                };
                if upgrade {
                    self.scopes[scope].taint.insert(var, src);
                }
            }
        }
    }

    /// 汇点产出发现。
    fn pass3_findings(&self) -> Vec<Finding> {
        let mut out = Vec::new();
        let mut seen: HashSet<(String, usize, String, String)> = HashSet::new();
        for call in &self.calls {
            let class_sev = if call.method {
                if call.callee == "execute" {
                    Some(("sql", "med"))
                } else if METHOD_SINKS.contains(&call.callee.as_str()) {
                    Some(("path", "med"))
                } else {
                    None
                }
            } else if EXEC_SINKS.contains(&call.callee.as_str())
                || call.callee.starts_with("subprocess.")
            {
                Some(("exec", "high"))
            } else if PATH_SINKS.contains(&call.callee.as_str()) {
                Some(("path", "med"))
            } else {
                None
            };
            let (class, sev) = match class_sev {
                Some(x) => x,
                None => continue,
            };
            if SANITIZERS.contains(&call.callee.as_str()) {
                continue;
            }
            let hit = if self.naive {
                // 模式匹配基线：实参/接收者含任何标识符即报
                let any_id = call.args.iter().any(|a| {
                    (a.start..a.end).any(|k| matches!(self.toks[k].tk, Tk::Id(_)))
                }) || (call.method && {
                    let (s, e) = call.recv;
                    (s..e).any(|k| matches!(self.toks[k].tk, Tk::Id(_)))
                });
                any_id.then(|| Hit {
                    var: "<expr>".into(),
                    line: call.line,
                    kind: "naive".into(),
                    interproc: false,
                    definite: false,
                })
            } else {
                let mut h = None;
                if call.method {
                    let (s, e) = call.recv;
                    h = self.expr_taint(call.scope, s, e);
                    if h.is_none() && class == "sql" {
                        for a in &call.args {
                            if let Some(x) = self.expr_taint(call.scope, a.start, a.end) {
                                h = Some(x);
                                break;
                            }
                        }
                    }
                } else {
                    for a in &call.args {
                        if let Some(x) = self.expr_taint(call.scope, a.start, a.end) {
                            h = Some(x);
                            break;
                        }
                    }
                }
                h
            };
            if let Some(hit) = hit {
                let key = (self.file.clone(), call.line, call.callee.clone(), hit.var.clone());
                if seen.insert(key) {
                    let flow = if hit.interproc { "interproc" } else { "direct" };
                    // 判级：入口可达 = 实锤 definite；内部形参流 = clue（待人工确认）
                    let kind = if self.naive {
                        "naive"
                    } else if hit.definite {
                        "definite"
                    } else {
                        "clue"
                    };
                    out.push(Finding {
                        file: self.file.clone(),
                        line: call.line,
                        sink: if call.method {
                            format!(".{}", call.callee)
                        } else {
                            call.callee.clone()
                        },
                        var: hit.var,
                        source_line: hit.line,
                        source_kind: hit.kind,
                        flow: flow.into(),
                        severity: sev.into(),
                        kind: kind.into(),
                    });
                }
            }
        }
        out
    }
}

// ---------------------------------------------------------------- 入口

fn walk_py(root: &Path, out: &mut Vec<PathBuf>) {
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

/// 扫描目录/单文件。root 必须已过沙盒（CLI 层负责）。
pub fn scan_path(root: &Path, naive: bool) -> ScanResult {
    let mut files: Vec<PathBuf> = Vec::new();
    if root.is_file() {
        files.push(root.to_path_buf());
    } else {
        walk_py(root, &mut files);
        files.sort();
    }
    let mut res = ScanResult::default();
    res.files_scanned = files.len();
    for f in &files {
        let bytes = match std::fs::read(f) {
            Ok(b) => b,
            Err(e) => {
                res.errors.push(format!("{}: {}", f.display(), e));
                continue;
            }
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
        res.findings.extend(a.pass3_findings());
    }
    res
}

/// 结果 → json::Value（零依赖序列化出口）。
pub fn result_to_json(r: &ScanResult) -> json::Value {
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
    ])
}
