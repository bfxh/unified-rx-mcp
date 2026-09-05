//! pyast —— 手写 Python 迷你解析器（S83，bug_scan 原生化的地基）。
//!
//! 为什么手写：红线禁止第三方 crate（没有 rustpython/syn 可用），而 bug_scan 的
//! 未定义变量检测需要 Load/Store 上下文判定 + ast.walk 的 BFS 遍历序——正则做不到。
//!
//! 保真目标：与 CPython `ast` 的**观察等价**（不追求完整文法）：
//! - 每个节点 children 严格按 ASDL 字段声明序排列（= ast.iter_child_nodes 顺序，
//!   ast.walk 的 BFS 事件序由此决定，同文件同行 tie 的次序依赖它）
//! - Name.ctx 三值 Load/Store/Del；赋值目标只在**最外层**节点标 Store，
//!   Tuple/List/Starred 元素递归标 Store，Subscript/Attribute 内部的 Name 保持 Load
//!   （`a[i] = x` 的 a、i 都是 Load——与 ast 一致）
//! - 节点行号 = 首 token 行；f-string 的 {} 区域递归解析为 FormattedValue 子树
//! - 已知怪癖刻意保留（与 tools/scan.py::_scan_python 对齐）：
//!   Lambda 只收 args+kwonlyargs（vararg/kwarg 不算定义）→ `lambda *a: a` 报
//!   未定义变量 'a'；ClassDef 的 bases Names 无条件入 defined、keywords 不收
//! - fail-soft：match 语句按宽松的 token 扫描解析（捕获名标 Store）；罕见文法
//!   （如 3.12 的 f-string 嵌套同类引号）宁可漏事件也不 panic
//!
//! 已知偏离（语料与真实仓库不触发，见 S83 对照实验）：
//! - 未终止字符串的 SyntaxError msg 不带 "(detected at line N)" 后缀
//! - f"{a:{w}}{b}" 的 spec 子表达式事件序与 ast 相比整体提前（同为 tie 内次序）
//! - 文件头 BOM 不报错（Python 报 invalid non-printable character）

/// Name/Tuple/List/Starred/Attribute/Subscript 的访问上下文。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ctx {
    Load,
    Store,
    Del,
}

/// Constant 节点的值载荷（S84：ast_scan 的 secret_literal 要字符串值、
/// shell_like_call 的 callee 要 ast.dump 形态的数值文本）。
#[derive(Clone, Debug, PartialEq)]
pub enum CVal {
    NoneC,
    Bool(bool),
    /// 数字的源码原文（含 0x/下划线）；dump 时再定 int/float 形态
    Num(String),
    Str(String),
    Bytes(Vec<u8>),
    EllipsisC,
}

/// 迷你 AST 节点。name/name2/ctx/aux/names 按节点类型取用：
/// - `name`：Name.id、FunctionDef/ClassDef.name、arg.arg、alias.name、
///           Attribute.attr、ExceptHandler.name（except-as）、ImportFrom.module
/// - `name2`：alias.asname
/// - `aux`：ClassDef = bases 个数（children 前 aux 个是 bases）；ExceptHandler = 有无 type
/// - `names`：Global/Nonlocal 的名字表
#[derive(Clone, Debug)]
pub struct PyNode {
    pub kind: &'static str,
    pub line: usize,
    /// CPython col_offset 口径的列号（0 基、按字符计）。S84 起只对 ast_scan 用到的
    /// 节点赋值：Call（后缀链首 token，含前置括号）与字符串 Constant（含前缀）。
    pub col: usize,
    pub name: String,
    pub name2: String,
    pub ctx: Ctx,
    pub aux: usize,
    pub names: Vec<String>,
    pub cval: CVal,
    pub children: Vec<PyNode>,
}

impl PyNode {
    fn new(kind: &'static str, line: usize) -> PyNode {
        PyNode {
            kind,
            line,
            col: 0,
            name: String::new(),
            name2: String::new(),
            ctx: Ctx::Load,
            aux: 0,
            names: Vec::new(),
            cval: CVal::NoneC,
            children: Vec::new(),
        }
    }

    fn with_name(kind: &'static str, line: usize, name: String) -> PyNode {
        let mut n = PyNode::new(kind, line);
        n.name = name;
        n
    }
}

/// 解析错误：等价 SyntaxError 的 (lineno, msg)——msg 逐字对齐 3.14 实测（S83 对照实验）。
#[derive(Clone, Debug)]
pub struct PyErr {
    pub line: usize,
    pub msg: String,
}

// ---------- 词法 ----------

#[derive(Clone, Debug)]
enum Tok {
    Name(String),
    Kw(&'static str),
    /// 数字源码原文（含 0x/0o/0b/下划线/小数/指数），值形态由 dump 层再定
    Num(String),
    /// 解码后的字符串字面量值（S84：raw 不走转义；bytes 单独收集）
    Str(StrLit),
    /// f-string：内插区域表（区域源码 + 区域首行）
    FStr(Vec<FRegion>),
    Op(String),
    Newline,
    Indent,
    Dedent,
    End,
}

/// 字符串字面量的解码结果：str 用 s，bytes 用 b（互斥；隐式拼接在此之上折叠）。
#[derive(Clone, Debug)]
pub struct StrLit {
    pub s: String,
    pub b: Vec<u8>,
    pub is_bytes: bool,
}

#[derive(Clone, Debug)]
struct FRegion {
    src: String,
    line: usize,
    /// '{' 的 0 基列号：区域内第 1 行的 col 映射基准（CPython 3.12+ 位置保真）
    col: usize,
}

struct TokOut {
    kind: Tok,
    line: usize,
    /// token 起始的 0 基字符列（S84：Call/str-Constant 的 col_offset 用）
    col: usize,
}

const KEYWORDS: &[&str] = &[
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class",
    "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global",
    "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return",
    "try", "while", "with", "yield",
];
// match/case/type 是软关键字：词法一律出 Name，语句级按上下文试探（失败回退表达式）

const PREFIXES: &[&str] = &["r", "u", "b", "f", "br", "rb", "fr", "rf"];
const OPS3: &[&str] = &["**=", "//=", ">>=", "<<=", "..."];
const OPS2: &[&str] = &[
    "**", "//", "<<", ">>", "<=", ">=", "==", "!=", "->", ":=", "+=", "-=", "*=", "/=", "%=",
    "&=", "|=", "^=", "@=",
];

struct Lexer<'a> {
    src: &'a str,
    b: &'a [u8],
    pos: usize,
    line: usize,
    /// 当前 token 起始字节偏移（行首块归一后、分发前设定；push 时换算列号）
    tok_start: usize,
    /// 未闭合括号栈：(括号字符, 所在行)——EOF 报最内层（CPython 同为最后未闭合者）
    opens: Vec<(char, usize)>,
    indents: Vec<usize>,
    out: Vec<TokOut>,
    line_open: bool,
}

impl<'a> Lexer<'a> {
    fn peek(&self) -> Option<u8> {
        self.b.get(self.pos).copied()
    }

    fn peek_at(&self, k: usize) -> Option<u8> {
        self.b.get(self.pos + k).copied()
    }

    fn push(&mut self, kind: Tok) {
        let col = self.col_at(self.tok_start);
        self.line_open = true;
        self.out.push(TokOut { kind, line: self.line, col });
    }

    /// tok_start 到行首的字符数（0 基列号）。按字节回扫、跳过 UTF-8 续字节，
    /// 与 CPython 在 decode 后文本上的字符列一致（\n 恒在字符边界）。
    fn col_at(&self, start: usize) -> usize {
        let mut i = start;
        let mut col = 0usize;
        while i > 0 {
            let b = self.b[i - 1];
            if b == b'\n' {
                break;
            }
            if b & 0xC0 != 0x80 {
                col += 1;
            }
            i -= 1;
        }
        col
    }

    fn run(mut self) -> Result<Vec<TokOut>, PyErr> {
        let mut at_start = true;
        loop {
            if at_start && self.opens.is_empty() {
                // 行首缩进：空行/注释行不产生 token；制表按 CPython tab=8 折算
                let mut col = 0usize;
                loop {
                    match self.peek() {
                        Some(b' ') => {
                            col += 1;
                            self.pos += 1;
                        }
                        Some(b'\t') => {
                            col = col / 8 * 8 + 8;
                            self.pos += 1;
                        }
                        Some(0x0c) => {
                            col = 0;
                            self.pos += 1;
                        }
                        _ => break,
                    }
                }
                match self.peek() {
                    None => break,
                    Some(b'\n') => {
                        self.pos += 1;
                        self.line += 1;
                        continue;
                    }
                    Some(b'#') => {
                        while let Some(c) = self.peek() {
                            if c == b'\n' {
                                break;
                            }
                            self.pos += 1;
                        }
                        continue;
                    }
                    _ => {}
                }
                let last = *self.indents.last().unwrap();
                if col > last {
                    self.indents.push(col);
                    self.push(Tok::Indent);
                } else if col < last {
                    while *self.indents.last().unwrap() > col {
                        self.indents.pop();
                        self.push(Tok::Dedent);
                    }
                    if *self.indents.last().unwrap() != col {
                        return Err(PyErr {
                            line: self.line,
                            msg: "unindent does not match any outer indentation level".into(),
                        });
                    }
                }
                at_start = false;
            }
            self.tok_start = self.pos;
            let c = match self.peek() {
                Some(c) => c,
                None => break,
            };
            match c {
                b'\n' => {
                    self.pos += 1;
                    if self.opens.is_empty() {
                        self.push(Tok::Newline);
                        // 仅终止逻辑行的换行才开启新行首；括号内换行仍是同一逻辑行，
                        // 否则闭合后的行尾 \n 会被 gate 当空行吞掉
                        at_start = true;
                    }
                    self.line += 1;
                    self.line_open = false;
                }
                b'\\' if self.peek_at(1) == Some(b'\n') => {
                    // 反斜杠续行：逻辑行不断
                    self.pos += 2;
                    self.line += 1;
                }
                b'#' => {
                    while let Some(c) = self.peek() {
                        if c == b'\n' {
                            break;
                        }
                        self.pos += 1;
                    }
                }
                b' ' | b'\t' | 0x0c => {
                    self.pos += 1;
                }
                b'\'' | b'"' => self.lex_string(String::new(), self.pos)?,
                c if c.is_ascii_digit()
                    || (c == b'.'
                        && matches!(self.peek_at(1), Some(d) if d.is_ascii_digit())) =>
                {
                    self.lex_number();
                }
                c if c == 0xEF && self.peek_at(1) == Some(0xBB) && self.peek_at(2) == Some(0xBF) => {
                    // U+FEFF（BOM 残留）：CPython 3.12+ 词法直接拒绝
                    return Err(PyErr {
                        line: self.line,
                        msg: "invalid non-printable character U+FEFF".into(),
                    });
                }
                c if c == b'_' || c.is_ascii_alphabetic() || c >= 0x80 => {
                    self.lex_name()?;
                }
                _ => self.lex_op()?,
            }
        }
        if let Some(&(ch, ln)) = self.opens.last() {
            return Err(PyErr { line: ln, msg: format!("'{}' was never closed", ch) });
        }
        if self.line_open {
            self.out.push(TokOut { kind: Tok::Newline, line: self.line, col: 0 });
        }
        while self.indents.len() > 1 {
            self.indents.pop();
            self.out.push(TokOut { kind: Tok::Dedent, line: self.line, col: 0 });
        }
        self.out.push(TokOut { kind: Tok::End, line: self.line, col: 0 });
        Ok(self.out)
    }

    fn lex_name(&mut self) -> Result<(), PyErr> {
        let start = self.pos;
        self.pos += 1;
        while let Some(c) = self.peek() {
            if c == b'_' || c.is_ascii_alphanumeric() || c >= 0x80 {
                self.pos += 1;
            } else {
                break;
            }
        }
        let text = &self.src[start..self.pos];
        let low = text.to_lowercase();
        if PREFIXES.contains(&low.as_str()) && matches!(self.peek(), Some(b'\'' | b'"')) {
            return self.lex_string(low, start);
        }
        if let Some(k) = KEYWORDS.iter().find(|k| **k == text) {
            self.push(Tok::Kw(k));
        } else {
            self.push(Tok::Name(text.to_string()));
        }
        Ok(())
    }

    fn lex_number(&mut self) {
        let start = self.pos;
        if self.peek() == Some(b'0')
            && matches!(self.peek_at(1), Some(b'x' | b'X' | b'o' | b'O' | b'b' | b'B'))
        {
            let base_ok = |c: u8, kind: u8| match kind {
                b'x' => c.is_ascii_hexdigit() || c == b'_',
                b'o' => (b'0'..=b'7').contains(&c) || c == b'_',
                _ => c == b'0' || c == b'1' || c == b'_',
            };
            let kind = self.peek_at(1).unwrap();
            self.pos += 2;
            while let Some(c) = self.peek() {
                if base_ok(c, kind) {
                    self.pos += 1;
                } else {
                    break;
                }
            }
            self.push(Tok::Num(self.src[start..self.pos].to_string()));
            return;
        }
        while let Some(c) = self.peek() {
            if c.is_ascii_digit() || c == b'_' {
                self.pos += 1;
            } else {
                break;
            }
        }
        // 小数点：后随数字，或后随非标识符起首（覆盖 "1." 结尾；"1.a" 走属性链）
        if self.peek() == Some(b'.') {
            let nxt = self.peek_at(1);
            let take = match nxt {
                Some(d) if d.is_ascii_digit() => true,
                Some(d) => !(d == b'_' || d.is_ascii_alphabetic() || d == b'.' || d >= 0x80),
                None => true,
            };
            if take {
                self.pos += 1;
                while let Some(c) = self.peek() {
                    if c.is_ascii_digit() || c == b'_' {
                        self.pos += 1;
                    } else {
                        break;
                    }
                }
            }
        }
        // 指数（含符号）
        if matches!(self.peek(), Some(b'e' | b'E')) {
            let mut k = self.pos + 1;
            if matches!(self.b.get(k), Some(b'+' | b'-')) {
                k += 1;
            }
            if matches!(self.b.get(k), Some(d) if d.is_ascii_digit()) {
                self.pos = k;
                while let Some(c) = self.peek() {
                    if c.is_ascii_digit() || c == b'_' {
                        self.pos += 1;
                    } else {
                        break;
                    }
                }
            }
        }
        if matches!(self.peek(), Some(b'j' | b'J')) {
            self.pos += 1;
        }
        self.push(Tok::Num(self.src[start..self.pos].to_string()));
    }

    fn lex_string(&mut self, prefix: String, lit_start: usize) -> Result<(), PyErr> {
        let q = self.peek().unwrap();
        let open_line = self.line;
        let triple = self.peek_at(1) == Some(q) && self.peek_at(2) == Some(q);
        let is_f = prefix.contains('f');
        if is_f {
            return self.lex_fstring(triple, q, open_line);
        }
        self.pos += if triple { 3 } else { 1 };
        if triple {
            loop {
                if self.peek().is_none() {
                    return Err(PyErr {
                        line: open_line,
                        msg: "unterminated triple-quoted string literal".into(),
                    });
                }
                if self.peek() == Some(q) && self.peek_at(1) == Some(q) && self.peek_at(2) == Some(q)
                {
                    self.pos += 3;
                    break;
                }
                match self.peek() {
                    Some(b'\\') => {
                        self.pos += 1;
                        if self.peek() == Some(b'\n') {
                            self.line += 1;
                        }
                        self.pos += 1;
                    }
                    Some(b'\n') => {
                        self.line += 1;
                        self.pos += 1;
                    }
                    _ => self.pos += 1,
                }
            }
        } else {
            loop {
                match self.peek() {
                    None | Some(b'\n') => {
                        return Err(PyErr {
                            line: open_line,
                            msg: "unterminated string literal".into(),
                        })
                    }
                    Some(b'\\') => {
                        self.pos += 1;
                        if self.peek() == Some(b'\n') {
                            self.line += 1;
                        }
                        self.pos += 1;
                    }
                    Some(c) if c == q => {
                        self.pos += 1;
                        break;
                    }
                    _ => self.pos += 1,
                }
            }
        }
        let lit = &self.src[lit_start..self.pos];
        let sv = Self::decode_str_lit(lit, &prefix).map_err(|msg| PyErr { line: open_line, msg })?;
        self.push(Tok::Str(sv));
        Ok(())
    }

    /// 字符串字面量解码（S84）：剥前缀与引号后按 Python 转义规则还原值。
    /// raw 一字不动；未知转义保形（\q 两个字符）；\N{名字} 简化为保形（真实仓库
    /// 与语料不触发）。bytes 里非 ASCII 字符与 >255 的八进制按 CPython 报错。
    fn decode_str_lit(lit: &str, prefix: &str) -> Result<StrLit, String> {
        let is_bytes = prefix.contains('b');
        let is_raw = prefix.contains('r');
        let bb = lit.as_bytes();
        let mut k = 0usize;
        while k < bb.len() && bb[k].is_ascii_alphabetic() {
            k += 1;
        }
        let Some(&q) = bb.get(k) else {
            return Err("unterminated string literal".into());
        };
        let qlen = if k + 2 < bb.len() && bb[k + 1] == q && bb[k + 2] == q { 3 } else { 1 };
        let body = &lit[k + qlen..lit.len() - qlen];
        if is_raw {
            return Ok(StrLit { s: body.to_string(), b: Vec::new(), is_bytes });
        }
        let cs: Vec<char> = body.chars().collect();
        let mut s = String::new();
        let mut b = Vec::new();
        let put = |s: &mut String, b: &mut Vec<u8>, c: char| -> Result<(), String> {
            if is_bytes {
                if (c as u32) > 0x7F {
                    return Err("bytes can only contain ASCII literal characters".into());
                }
                b.push(c as u8);
            } else {
                s.push(c);
            }
            Ok(())
        };
        let mut i = 0usize;
        while i < cs.len() {
            let c = cs[i];
            if c != '\\' {
                put(&mut s, &mut b, c)?;
                i += 1;
                continue;
            }
            i += 1;
            if i >= cs.len() {
                break; // 完整字面量不会以孤立反斜杠结尾（词法层已排除），防御性出口
            }
            let e = cs[i];
            i += 1;
            match e {
                '\n' => {}
                '\r' => {
                    if cs.get(i) == Some(&'\n') {
                        i += 1;
                    }
                }
                'n' => put(&mut s, &mut b, '\n')?,
                't' => put(&mut s, &mut b, '\t')?,
                'r' => put(&mut s, &mut b, '\r')?,
                'a' => put(&mut s, &mut b, '\u{7}')?,
                'b' => put(&mut s, &mut b, '\u{8}')?,
                'f' => put(&mut s, &mut b, '\u{c}')?,
                'v' => put(&mut s, &mut b, '\u{b}')?,
                '\\' | '\'' | '"' => put(&mut s, &mut b, e)?,
                '0'..='7' => {
                    let mut v: u32 = e.to_digit(8).unwrap();
                    let mut n = 1;
                    while n < 3 && matches!(cs.get(i), Some('0'..='7')) {
                        v = v * 8 + cs[i].to_digit(8).unwrap();
                        i += 1;
                        n += 1;
                    }
                    if is_bytes {
                        // 转义产物可取任意字节值，ASCII 限制只针对源字符（b"\xef" 合法）
                        if v > 0xFF {
                            return Err("bytes must be in range(0, 256)".into());
                        }
                        b.push(v as u8);
                    } else {
                        s.push(char::from_u32(v).unwrap_or('\u{fffd}'));
                    }
                }
                'x' => {
                    let mut v = 0u32;
                    let mut n = 0;
                    while n < 2 && matches!(cs.get(i), Some(c) if c.is_ascii_hexdigit()) {
                        v = v * 16 + cs[i].to_digit(16).unwrap();
                        i += 1;
                        n += 1;
                    }
                    if n < 2 {
                        return Err("truncated \\xXX escape".into());
                    }
                    if is_bytes {
                        b.push(v as u8);
                    } else {
                        s.push(char::from_u32(v).unwrap());
                    }
                }
                'u' | 'U' => {
                    if is_bytes {
                        return Err("invalid \\u escape in bytes literal".into());
                    }
                    let want = if e == 'u' { 4 } else { 8 };
                    let mut v = 0u32;
                    let mut n = 0;
                    while n < want && matches!(cs.get(i), Some(c) if c.is_ascii_hexdigit()) {
                        v = v * 16 + cs[i].to_digit(16).unwrap();
                        i += 1;
                        n += 1;
                    }
                    if n < want {
                        return Err("truncated \\uXXXX escape".into());
                    }
                    match char::from_u32(v) {
                        Some(ch) => s.push(ch),
                        None => return Err("illegal Unicode character".into()),
                    }
                }
                _ => {
                    // 未知转义：CPython 保形（DeprecationWarning），\N{名字} 亦从简；
                    // bytes 的 ASCII 限制作用于源字符——转义符本身非 ASCII 也报错
                    if is_bytes {
                        if (e as u32) > 0x7F {
                            return Err("bytes can only contain ASCII literal characters".into());
                        }
                        b.push(b'\\');
                        b.push(e as u8);
                    } else {
                        s.push('\\');
                        s.push(e);
                    }
                }
            }
        }
        Ok(StrLit { s, b, is_bytes })
    }

    /// f-string 外层内容扫描：{{/}} 字面量、{区域} 提取、引号终止。
    fn lex_fstring(&mut self, triple: bool, q: u8, open_line: usize) -> Result<(), PyErr> {
        let mut regions: Vec<FRegion> = Vec::new();
        self.pos += if triple { 3 } else { 1 };
        loop {
            match self.peek() {
                None => {
                    return Err(PyErr {
                        line: open_line,
                        msg: if triple {
                            "unterminated triple-quoted string literal".into()
                        } else {
                            "unterminated string literal".into()
                        },
                    })
                }
                Some(b'\n') if !triple => {
                    return Err(PyErr { line: open_line, msg: "unterminated string literal".into() })
                }
                Some(b'\\') => {
                    self.pos += 1;
                    if self.peek() == Some(b'\n') {
                        self.line += 1;
                    }
                    self.pos += 1;
                }
                Some(c) if c == q => {
                    if triple {
                        if self.peek_at(1) == Some(q) && self.peek_at(2) == Some(q) {
                            self.pos += 3;
                            break;
                        }
                        self.pos += 1;
                    } else {
                        self.pos += 1;
                        break;
                    }
                }
                Some(b'{') if self.peek_at(1) == Some(b'{') => self.pos += 2,
                Some(b'{') => {
                    let (r, deeper) = self.fstring_region()?;
                    regions.push(r);
                    regions.extend(deeper);
                }
                Some(b'}') if self.peek_at(1) == Some(b'}') => self.pos += 2,
                Some(b'}') => {
                    return Err(PyErr {
                        line: self.line,
                        msg: "f-string: single '}' is not allowed".into(),
                    })
                }
                _ => self.pos += 1,
            }
        }
        self.push(Tok::FStr(regions));
        Ok(())
    }

    /// 单个内插区域：pos 停在 '{'，消费到配对 '}'（含可选 format spec）。
    /// 返回（外层区域, spec 内嵌套区域）；ast 事件序里嵌套在外层之后，调用方先推
    /// 外层再追加。转换符 !r/!s/!a 与调试 '=' 不属于表达式，从区域源里剪掉。
    fn fstring_region(&mut self) -> Result<(FRegion, Vec<FRegion>), PyErr> {
        let line0 = self.line;
        let brace_col = self.col_at(self.pos);
        let start = self.pos + 1;
        self.pos += 1;
        let mut depth = 1usize;
        let mut sq = 0usize; // 区域内 ( [ 嵌套深度：里面的 ':=' / 切片 ':' 不是 spec 起点
        let mut cut: Option<usize> = None; // 表达式源截断点：调试 '=' 或转换符 '!' 处
        let mut spec_regs: Vec<FRegion> = Vec::new();
        loop {
            match self.peek() {
                None => {
                    return Err(PyErr { line: line0, msg: "f-string: expecting '}'".into() });
                }
                Some(b'\n') => {
                    self.line += 1;
                    self.pos += 1;
                }
                Some(b'{') => {
                    depth += 1;
                    self.pos += 1;
                }
                Some(b'(') | Some(b'[') => {
                    sq += 1;
                    self.pos += 1;
                }
                Some(b')') | Some(b']') => {
                    if sq > 0 {
                        sq -= 1;
                    }
                    self.pos += 1;
                }
                Some(b'}') => {
                    depth -= 1;
                    self.pos += 1;
                    if depth == 0 {
                        let end = cut.unwrap_or(self.pos - 1);
                        return Ok((
                            FRegion {
                                src: self.src[start..end].to_string(),
                                line: line0,
                                col: brace_col,
                            },
                            spec_regs,
                        ));
                    }
                }
                Some(b'\'') | Some(b'"') => self.region_string()?,
                Some(b'!') if self.peek_at(1) == Some(b'=') => self.pos += 2,
                Some(b'!')
                    if matches!(self.peek_at(1), Some(b's' | b'r' | b'a'))
                        && matches!(self.peek_at(2), Some(b':') | Some(b'}')) =>
                {
                    if cut.is_none() {
                        cut = Some(self.pos); // 转换符不进表达式源
                    }
                    self.pos += 2;
                }
                Some(b'=') if depth == 1 && sq == 0 && cut.is_none()
                    && self.peek_at(1) != Some(b'=')
                    && self.b[self.pos - 1] != b'=' =>
                {
                    cut = Some(self.pos); // {name=}：'=' 起是调试文本
                    self.pos += 1;
                }
                Some(b':') if depth == 1 && sq == 0 => {
                    let end = cut.unwrap_or(self.pos);
                    self.pos += 1;
                    self.format_spec(&mut spec_regs)?;
                    return Ok((
                        FRegion {
                            src: self.src[start..end].to_string(),
                            line: line0,
                            col: brace_col,
                        },
                        spec_regs,
                    ));
                }
                _ => self.pos += 1,
            }
        }
    }

    /// format spec：到本区域收尾 '}' 为止；内部 {..} 是嵌套表达式区域，追加进 out
    /// （嵌套再带子嵌套时同样外层在前，保持 ast 事件序）。
    fn format_spec(&mut self, out: &mut Vec<FRegion>) -> Result<(), PyErr> {
        loop {
            match self.peek() {
                None => {
                    return Err(PyErr { line: self.line, msg: "f-string: expecting '}'".into() });
                }
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(());
                }
                Some(b'{') if self.peek_at(1) == Some(b'{') => self.pos += 2,
                Some(b'{') => {
                    let (r, deeper) = self.fstring_region()?;
                    out.push(r);
                    out.extend(deeper);
                }
                Some(b'\n') => {
                    self.line += 1;
                    self.pos += 1;
                }
                Some(b'\'') | Some(b'"') => self.region_string()?,
                _ => self.pos += 1,
            }
        }
    }

    /// 区域内的引号字符串整体跳过（含转义；内嵌 f-string 的区域不再展开——fail-soft）。
    fn region_string(&mut self) -> Result<(), PyErr> {
        let q = self.peek().unwrap();
        let open_line = self.line;
        self.pos += 1;
        loop {
            match self.peek() {
                None => {
                    return Err(PyErr {
                        line: open_line,
                        msg: "unterminated string literal".into(),
                    })
                }
                Some(b'\\') => {
                    self.pos += 1;
                    if self.peek() == Some(b'\n') {
                        self.line += 1;
                    }
                    self.pos += 1;
                }
                Some(c) if c == q => {
                    self.pos += 1;
                    return Ok(());
                }
                Some(b'\n') => {
                    self.line += 1;
                    self.pos += 1;
                }
                _ => self.pos += 1,
            }
        }
    }

    fn lex_op(&mut self) -> Result<(), PyErr> {
        let rest = &self.src[self.pos..];
        let mut matched = false;
        for op in OPS3 {
            if rest.starts_with(op) {
                self.pos += op.len();
                self.push(Tok::Op((*op).into()));
                matched = true;
                break;
            }
        }
        if !matched {
            for op in OPS2 {
                if rest.starts_with(op) {
                    self.pos += op.len();
                    self.push(Tok::Op((*op).into()));
                    matched = true;
                    break;
                }
            }
        }
        if !matched {
            let c = self.peek().unwrap();
            if matches!(
                c,
                b'(' | b')' | b'[' | b']' | b'{' | b'}' | b',' | b':' | b'.' | b';' | b'='
                    | b'+' | b'-' | b'*' | b'/' | b'%' | b'&' | b'|' | b'^' | b'~' | b'<'
                    | b'>' | b'@'
            ) {
                self.pos += 1;
                self.push(Tok::Op((c as char).to_string()));
                matched = true;
            }
        }
        if !matched {
            return Err(PyErr { line: self.line, msg: "invalid syntax".into() });
        }
        // 括号深度与未闭合追踪
        match self.out.last().map(|t| t.kind.text_or_empty()) {
            Some("(") | Some("[") | Some("{") => {
                let ch = self.out.last().unwrap().kind.text_or_empty().chars().next().unwrap();
                self.opens.push((ch, self.line));
            }
            Some(")") | Some("]") | Some("}") => {
                if self.opens.pop().is_none() {
                    let ch = self.out.last().unwrap().kind.text_or_empty();
                    return Err(PyErr { line: self.line, msg: format!("unmatched '{}'", ch) });
                }
            }
            _ => {}
        }
        Ok(())
    }
}

impl Tok {
    fn text_or_empty(&self) -> &str {
        match self {
            Tok::Op(s) => s.as_str(),
            _ => "",
        }
    }
}

fn tokenize(src: &str) -> Result<Vec<TokOut>, PyErr> {
    // 通用换行等价（Python read_text 默认 newline=None：\r\n 与 \r 都归一为 \n）
    let norm = src.replace("\r\n", "\n").replace('\r', "\n");
    let lx = Lexer {
        src: &norm,
        b: norm.as_bytes(),
        pos: 0,
        line: 1,
        tok_start: 0,
        opens: Vec::new(),
        indents: vec![0],
        out: Vec::new(),
        line_open: false,
    };
    lx.run()
}

// ---------- 语法 ----------

struct Parser {
    t: Vec<TokOut>,
    i: usize,
}

const AUG_OPS: &[&str] = &[
    "+=", "-=", "*=", "/=", "//=", "%=", "**=", ">>=", "<<=", "&=", "|=", "^=", "@=",
];

impl Parser {
    fn cur(&self) -> &TokOut {
        &self.t[self.i.min(self.t.len() - 1)]
    }

    fn cur_line(&self) -> usize {
        self.cur().line
    }

    fn cur_col(&self) -> usize {
        self.t[self.i].col
    }

    fn err(&self, msg: &str) -> PyErr {
        PyErr { line: self.cur_line(), msg: msg.into() }
    }

    fn at_op(&self, s: &str) -> bool {
        matches!(&self.cur().kind, Tok::Op(x) if x == s)
    }

    fn at_kw(&self, s: &str) -> bool {
        matches!(&self.cur().kind, Tok::Kw(k) if *k == s)
    }

    fn at_name(&self) -> bool {
        matches!(self.cur().kind, Tok::Name(_))
    }

    fn at_end(&self) -> bool {
        matches!(self.cur().kind, Tok::End)
    }

    /// 语句边界判断（simple_stmt 循环与尾逗号终止用）
    fn at_stmt_end(&self) -> bool {
        matches!(
            self.cur().kind,
            Tok::Newline | Tok::End | Tok::Dedent
        ) || self.at_op(")")
            || self.at_op("]")
            || self.at_op("}")
    }

    fn eat_op(&mut self, s: &str) -> bool {
        if self.at_op(s) {
            self.i += 1;
            true
        } else {
            false
        }
    }

    fn eat_kw(&mut self, s: &str) -> bool {
        if self.at_kw(s) {
            self.i += 1;
            true
        } else {
            false
        }
    }

    fn expect_op(&mut self, s: &str) -> Result<(), PyErr> {
        if self.eat_op(s) {
            Ok(())
        } else {
            Err(self.err("invalid syntax"))
        }
    }

    fn expect_kw(&mut self, s: &str) -> Result<(), PyErr> {
        if self.eat_kw(s) {
            Ok(())
        } else {
            Err(self.err("invalid syntax"))
        }
    }

    fn expect_newline(&mut self) -> Result<(), PyErr> {
        if matches!(self.cur().kind, Tok::Newline) {
            self.i += 1;
            Ok(())
        } else {
            Err(self.err("invalid syntax"))
        }
    }

    fn expect_name(&mut self) -> Result<(String, usize), PyErr> {
        match &self.cur().kind {
            Tok::Name(n) => {
                let r = (n.clone(), self.cur().line);
                self.i += 1;
                Ok(r)
            }
            _ => Err(self.err("invalid syntax")),
        }
    }

    // ----- 模块 -----

    fn run_module(&mut self) -> Result<PyNode, PyErr> {
        let mut body = Vec::new();
        loop {
            match &self.cur().kind {
                Tok::End => break,
                Tok::Newline | Tok::Dedent => self.i += 1,
                Tok::Indent => return Err(self.err("unexpected indent")),
                _ => body.extend(self.parse_statement()?),
            }
        }
        Ok(PyNode::with_children("Module", 1, body))
    }

    // ----- 语句 -----

    /// 冒号后的块：缩进块（吃一对 Indent/Dedent）或单行简单语句。
    /// 空体报 IndentationError——EOF 报语句行、否则报越界 token 行（3.14 实测口径）。
    fn block(&mut self, desc: &str, stmt_line: usize) -> Result<Vec<PyNode>, PyErr> {
        self.expect_op(":")?;
        if !matches!(self.cur().kind, Tok::Newline) {
            return self.simple_line();
        }
        self.i += 1;
        if !matches!(self.cur().kind, Tok::Indent) {
            let line = if self.at_end() { stmt_line } else { self.cur_line() };
            return Err(PyErr {
                line,
                msg: format!("expected an indented block after {} on line {}", desc, stmt_line),
            });
        }
        self.i += 1;
        let mut body = Vec::new();
        loop {
            match &self.cur().kind {
                Tok::Dedent => {
                    self.i += 1;
                    break;
                }
                Tok::End => {
                    return Err(PyErr {
                        line: stmt_line,
                        msg: format!("expected an indented block after {} on line {}", desc, stmt_line),
                    });
                }
                _ => body.extend(self.parse_statement()?),
            }
        }
        Ok(body)
    }

    fn parse_statement(&mut self) -> Result<Vec<PyNode>, PyErr> {
        match &self.cur().kind {
            Tok::Indent => Err(self.err("unexpected indent")),
            Tok::Kw("if") => Ok(vec![self.if_like("if")?]),
            Tok::Kw("elif") => Err(self.err("invalid syntax")),
            Tok::Kw("while") => Ok(vec![self.while_stmt()?]),
            Tok::Kw("for") => Ok(vec![self.for_stmt(false)?]),
            Tok::Kw("try") => Ok(vec![self.try_stmt()?]),
            Tok::Kw("with") => {
                let line = self.cur_line();
                self.i += 1;
                Ok(vec![self.with_body(line, false)?])
            }
            Tok::Kw("async") => {
                let line = self.cur_line();
                self.i += 1;
                if self.eat_kw("def") {
                    Ok(vec![self.funcdef(line, true, vec![])?])
                } else if self.eat_kw("with") {
                    Ok(vec![self.with_body(line, true)?])
                } else if self.eat_kw("for") {
                    Ok(vec![self.for_body(line, true)?])
                } else {
                    Err(self.err("invalid syntax"))
                }
            }
            Tok::Kw("def") => {
                let line = self.cur_line();
                self.i += 1;
                Ok(vec![self.funcdef(line, false, vec![])?])
            }
            Tok::Kw("class") => Ok(vec![self.classdef(vec![])?]),
            Tok::Op(s) if s == "@" => {
                let mut decs = Vec::new();
                while self.at_op("@") {
                    self.i += 1;
                    decs.push(self.parse_namedexpr()?);
                    self.expect_newline()?;
                }
                match &self.cur().kind {
                    Tok::Kw("def") => {
                        let line = self.cur_line();
                        self.i += 1;
                        Ok(vec![self.funcdef(line, false, decs)?])
                    }
                    Tok::Kw("class") => Ok(vec![self.classdef(decs)?]),
                    Tok::Kw("async") => {
                        let line = self.cur_line();
                        self.i += 1;
                        self.expect_kw("def")?;
                        Ok(vec![self.funcdef(line, true, decs)?])
                    }
                    _ => Err(self.err("invalid syntax")),
                }
            }
            // 软关键字 match：试探解析，失败回退表达式语句
            Tok::Name(n) if n == "match" => {
                let save = self.i;
                match self.match_stmt() {
                    Ok(m) => Ok(vec![m]),
                    Err(_) => {
                        self.i = save;
                        self.simple_line()
                    }
                }
            }
            _ => self.simple_line(),
        }
    }

    fn if_like(&mut self, kw: &'static str) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.expect_kw(kw)?;
        let test = self.parse_namedexpr()?;
        let desc: &'static str = if kw == "if" { "'if' statement" } else { "'elif' statement" };
        let body = self.block(desc, line)?;
        let orelse = self.else_tail()?;
        let mut n = PyNode::new("If", line);
        n.children.push(test);
        n.children.extend(body);
        n.children.extend(orelse);
        Ok(n)
    }

    fn else_tail(&mut self) -> Result<Vec<PyNode>, PyErr> {
        if self.at_kw("elif") {
            Ok(vec![self.if_like("elif")?])
        } else if self.at_kw("else") {
            let el = self.cur_line();
            self.i += 1;
            Ok(self.block("'else' statement", el)?)
        } else {
            Ok(Vec::new())
        }
    }

    fn while_stmt(&mut self) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.expect_kw("while")?;
        let test = self.parse_namedexpr()?;
        let body = self.block("'while' statement", line)?;
        let orelse = self.else_tail()?;
        let mut n = PyNode::new("While", line);
        n.children.push(test);
        n.children.extend(body);
        n.children.extend(orelse);
        Ok(n)
    }

    fn for_stmt(&mut self, is_async: bool) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.expect_kw("for")?;
        self.for_body(line, is_async)
    }

    fn for_body(&mut self, line: usize, is_async: bool) -> Result<PyNode, PyErr> {
        let target = self.parse_target_list()?;
        self.expect_kw("in")?;
        let iter = self.parse_testlist_star()?;
        let body = self.block("'for' statement", line)?;
        let orelse = self.else_tail()?;
        let mut n = PyNode::new(if is_async { "AsyncFor" } else { "For" }, line);
        n.children.push(target);
        n.children.push(iter);
        n.children.extend(body);
        n.children.extend(orelse);
        Ok(n)
    }

    fn try_stmt(&mut self) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.expect_kw("try")?;
        let body = self.block("'try' statement", line)?;
        let mut handlers = Vec::new();
        let mut has_star = false;
        while self.at_kw("except") {
            let hl = self.cur_line();
            self.i += 1;
            if self.eat_op("*") {
                has_star = true;
            }
            let (ty, name) = if self.at_op(":") {
                (None, String::new())
            } else {
                let t = self.parse_expr()?;
                let nm = if self.eat_kw("as") { self.expect_name()?.0 } else { String::new() };
                (Some(t), nm)
            };
            let hbody = self.block("'except' statement", hl)?;
            let mut h = PyNode::with_name("ExceptHandler", hl, name);
            h.aux = if ty.is_some() { 1 } else { 0 };
            if let Some(t) = ty {
                h.children.push(t);
            }
            h.children.extend(hbody);
            handlers.push(h);
        }
        let orelse = if self.at_kw("else") {
            let el = self.cur_line();
            self.i += 1;
            self.block("'else' statement", el)?
        } else {
            Vec::new()
        };
        let finalbody = if self.at_kw("finally") {
            let fl = self.cur_line();
            self.i += 1;
            self.block("'finally' statement", fl)?
        } else {
            Vec::new()
        };
        if handlers.is_empty() && finalbody.is_empty() {
            // CPython 报在 body 结束处：回退到最近一个 Newline token 的行
            // （多行语句算到收尾行；Dedent 行号是行尾累计，不可用）
            let mut bl = line;
            let mut j = self.i;
            while j > 0 {
                j -= 1;
                if matches!(self.t[j].kind, Tok::Newline) {
                    bl = self.t[j].line;
                    break;
                }
            }
            return Err(PyErr {
                line: bl,
                msg: "expected 'except' or 'finally' block".into(),
            });
        }
        let mut n = PyNode::new(if has_star { "TryStar" } else { "Try" }, line);
        n.children.extend(body);
        n.children.extend(handlers);
        n.children.extend(orelse);
        n.children.extend(finalbody);
        Ok(n)
    }

    fn with_body(&mut self, line: usize, is_async: bool) -> Result<PyNode, PyErr> {
        let items = self.with_items()?;
        let body = self.block("'with' statement", line)?;
        let mut n = PyNode::new(if is_async { "AsyncWith" } else { "With" }, line);
        n.children.extend(items);
        n.children.extend(body);
        Ok(n)
    }

    fn with_items(&mut self) -> Result<Vec<PyNode>, PyErr> {
        // 3.10+ 括号包裹的多项 with：试探 + 回退
        if self.at_op("(") {
            let save = self.i;
            self.i += 1;
            let r = self.try_paren_with_items();
            match r {
                Ok(items) => return Ok(items),
                Err(_) => self.i = save,
            }
        }
        let mut items = Vec::new();
        loop {
            let e = self.parse_expr()?;
            let mut item = PyNode::new("withitem", e.line);
            item.children.push(e);
            if self.eat_kw("as") {
                item.children.push(self.parse_target()?);
            }
            items.push(item);
            if !self.eat_op(",") {
                break;
            }
        }
        Ok(items)
    }

    fn try_paren_with_items(&mut self) -> Result<Vec<PyNode>, PyErr> {
        let mut items = Vec::new();
        loop {
            if self.at_op(")") {
                return Err(self.err("invalid syntax")); // 空括号不是 with 项
            }
            let e = self.parse_expr()?;
            let mut item = PyNode::new("withitem", e.line);
            item.children.push(e);
            if self.eat_kw("as") {
                item.children.push(self.parse_target()?);
            }
            items.push(item);
            if self.eat_op(",") {
                continue;
            }
            if self.eat_op(")") {
                return Ok(items);
            }
            return Err(self.err("invalid syntax"));
        }
    }

    fn funcdef(&mut self, line: usize, is_async: bool, decs: Vec<PyNode>) -> Result<PyNode, PyErr> {
        let name = self.expect_name()?.0;
        self.expect_op("(")?;
        let args = self.params(true)?;
        self.expect_op(")")?;
        let ret = if self.eat_op("->") { Some(self.parse_expr()?) } else { None };
        let body = self.block("function definition", line)?;
        let mut n = PyNode::with_name(
            if is_async { "AsyncFunctionDef" } else { "FunctionDef" },
            line,
            name,
        );
        n.children.push(args);
        n.children.extend(body);
        n.children.extend(decs);
        if let Some(r) = ret {
            n.children.push(r);
        }
        Ok(n)
    }

    /// 参数表（def 与 lambda 共用；lambda 以 ':' 收尾、无注解）。
    /// arguments children 按 ASDL 序：posonlyargs, args, vararg, kwonlyargs,
    /// kw_defaults, kwarg, defaults（collector 的 Lambda 怪癖据此只认 kind=="arg"）。
    fn params(&mut self, annot: bool) -> Result<PyNode, PyErr> {
        let aline = self.cur_line();
        let mut posonly: Vec<PyNode> = Vec::new();
        let mut args: Vec<PyNode> = Vec::new();
        let mut vararg: Option<PyNode> = None;
        let mut kwonly: Vec<PyNode> = Vec::new();
        let mut kw_def: Vec<PyNode> = Vec::new();
        let mut kwarg: Option<PyNode> = None;
        let mut defaults: Vec<PyNode> = Vec::new();
        let mut star_seen = false;
        loop {
            if self.at_op(")") || self.at_op(":") {
                break;
            }
            // */** 分支 continue 跳过了底部的逗号消费，这里补上（含 "f(*,)" 尾逗号）
            self.eat_op(",");
            if self.at_op(")") || self.at_op(":") {
                break;
            }
            if self.eat_op("/") {
                posonly = std::mem::take(&mut args);
                continue;
            }
            if self.eat_op("**") {
                kwarg = Some(self.arg_node(annot, "kwarg")?);
                continue;
            }
            if self.eat_op("*") {
                if star_seen {
                    return Err(self.err("invalid syntax"));
                }
                star_seen = true;
                if self.at_name() {
                    vararg = Some(self.arg_node(annot, "vararg")?);
                }
                continue;
            }
            let a = self.arg_node(annot, "arg")?;
            if self.eat_op("=") {
                let d = self.parse_expr()?;
                if star_seen {
                    kw_def.push(d);
                } else {
                    defaults.push(d);
                }
            }
            if star_seen {
                kwonly.push(a);
            } else {
                args.push(a);
            }
            if !self.eat_op(",") {
                break;
            }
        }
        let mut n = PyNode::new("arguments", aline);
        n.children.extend(posonly);
        n.children.extend(args);
        n.children.extend(vararg);
        n.children.extend(kwonly);
        n.children.extend(kw_def);
        n.children.extend(kwarg);
        n.children.extend(defaults);
        Ok(n)
    }

    fn arg_node(&mut self, annot: bool, kind: &'static str) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        let name = self.expect_name()?.0;
        let mut n = PyNode::with_name(kind, line, name);
        if annot && self.eat_op(":") {
            n.children.push(self.parse_expr()?);
        }
        Ok(n)
    }

    fn classdef(&mut self, decs: Vec<PyNode>) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.expect_kw("class")?;
        let name = self.expect_name()?.0;
        let mut bases = Vec::new();
        let mut kws = Vec::new();
        if self.eat_op("(") {
            if !self.at_op(")") {
                loop {
                    if self.eat_op("**") {
                        let v = self.parse_expr()?;
                        let mut k = PyNode::new("keyword", line);
                        k.children.push(v);
                        kws.push(k);
                    } else if self.eat_op("*") {
                        let bl = self.cur_line();
                        let v = self.parse_expr()?;
                        let mut s = PyNode::new("Starred", bl);
                        s.children.push(v);
                        bases.push(s);
                    } else if self.at_name() && self.peek2_is_op("=") {
                        let kn = self.expect_name()?.0;
                        self.expect_op("=")?;
                        let v = self.parse_expr()?;
                        let mut k = PyNode::with_name("keyword", line, kn);
                        k.children.push(v);
                        kws.push(k);
                    } else {
                        bases.push(self.parse_expr()?);
                    }
                    if !self.eat_op(",") {
                        break;
                    }
                    if self.at_op(")") {
                        break;
                    }
                }
            }
            self.expect_op(")")?;
        }
        let body = self.block("class definition", line)?;
        let mut n = PyNode::with_name("ClassDef", line, name);
        n.aux = bases.len();
        n.children.extend(bases);
        n.children.extend(kws);
        n.children.extend(body);
        n.children.extend(decs);
        Ok(n)
    }

    /// 软关键字 match 语句：宽松解析（捕获名标 Store；class-pattern 成员名 Load）。
    fn match_stmt(&mut self) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.i += 1; // 'match'
        let subject = self.parse_testlist_star()?;
        self.expect_op(":")?;
        if !matches!(self.cur().kind, Tok::Newline) {
            return Err(self.err("invalid syntax"));
        }
        self.i += 1;
        if !matches!(self.cur().kind, Tok::Indent) {
            return Err(self.err("invalid syntax"));
        }
        self.i += 1;
        let mut cases = Vec::new();
        loop {
            let is_case = matches!(&self.cur().kind, Tok::Name(n) if n == "case");
            if !is_case {
                break;
            }
            let cl = self.cur_line();
            self.i += 1;
            let pat = self.parse_case_pattern()?;
            let mut c = PyNode::new("match_case", cl);
            c.children.push(pat);
            if self.eat_kw("if") {
                c.children.push(self.parse_expr()?);
            }
            c.children.extend(self.block("'case' block", cl)?); // block 自吃 ':'
            cases.push(c);
        }
        if cases.is_empty() {
            return Err(self.err("invalid syntax"));
        }
        if !matches!(self.cur().kind, Tok::Dedent) {
            return Err(self.err("invalid syntax"));
        }
        self.i += 1;
        let mut n = PyNode::new("Match", line);
        n.children.push(subject);
        n.children.extend(cases);
        Ok(n)
    }

    /// case 模式解析：节点形状与 ast 的 Match* 同名同序（捕获名进 MatchAs.name /
    /// MatchStar.name / MatchMapping.name 字段，kwd_attrs 记入 name2——这些是字符串
    /// 字段，ast.walk 同样不产生 Name 事件）。'(' 组单元素透明，逗号分隔折叠为
    /// MatchSequence；映射的 children 先 keys 后值模式（ASDL 字段序）。
    fn parse_case_pattern(&mut self) -> Result<PyNode, PyErr> {
        let first = self.parse_as_pattern()?;
        if self.at_op(",") {
            let mut seq = PyNode::new("MatchSequence", first.line);
            seq.children.push(first);
            while self.eat_op(",") {
                if self.at_op(":") || self.at_kw("if") {
                    break;
                }
                seq.children.push(self.parse_as_pattern()?);
            }
            return Ok(seq);
        }
        Ok(first)
    }

    fn parse_as_pattern(&mut self) -> Result<PyNode, PyErr> {
        let p = self.parse_or_pattern()?;
        if self.eat_kw("as") {
            let (nm, l) = self.expect_name()?;
            let mut a = PyNode::with_name("MatchAs", l, nm);
            a.children.push(p);
            return Ok(a);
        }
        Ok(p)
    }

    fn parse_or_pattern(&mut self) -> Result<PyNode, PyErr> {
        let p = self.parse_closed_pattern()?;
        if self.at_op("|") {
            let mut or = PyNode::new("MatchOr", p.line);
            or.children.push(p);
            while self.eat_op("|") {
                or.children.push(self.parse_closed_pattern()?);
            }
            return Ok(or);
        }
        Ok(p)
    }

    fn parse_closed_pattern(&mut self) -> Result<PyNode, PyErr> {
        match self.cur().kind.clone() {
            Tok::Op(s) if s == "[" => {
                let l = self.cur_line();
                self.i += 1;
                let mut n = PyNode::new("MatchSequence", l);
                while !self.at_op("]") {
                    if self.eat_op("*") {
                        let (nm, sl) = self.expect_name()?;
                        n.children.push(PyNode::with_name("MatchStar", sl, nm));
                    } else {
                        n.children.push(self.parse_as_pattern()?);
                    }
                    if !self.eat_op(",") {
                        break;
                    }
                }
                self.expect_op("]")?;
                Ok(n)
            }
            Tok::Op(s) if s == "{" => {
                let l = self.cur_line();
                self.i += 1;
                let mut keys = Vec::new();
                let mut pats = Vec::new();
                let mut rest = String::new();
                while !self.at_op("}") {
                    if self.eat_op("**") {
                        let (nm, _) = self.expect_name()?;
                        rest = nm;
                    } else {
                        keys.push(self.parse_value_expr()?);
                        self.expect_op(":")?;
                        pats.push(self.parse_as_pattern()?);
                    }
                    if !self.eat_op(",") {
                        break;
                    }
                }
                self.expect_op("}")?;
                let mut n = PyNode::new("MatchMapping", l);
                n.children.extend(keys);
                n.children.extend(pats);
                n.name = rest; // rest 字段只记录，不产生事件
                Ok(n)
            }
            Tok::Op(s) if s == "(" => {
                // 组模式：单元素透明（组不产生节点）；逗号分隔折叠为 MatchSequence
                let l = self.cur_line();
                self.i += 1;
                if self.at_op(")") {
                    self.i += 1;
                    return Ok(PyNode::new("MatchSequence", l));
                }
                let first = self.parse_as_pattern()?;
                if self.at_op(",") {
                    let mut n = PyNode::new("MatchSequence", l);
                    n.children.push(first);
                    while self.eat_op(",") {
                        if self.at_op(")") {
                            break;
                        }
                        n.children.push(self.parse_as_pattern()?);
                    }
                    self.expect_op(")")?;
                    return Ok(n);
                }
                self.expect_op(")")?;
                Ok(first)
            }
            Tok::Name(nm) => {
                let l = self.cur_line();
                if self.peek2_is_op("(") {
                    let cls = self.parse_dotted_name()?;
                    self.class_pattern_with(cls)
                } else if self.peek2_is_op(".") {
                    let v = self.parse_dotted_name()?;
                    if self.at_op("(") {
                        return self.class_pattern_with(v);
                    }
                    let mut n = PyNode::new("MatchValue", l);
                    n.children.push(v);
                    Ok(n)
                } else {
                    self.i += 1;
                    if nm == "_" {
                        Ok(PyNode::new("MatchAs", l)) // 通配：name 为 None
                    } else {
                        Ok(PyNode::with_name("MatchAs", l, nm)) // 捕获
                    }
                }
            }
            _ => {
                let v = self.parse_value_expr()?;
                let mut n = PyNode::new("MatchValue", v.line);
                n.children.push(v);
                Ok(n)
            }
        }
    }

    /// 值模式/映射键的字面量：-/+ 一元、字面量 atom、a.b 点链（Load 语境）
    fn parse_value_expr(&mut self) -> Result<PyNode, PyErr> {
        if self.at_op("-") || self.at_op("+") {
            let l = self.cur_line();
            self.i += 1;
            let v = self.parse_value_expr()?;
            let mut n = PyNode::new("UnaryOp", l);
            n.children.push(v);
            return Ok(n);
        }
        match self.cur().kind.clone() {
            Tok::Name(_) => self.parse_dotted_name(),
            _ => self.atom(),
        }
    }

    /// a.b.c 点链（值模式 / 类模式的 cls）
    fn parse_dotted_name(&mut self) -> Result<PyNode, PyErr> {
        let (nm, l) = self.expect_name()?;
        let mut node = PyNode::with_ctx("Name", l, nm, Ctx::Load);
        while self.eat_op(".") {
            let (attr, _) = self.expect_name()?;
            let mut n = PyNode::with_name("Attribute", node.line, attr);
            n.children.push(node);
            node = n;
        }
        Ok(node)
    }

    /// C(..., x=p) 类模式：cls、位置模式、关键字模式（kwd_attrs 是字符串字段，无事件）
    fn class_pattern_with(&mut self, cls: PyNode) -> Result<PyNode, PyErr> {
        let l = cls.line;
        self.i += 1; // '('
        let mut n = PyNode::new("MatchClass", l);
        n.children.push(cls);
        while !self.at_op(")") {
            if self.at_name() && self.peek2_is_op("=") {
                let (kn, _) = self.expect_name()?;
                self.i += 1; // '='
                if !n.name2.is_empty() {
                    n.name2.push(',');
                }
                n.name2.push_str(&kn);
                n.children.push(self.parse_as_pattern()?);
            } else {
                n.children.push(self.parse_as_pattern()?);
            }
            if !self.eat_op(",") {
                break;
            }
        }
        self.expect_op(")")?;
        Ok(n)
    }

    fn simple_line(&mut self) -> Result<Vec<PyNode>, PyErr> {
        let mut out = vec![self.small_stmt()?];
        while self.eat_op(";") {
            if matches!(self.cur().kind, Tok::Newline | Tok::End | Tok::Dedent) {
                break;
            }
            out.push(self.small_stmt()?);
        }
        if matches!(self.cur().kind, Tok::Newline) {
            self.i += 1;
        } else if !matches!(self.cur().kind, Tok::End | Tok::Dedent) {
            return Err(self.err("invalid syntax"));
        }
        Ok(out)
    }

    fn small_stmt(&mut self) -> Result<PyNode, PyErr> {
        match &self.cur().kind {
            Tok::Kw("pass") => {
                let l = self.cur_line();
                self.i += 1;
                Ok(PyNode::new("Pass", l))
            }
            Tok::Kw("break") => {
                let l = self.cur_line();
                self.i += 1;
                Ok(PyNode::new("Break", l))
            }
            Tok::Kw("continue") => {
                let l = self.cur_line();
                self.i += 1;
                Ok(PyNode::new("Continue", l))
            }
            Tok::Kw("return") => {
                let l = self.cur_line();
                self.i += 1;
                let mut n = PyNode::new("Return", l);
                if !self.at_stmt_end() && !self.at_op(";") {
                    n.children.push(self.parse_testlist_star()?);
                }
                Ok(n)
            }
            Tok::Kw("raise") => {
                let l = self.cur_line();
                self.i += 1;
                let mut n = PyNode::new("Raise", l);
                if !self.at_stmt_end() && !self.at_op(";") {
                    n.children.push(self.parse_expr()?);
                    if self.eat_kw("from") {
                        n.children.push(self.parse_expr()?);
                    }
                }
                Ok(n)
            }
            Tok::Kw("assert") => {
                let l = self.cur_line();
                self.i += 1;
                let mut n = PyNode::new("Assert", l);
                n.children.push(self.parse_expr()?);
                if self.eat_op(",") {
                    n.children.push(self.parse_expr()?);
                }
                Ok(n)
            }
            Tok::Kw("global") | Tok::Kw("nonlocal") => {
                let l = self.cur_line();
                let kw = if self.at_kw("global") { "Global" } else { "Nonlocal" };
                self.i += 1;
                let mut names = vec![self.expect_name()?.0];
                while self.eat_op(",") {
                    names.push(self.expect_name()?.0);
                }
                let mut n = PyNode::new(kw, l);
                n.names = names;
                Ok(n)
            }
            Tok::Kw("import") => self.import_stmt(),
            Tok::Kw("from") => self.from_stmt(),
            Tok::Kw("del") => self.del_stmt(),
            Tok::Kw("yield") => {
                let l = self.cur_line();
                let y = self.yield_expr()?;
                let mut n = PyNode::new("Expr", l);
                n.children.push(y);
                Ok(n)
            }
            _ => self.expr_stmt(),
        }
    }

    fn import_stmt(&mut self) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.i += 1;
        let mut aliases = Vec::new();
        loop {
            let mut name = self.expect_name()?.0;
            while self.eat_op(".") {
                name.push('.');
                name.push_str(&self.expect_name()?.0);
            }
            let mut a = PyNode::with_name("alias", line, name);
            if self.eat_kw("as") {
                a.name2 = self.expect_name()?.0;
            }
            aliases.push(a);
            if !self.eat_op(",") {
                break;
            }
        }
        let mut n = PyNode::new("Import", line);
        n.children = aliases;
        Ok(n)
    }

    fn from_stmt(&mut self) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.i += 1;
        while self.eat_op(".") {}
        let module = if self.at_name() {
            let mut name = self.expect_name()?.0;
            while self.eat_op(".") {
                name.push('.');
                name.push_str(&self.expect_name()?.0);
            }
            name
        } else {
            String::new()
        };
        self.expect_kw("import")?;
        let mut aliases = Vec::new();
        if self.eat_op("(") {
            loop {
                if self.at_op(")") {
                    break;
                }
                aliases.push(self.import_alias(line)?);
                if !self.eat_op(",") {
                    break;
                }
            }
            self.expect_op(")")?;
        } else if self.eat_op("*") {
            aliases.push(PyNode::with_name("alias", line, "*".into()));
        } else {
            loop {
                aliases.push(self.import_alias(line)?);
                if !self.eat_op(",") {
                    break;
                }
            }
        }
        let mut n = PyNode::with_name("ImportFrom", line, module);
        n.children = aliases;
        Ok(n)
    }

    fn import_alias(&mut self, line: usize) -> Result<PyNode, PyErr> {
        if self.eat_op("*") {
            return Ok(PyNode::with_name("alias", line, "*".into()));
        }
        let name = self.expect_name()?.0;
        let mut a = PyNode::with_name("alias", line, name);
        if self.eat_kw("as") {
            a.name2 = self.expect_name()?.0;
        }
        Ok(a)
    }

    fn del_stmt(&mut self) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.i += 1;
        let mut n = PyNode::new("Delete", line);
        loop {
            let sl = self.cur_line();
            let starred = self.eat_op("*");
            let mut t = self.parse_or()?;
            if starred {
                return Err(PyErr { line: sl, msg: "cannot delete starred".into() });
            }
            self.mark_del(&mut t)?;
            n.children.push(t);
            if !self.eat_op(",") {
                break;
            }
        }
        Ok(n)
    }

    fn mark_del(&self, node: &mut PyNode) -> Result<(), PyErr> {
        match node.kind {
            "Name" | "Attribute" | "Subscript" => {
                node.ctx = Ctx::Del;
                Ok(())
            }
            "Tuple" | "List" => {
                node.ctx = Ctx::Del;
                for c in node.children.iter_mut() {
                    self.mark_del(c)?;
                }
                Ok(())
            }
            _ => Err(PyErr { line: node.line, msg: "invalid syntax".into() }),
        }
    }

    fn expr_stmt(&mut self) -> Result<PyNode, PyErr> {
        let e1 = self.parse_testlist_star()?;
        let aug = match &self.cur().kind {
            Tok::Op(s) if AUG_OPS.contains(&s.as_str()) => true,
            _ => false,
        };
        if aug {
            self.i += 1;
            let target = self.mark_target(e1, false)?;
            let value = self.parse_testlist_star()?;
            let mut n = PyNode::new("AugAssign", target.line);
            n.children.push(target);
            n.children.push(value);
            return Ok(n);
        }
        if self.at_op("=") {
            let first = self.mark_target(e1, true)?;
            let mut targets = vec![first];
            let value;
            loop {
                self.i += 1; // '='
                let e = if self.at_kw("yield") {
                    self.yield_expr()?
                } else {
                    self.parse_testlist_star()?
                };
                if self.at_op("=") {
                    let t = self.mark_target(e, true)?;
                    targets.push(t);
                } else {
                    value = e;
                    break;
                }
            }
            let mut n = PyNode::new("Assign", targets[0].line);
            n.children = targets;
            n.children.push(value);
            return Ok(n);
        }
        if self.at_op(":") && matches!(e1.kind, "Name" | "Attribute" | "Subscript") {
            let line = e1.line;
            self.i += 1;
            let ann = self.parse_expr()?;
            let target = self.mark_target(e1, false)?;
            let mut n = PyNode::new("AnnAssign", line);
            n.children.push(target);
            n.children.push(ann);
            if self.eat_op("=") {
                n.children.push(self.parse_testlist_star()?);
            }
            return Ok(n);
        }
        let mut n = PyNode::new("Expr", e1.line);
        n.children.push(e1);
        Ok(n)
    }

    /// 赋值目标标 Store：只标最外层；Tuple/List/Starred 元素递归；
    /// Subscript/Attribute 内部 Name 保持 Load（与 ast 完全一致）。
    fn mark_target(&self, node: PyNode, tuple_ok: bool) -> Result<PyNode, PyErr> {
        match node.kind {
            "Name" | "Attribute" | "Subscript" => Ok(PyNode { ctx: Ctx::Store, ..node }),
            "Tuple" | "List" | "Starred" if tuple_ok => {
                let mut node = PyNode { ctx: Ctx::Store, ..node };
                let mut kids = Vec::with_capacity(node.children.len());
                for c in std::mem::take(&mut node.children) {
                    kids.push(self.mark_target(c, true)?);
                }
                node.children = kids;
                Ok(node)
            }
            _ => Err(PyErr { line: node.line, msg: "cannot assign to expression".into() }),
        }
    }

    fn yield_expr(&mut self) -> Result<PyNode, PyErr> {
        let l = self.cur_line();
        self.i += 1; // 'yield'
        if self.eat_kw("from") {
            let mut n = PyNode::new("YieldFrom", l);
            n.children.push(self.parse_expr()?);
            return Ok(n);
        }
        let mut n = PyNode::new("Yield", l);
        if !self.at_stmt_end()
            && !self.at_op(";")
            && !self.at_op(")")
            && !self.at_op("=")
            && !self.at_op(":")
        {
            n.children.push(self.parse_testlist_star()?);
        }
        Ok(n)
    }

    // ----- 表达式 -----

    fn parse_namedexpr(&mut self) -> Result<PyNode, PyErr> {
        if self.at_name() && self.peek2_is_op(":=") {
            let l = self.cur_line();
            let name = self.expect_name()?.0;
            self.i += 1; // ':='
            let value = self.parse_expr()?;
            let mut n = PyNode::new("NamedExpr", l);
            n.children.push(PyNode::with_ctx("Name", l, name, Ctx::Store));
            n.children.push(value);
            return Ok(n);
        }
        self.parse_expr()
    }

    fn parse_testlist_star(&mut self) -> Result<PyNode, PyErr> {
        if self.at_kw("yield") {
            return self.yield_expr();
        }
        let first = self.star_elt()?;
        if !self.at_op(",") {
            return Ok(first);
        }
        let mut elts = vec![first];
        while self.eat_op(",") {
            if self.at_stmt_end() || self.at_op("=") || self.at_op(":") {
                break;
            }
            elts.push(self.star_elt()?);
        }
        let mut n = PyNode::new("Tuple", elts[0].line);
        n.children = elts;
        Ok(n)
    }

    fn parse_expr(&mut self) -> Result<PyNode, PyErr> {
        if self.at_kw("lambda") {
            return self.lambda_expr();
        }
        let e = self.parse_or()?;
        if self.at_kw("if") {
            self.i += 1;
            let cond = self.parse_or()?;
            self.expect_kw("else")?;
            let els = self.parse_expr()?;
            let mut n = PyNode::new("IfExp", e.line);
            n.children.push(cond);
            n.children.push(e);
            n.children.push(els);
            return Ok(n);
        }
        Ok(e)
    }

    fn lambda_expr(&mut self) -> Result<PyNode, PyErr> {
        let l = self.cur_line();
        self.i += 1;
        let args = self.params(false)?;
        self.expect_op(":")?;
        let body = self.parse_expr()?;
        let mut n = PyNode::new("Lambda", l);
        n.children.push(args);
        n.children.push(body);
        Ok(n)
    }

    fn parse_or(&mut self) -> Result<PyNode, PyErr> {
        let mut parts = vec![self.parse_and()?];
        while self.eat_kw("or") {
            parts.push(self.parse_and()?);
        }
        if parts.len() == 1 {
            Ok(parts.pop().unwrap())
        } else {
            let l = parts[0].line;
            let mut n = PyNode::with_children("BoolOp", l, parts);
            n.name = "Or".into();
            Ok(n)
        }
    }

    fn parse_and(&mut self) -> Result<PyNode, PyErr> {
        let mut parts = vec![self.parse_not()?];
        while self.eat_kw("and") {
            parts.push(self.parse_not()?);
        }
        if parts.len() == 1 {
            Ok(parts.pop().unwrap())
        } else {
            let l = parts[0].line;
            let mut n = PyNode::with_children("BoolOp", l, parts);
            n.name = "And".into();
            Ok(n)
        }
    }

    fn parse_not(&mut self) -> Result<PyNode, PyErr> {
        if self.eat_kw("not") {
            let l = self.cur_line();
            let child = self.parse_not()?;
            let mut n = PyNode::new("UnaryOp", l);
            n.name = "Not".into();
            n.children.push(child);
            Ok(n)
        } else {
            self.parse_comparison()
        }
    }

    fn parse_comparison(&mut self) -> Result<PyNode, PyErr> {
        let left = self.parse_bitor()?;
        let mut comps = Vec::new();
        let mut ops: Vec<&'static str> = Vec::new();
        loop {
            let is_op = matches!(&self.cur().kind,
                Tok::Op(s) if matches!(s.as_str(), "==" | "!=" | "<" | "<=" | ">" | ">="));
            if is_op || self.at_kw("in") {
                ops.push(match &self.cur().kind {
                    Tok::Op(s) => ast_op_name(s),
                    _ => "In",
                });
                self.i += 1;
                comps.push(self.parse_bitor()?);
            } else if self.at_kw("is") {
                self.i += 1;
                ops.push(if self.eat_kw("not") { "IsNot" } else { "Is" });
                comps.push(self.parse_bitor()?);
            } else if self.at_kw("not") && self.peek2_is_kw("in") {
                self.i += 2;
                ops.push("NotIn");
                comps.push(self.parse_bitor()?);
            } else {
                break;
            }
        }
        if comps.is_empty() {
            Ok(left)
        } else {
            let mut n = PyNode::new("Compare", left.line);
            n.names = ops.iter().map(|s| s.to_string()).collect();
            n.children.push(left);
            n.children.extend(comps);
            Ok(n)
        }
    }

    fn parse_bitor(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_bitxor, &["|"])
    }

    fn parse_bitxor(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_bitand, &["^"])
    }

    fn parse_bitand(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_shift, &["&"])
    }

    fn parse_shift(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_arith, &["<<", ">>"])
    }

    /// 左结合二元链：children 按 [左, 右, 右, …] 嵌套（= ast 的 BinOp 树形）；
    /// name 记 ast 算子名（S84：dump_expr 需要 Add/FloorDiv 等原名）
    fn binop_chain(
        &mut self,
        sub: fn(&mut Parser) -> Result<PyNode, PyErr>,
        ops: &[&str],
    ) -> Result<PyNode, PyErr> {
        let mut node = sub(self)?;
        loop {
            let Some(op) = ops.iter().find(|op| self.at_op(op)) else {
                break;
            };
            self.i += 1;
            let rhs = sub(self)?;
            let l = node.line;
            let mut n = PyNode::new("BinOp", l);
            n.name = ast_op_name(op).to_string();
            n.children.push(std::mem::replace(&mut node, PyNode::new("Pass", l)));
            n.children.push(rhs);
            node = n;
        }
        Ok(node)
    }

    fn parse_arith(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_term, &["+", "-"])
    }

    fn parse_term(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_factor, &["*", "/", "//", "%", "@"])
    }

    fn parse_factor(&mut self) -> Result<PyNode, PyErr> {
        if matches!(&self.cur().kind, Tok::Op(s) if s == "+" || s == "-" || s == "~") {
            let l = self.cur_line();
            let op = match &self.cur().kind {
                Tok::Op(s) if s == "+" => "UAdd",
                Tok::Op(s) if s == "-" => "USub",
                _ => "Invert",
            };
            self.i += 1;
            let child = self.parse_factor()?;
            let mut n = PyNode::new("UnaryOp", l);
            n.name = op.to_string();
            n.children.push(child);
            return Ok(n);
        }
        if self.at_kw("await") {
            let l = self.cur_line();
            self.i += 1;
            let child = self.parse_factor()?;
            let mut n = PyNode::new("Await", l);
            n.children.push(child);
            return Ok(n);
        }
        self.parse_power()
    }

    fn parse_power(&mut self) -> Result<PyNode, PyErr> {
        let base = self.parse_postfix()?;
        if self.eat_op("**") {
            let rhs = self.parse_factor()?; // 右结合
            let mut n = PyNode::new("BinOp", base.line);
            n.children.push(base);
            n.children.push(rhs);
            Ok(n)
        } else {
            Ok(base)
        }
    }

    fn parse_postfix(&mut self) -> Result<PyNode, PyErr> {
        // CPython 口径：链式节点的 col_offset = 链首 token（含前置括号/下标括号），
        // 每过一个 trailer 都回写链首列（Call 与 Attribute/Subscript 一致）
        let start_col = self.cur_col();
        let mut node = self.atom()?;
        loop {
            if self.at_op("(") {
                node = self.call_args(node)?;
            } else if self.at_op("[") {
                node = self.subscript(node)?;
            } else if self.at_op(".") {
                self.i += 1;
                let (attr, _) = self.expect_name()?;
                let l = node.line;
                let mut n = PyNode::with_name("Attribute", l, attr);
                n.children.push(node);
                node = n;
            } else {
                break;
            }
            node.col = start_col;
        }
        Ok(node)
    }

    fn call_args(&mut self, func: PyNode) -> Result<PyNode, PyErr> {
        let l = func.line;
        self.i += 1; // '('
        let mut n = PyNode::new("Call", l);
        n.children.push(func);
        if !self.at_op(")") {
            loop {
                if self.eat_op("*") {
                    let l = self.cur_line();
                    let v = self.parse_expr()?;
                    let mut s = PyNode::new("Starred", l);
                    s.children.push(v);
                    n.children.push(s);
                } else if self.eat_op("**") {
                    let l = self.cur_line();
                    let v = self.parse_expr()?;
                    let mut k = PyNode::new("keyword", l);
                    k.children.push(v);
                    n.children.push(k);
                } else if self.at_name() && self.peek2_is_op("=") {
                    let l = self.cur_line();
                    let kn = self.expect_name()?.0;
                    self.i += 1; // '='
                    let v = self.parse_expr()?;
                    let mut k = PyNode::with_name("keyword", l, kn);
                    k.children.push(v);
                    n.children.push(k);
                } else {
                    let e = self.parse_expr()?;
                    if self.at_kw("for") {
                        let gens = self.comp_tail()?;
                        let mut g = PyNode::new("GeneratorExp", e.line);
                        g.children.push(e);
                        g.children.extend(gens);
                        n.children.push(g);
                    } else {
                        n.children.push(e);
                    }
                }
                if !self.eat_op(",") {
                    break;
                }
                if self.at_op(")") {
                    break;
                }
            }
        }
        self.expect_op(")")?;
        Ok(n)
    }

    fn subscript(&mut self, value: PyNode) -> Result<PyNode, PyErr> {
        let l = value.line;
        self.i += 1; // '['
        let mut items: Vec<PyNode> = Vec::new();
        let mut tuple = false;
        loop {
            if self.at_op("]") {
                break;
            }
            items.push(self.parse_slice_item()?);
            if self.eat_op(",") {
                tuple = true;
                continue;
            }
            break;
        }
        self.expect_op("]")?;
        if items.is_empty() {
            return Err(PyErr { line: l, msg: "invalid syntax".into() });
        }
        let slice = if tuple || items.len() != 1 {
            let mut t = PyNode::new("Tuple", items.first().map(|n| n.line).unwrap_or(l));
            t.children = items;
            t
        } else {
            items.pop().unwrap()
        };
        let mut n = PyNode::new("Subscript", l);
        n.children.push(value);
        n.children.push(slice);
        Ok(n)
    }

    fn parse_slice_item(&mut self) -> Result<PyNode, PyErr> {
        let l = self.cur_line();
        let lower = if self.at_op(":") { None } else { Some(self.parse_expr()?) };
        if !self.at_op(":") {
            return Ok(lower.unwrap());
        }
        self.i += 1; // ':'
        let upper = if self.at_op(":") || self.at_op(",") || self.at_op("]") {
            None
        } else {
            Some(self.parse_expr()?)
        };
        let step = if self.eat_op(":") {
            if self.at_op(",") || self.at_op("]") {
                None
            } else {
                Some(self.parse_expr()?)
            }
        } else {
            None
        };
        let mut n = PyNode::new("Slice", l);
        // aux 位图记录实心字段（bit0=lower bit1=upper bit2=step）：dump 按 ASDL 字段名取
        if lower.is_some() {
            n.aux |= 1;
        }
        if upper.is_some() {
            n.aux |= 2;
        }
        if step.is_some() {
            n.aux |= 4;
        }
        n.children.extend(lower);
        n.children.extend(upper);
        n.children.extend(step);
        Ok(n)
    }

    fn atom(&mut self) -> Result<PyNode, PyErr> {
        let l = self.cur_line();
        let c = self.cur_col();
        match self.cur().kind.clone() {
            Tok::Name(nm) => {
                self.i += 1;
                Ok(PyNode::with_ctx("Name", l, nm, Ctx::Load))
            }
            Tok::Num(t) => {
                self.i += 1;
                let mut n = PyNode::new("Constant", l);
                n.col = c;
                n.cval = CVal::Num(t);
                Ok(n)
            }
            Tok::Str(_) | Tok::FStr(_) => self.strings(),
            Tok::Kw("True") | Tok::Kw("False") | Tok::Kw("None") => {
                let cval = match self.cur().kind {
                    Tok::Kw("True") => CVal::Bool(true),
                    Tok::Kw("False") => CVal::Bool(false),
                    _ => CVal::NoneC,
                };
                self.i += 1;
                let mut n = PyNode::new("Constant", l);
                n.col = c;
                n.cval = cval;
                Ok(n)
            }
            Tok::Kw("lambda") => self.lambda_expr(),
            Tok::Op(s) if s == "(" => self.paren_atom(),
            Tok::Op(s) if s == "[" => self.list_atom(),
            Tok::Op(s) if s == "{" => self.dictset_atom(),
            Tok::Op(s) if s == "..." => {
                self.i += 1;
                let mut n = PyNode::new("Constant", l);
                n.col = c;
                n.cval = CVal::EllipsisC;
                Ok(n)
            }
            _ => Err(self.err("invalid syntax")),
        }
    }

    /// 相邻字符串/ f-string token：全 Str → 单 Constant（值 = 解码值依序拼接，
    /// bytes 与 str 混排按 CPython 报错）；含 f → JoinedStr（各区域递归解析为
    /// FormattedValue；事件序与 ast 一致，字面量部分是叶子）。
    fn strings(&mut self) -> Result<PyNode, PyErr> {
        let l = self.cur_line();
        let c = self.cur_col();
        let mut parts: Vec<StrLit> = Vec::new();
        let mut fvals: Vec<PyNode> = Vec::new();
        loop {
            match self.cur().kind.clone() {
                Tok::Str(sv) => {
                    self.i += 1;
                    parts.push(sv);
                }
                Tok::FStr(regions) => {
                    self.i += 1;
                    for r in regions {
                        // 区域第 1 行基列 = '{' 列 + 1（CPython 3.12+ 真实行列）
                        let e = parse_region_expr(&r.src, r.line, r.col + 1)?;
                        let mut fv = PyNode::new("FormattedValue", r.line);
                        fv.children.push(e);
                        fvals.push(fv);
                    }
                }
                _ => break,
            }
        }
        if fvals.is_empty() {
            let any_b = parts.iter().any(|p| p.is_bytes);
            if any_b && parts.iter().any(|p| !p.is_bytes) {
                return Err(PyErr {
                    line: l,
                    msg: "cannot mix bytes and nonbytes literals".into(),
                });
            }
            let mut n = PyNode::new("Constant", l);
            n.col = c;
            n.cval = if any_b {
                let mut acc = Vec::new();
                for p in &parts {
                    acc.extend_from_slice(&p.b);
                }
                CVal::Bytes(acc)
            } else {
                CVal::Str(parts.into_iter().map(|p| p.s).collect())
            };
            Ok(n)
        } else {
            let n = PyNode::with_children("JoinedStr", l, fvals);
            Ok(n)
        }
    }

    fn paren_atom(&mut self) -> Result<PyNode, PyErr> {
        let l = self.cur_line();
        self.i += 1; // '('
        if self.at_op(")") {
            self.i += 1;
            return Ok(PyNode::new("Tuple", l));
        }
        if self.at_kw("yield") {
            let y = self.yield_expr()?;
            self.expect_op(")")?;
            return Ok(y);
        }
        let e = self.star_named()?;
        if self.at_kw("for") {
            let gens = self.comp_tail()?;
            self.expect_op(")")?;
            let mut n = PyNode::new("GeneratorExp", e.line);
            n.children.push(e);
            n.children.extend(gens);
            return Ok(n);
        }
        if self.at_op(",") {
            let mut elts = vec![e];
            while self.eat_op(",") {
                if self.at_op(")") {
                    break;
                }
                elts.push(self.star_named()?);
            }
            self.expect_op(")")?;
            let mut n = PyNode::new("Tuple", l);
            n.children = elts;
            return Ok(n);
        }
        self.expect_op(")")?;
        Ok(e)
    }

    fn list_atom(&mut self) -> Result<PyNode, PyErr> {
        let l = self.cur_line();
        self.i += 1; // '['
        if self.at_op("]") {
            self.i += 1;
            return Ok(PyNode::new("List", l));
        }
        let first = self.star_elt()?;
        if self.at_kw("for") {
            let gens = self.comp_tail()?;
            self.expect_op("]")?;
            let mut n = PyNode::new("ListComp", first.line);
            n.children.push(first);
            n.children.extend(gens);
            return Ok(n);
        }
        let mut elts = vec![first];
        while self.eat_op(",") {
            if self.at_op("]") {
                break;
            }
            elts.push(self.star_elt()?);
        }
        self.expect_op("]")?;
        let mut n = PyNode::new("List", l);
        n.children = elts;
        Ok(n)
    }

    fn star_elt(&mut self) -> Result<PyNode, PyErr> {
        if self.eat_op("*") {
            let l = self.cur_line();
            let v = self.parse_or()?;
            let mut s = PyNode::new("Starred", l);
            s.children.push(v);
            Ok(s)
        } else {
            self.parse_expr()
        }
    }

    /// 元组/集合显示元素：星号解包或带 walrus 的普通表达式
    fn star_named(&mut self) -> Result<PyNode, PyErr> {
        if self.at_op("*") {
            let l = self.cur_line();
            self.i += 1;
            let v = self.parse_or()?;
            let mut s = PyNode::new("Starred", l);
            s.children.push(v);
            Ok(s)
        } else {
            self.parse_namedexpr()
        }
    }

    fn dictset_atom(&mut self) -> Result<PyNode, PyErr> {
        let l = self.cur_line();
        self.i += 1; // '{'
        if self.at_op("}") {
            self.i += 1;
            return Ok(PyNode::new("Dict", l));
        }
        if self.eat_op("**") {
            // {**a, ...}：键位为 None（children 跳过），值照收
            let v = self.parse_or()?;
            let mut values = vec![v];
            while self.eat_op(",") {
                if self.at_op("}") {
                    break;
                }
                if self.eat_op("**") {
                    values.push(self.parse_or()?);
                } else {
                    let k = self.parse_expr()?;
                    self.expect_op(":")?;
                    values.push(self.parse_expr()?);
                    let _ = k;
                }
            }
            self.expect_op("}")?;
            let mut n = PyNode::new("Dict", l);
            n.children = values; // ** 起首时 keys 全空，children 即 values 序
            return Ok(n);
        }
        let e = self.star_named()?;
        if self.at_op(":") {
            self.i += 1;
            let v = self.parse_expr()?;
            if self.at_kw("for") {
                let gens = self.comp_tail()?;
                self.expect_op("}")?;
                let mut n = PyNode::new("DictComp", l);
                n.children.push(e);
                n.children.push(v);
                n.children.extend(gens);
                return Ok(n);
            }
            let mut keys = vec![e];
            let mut values = vec![v];
            while self.eat_op(",") {
                if self.at_op("}") {
                    break;
                }
                if self.eat_op("**") {
                    values.push(self.parse_or()?);
                } else {
                    keys.push(self.parse_expr()?);
                    self.expect_op(":")?;
                    values.push(self.parse_expr()?);
                }
            }
            self.expect_op("}")?;
            let mut n = PyNode::new("Dict", l);
            n.aux = keys.len(); // keys 数：children = keys ++ values，dump 据此切分
            n.children = keys; // ASDL 字段序：先 keys 后 values
            n.children.extend(values);
            return Ok(n);
        }
        if self.at_kw("for") {
            let gens = self.comp_tail()?;
            self.expect_op("}")?;
            let mut n = PyNode::new("SetComp", l);
            n.children.push(e);
            n.children.extend(gens);
            return Ok(n);
        }
        let mut elts = vec![e];
        while self.eat_op(",") {
            if self.at_op("}") {
                break;
            }
            elts.push(self.star_named()?);
        }
        self.expect_op("}")?;
        let mut n = PyNode::new("Set", l);
        n.children = elts;
        Ok(n)
    }

    /// 推导式尾部：进入时 peek 是 'for'。comprehension children = [target, iter, ifs..]。
    fn comp_tail(&mut self) -> Result<Vec<PyNode>, PyErr> {
        let mut gens = Vec::new();
        loop {
            self.expect_kw("for")?;
            let target = self.parse_target_list()?;
            self.expect_kw("in")?;
            let iter = self.parse_or()?;
            let mut g = PyNode::new("comprehension", target.line);
            g.children.push(target);
            g.children.push(iter);
            while self.eat_kw("if") {
                g.children.push(self.parse_or()?);
            }
            gens.push(g);
            if !self.at_kw("for") {
                break;
            }
        }
        Ok(gens)
    }

    /// for/推导式目标：or 层解析（含逗号元组）后标 Store。
    /// for/推导式目标：star_targets 文法（Name/Attribute/Subscript/Tuple/List/Starred），
    /// 不能用完整表达式入口——`for i in r` 的 in 是比较运算符，会被比较级吃掉
    fn parse_target_elem(&mut self) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        if self.eat_op("*") {
            let child = self.parse_target_elem()?;
            let mut n = PyNode::new("Starred", line);
            n.children.push(child);
            return Ok(n);
        }
        self.parse_factor()
    }

    fn parse_target_list(&mut self) -> Result<PyNode, PyErr> {
        let e = self.parse_target_elem()?;
        if !self.at_op(",") {
            return self.mark_target(e, true);
        }
        let mut elts = vec![e];
        while self.eat_op(",") {
            if self.at_kw("in") || self.at_op(":") || self.at_op(")") {
                break;
            }
            elts.push(self.parse_target_elem()?);
        }
        let l = elts[0].line;
        let t = PyNode::with_children("Tuple", l, elts);
        self.mark_target(t, true)
    }

    /// with-as / except-as 目标
    fn parse_target(&mut self) -> Result<PyNode, PyErr> {
        let e = self.parse_target_elem()?;
        self.mark_target(e, true)
    }

    fn peek2_is_op(&self, s: &str) -> bool {
        matches!(&self.t.get(self.i + 1).map(|t| &t.kind), Some(Tok::Op(x)) if x == s)
    }

    fn peek2_is_kw(&self, s: &str) -> bool {
        matches!(&self.t.get(self.i + 1).map(|t| &t.kind), Some(Tok::Kw(k)) if *k == s)
    }
}

impl PyNode {
    fn with_children(kind: &'static str, line: usize, children: Vec<PyNode>) -> PyNode {
        let mut n = PyNode::new(kind, line);
        n.children = children;
        n
    }

    fn with_ctx(kind: &'static str, line: usize, name: String, ctx: Ctx) -> PyNode {
        let mut n = PyNode::with_name(kind, line, name);
        n.ctx = ctx;
        n
    }
}

/// f-string 区域表达式：允许顶层元组与 walrus（3.12+ f"{x := 1}"）；行号/列号映射
/// 回原文——区域内第 1 行的 col 加 base_col（'{' 列 + 1），第 2 行起 line 偏移、
/// col 从行首重计（= 物理列，多行区域实测）。
fn parse_region_expr(src: &str, base_line: usize, base_col: usize) -> Result<PyNode, PyErr> {
    let toks = match tokenize(src) {
        Ok(t) => t,
        Err(e) => return Err(PyErr { line: e.line + base_line - 1, msg: e.msg }),
    };
    // 区域等价于括号内上下文：行结构 token（NL/INDENT/DEDENT）全部滤除，
    // 多行表达式（PEP 701）与尾随换行都因此自然成立
    let toks: Vec<TokOut> = toks
        .into_iter()
        .map(|mut t| {
            if t.line > 1 {
                t.line += base_line - 1;
            } else {
                t.line = base_line;
                t.col += base_col;
            }
            t
        })
        .filter(|t| !matches!(t.kind, Tok::Newline | Tok::Indent | Tok::Dedent))
        .collect();
    let mut p = Parser { t: toks, i: 0 };
    let e = if p.at_name() && p.peek2_is_op(":=") {
        p.parse_namedexpr()?
    } else {
        p.parse_testlist_star()?
    };
    if !p.at_end() {
        return Err(p.err("invalid syntax"));
    }
    Ok(e)
}

/// 词法算子文本 → ast 算子名（S84：BinOp/Compare 的 name/names 记原名供 dump）。
fn ast_op_name(op: &str) -> &'static str {
    match op {
        "|" => "BitOr",
        "^" => "BitXor",
        "&" => "BitAnd",
        "<<" => "LShift",
        ">>" => "RShift",
        "+" => "Add",
        "-" => "Sub",
        "*" => "Mult",
        "/" => "Div",
        "//" => "FloorDiv",
        "%" => "Mod",
        "@" => "MatMult",
        "**" => "Pow",
        "==" => "Eq",
        "!=" => "NotEq",
        "<" => "Lt",
        "<=" => "LtE",
        ">" => "Gt",
        ">=" => "GtE",
        _ => "Add",
    }
}

/// 解析整个模块（bug_scan 入口）。
pub fn parse_module(src: &str) -> Result<PyNode, PyErr> {
    let toks = tokenize(src)?;
    let mut p = Parser { t: toks, i: 0 };
    p.run_module()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 收集一行源码里 f-string 的全部区域 (line, src)，按扁平化顺序。
    fn fstr_regions(src: &str) -> Vec<(usize, String)> {
        let toks = tokenize(src).expect("tokenize");
        let mut out = Vec::new();
        for t in &toks {
            if let Tok::FStr(rs) = &t.kind {
                for r in rs {
                    out.push((r.line, r.src.clone()));
                }
            }
        }
        out
    }

    #[test]
    fn fstring_conversion_and_debug_eq_are_cut_from_expr() {
        // !r 等转换符与调试 '=' 必须真截断（区域是源切片，跳过字节无效）
        let rs = fstr_regions("s = f\"{name!r} is {len(name):>{width}}\"\n");
        assert_eq!(
            rs,
            vec![
                (1, "name".to_string()),
                (1, "len(name)".to_string()),
                (1, "width".to_string()),
            ]
        );
        let dbg = fstr_regions("s = f\"{name=}\"\n");
        assert_eq!(dbg, vec![(1, "name".to_string())]);
    }

    #[test]
    fn fstring_bracket_colon_is_not_spec() {
        // 切片/下标/lambda 的 ':' 在括号内（sq>0）不得触发 format-spec 臂
        parse_module("raise ValueError(f\"bad: {lines[-1][:200]}\")\n")
            .expect("slice colon inside f-string");
        parse_module("g = f\"{(lambda x: x)(1)}\"\n").expect("lambda colon inside f-string");
        // 顶层冒号仍是 spec 起点
        let rs = fstr_regions("s = f\"{val:>8}\"\n");
        assert_eq!(rs, vec![(1, "val".to_string())]);
    }

    #[test]
    fn match_soft_keyword_dispatches_and_falls_back() {
        parse_module(concat!(
            "def h(cmd, v):\n",
            "    match cmd:\n",
            "        case [1, 2, rest]:\n",
            "            return rest\n",
            "        case {\"k\": vv}:\n",
            "            return vv\n",
            "        case Point(x=px):\n",
            "            return px\n",
            "        case _:\n",
            "            return v\n",
            "    match = 1\n",
            "    return match\n",
        ))
        .expect("match 语句与软关键字回退（match 作标识符）共存");
    }

    #[test]
    fn del_starred_is_syntax_error_like_cpython() {
        match parse_module("del *a\n") {
            Ok(_) => panic!("del *a 应为语法错误（CPython: cannot delete starred）"),
            Err(e) => assert_eq!(e.line, 1),
        }
    }

    /// S84：col_offset 口径（探针实测 CPython）——Call 列 = 链首 token（含前置
    /// 括号）；字符串 Constant 列含前缀；括号对原子透明。
    #[test]
    fn cols_match_cpython_probes() {
        let got = |src: &str, kind: &str| -> usize {
            let tree = parse_module(src).expect("parse");
            let mut out = Vec::new();
            fn go(n: &PyNode, kind: &str, out: &mut Vec<usize>) {
                if n.kind == kind {
                    out.push(n.col);
                }
                for c in &n.children {
                    go(c, kind, out);
                }
            }
            go(&tree, kind, &mut out);
            *out.last().expect("node found")
        };
        // (a+b)(x)：Call 列在 '('，内层 BinOp 不参与
        assert_eq!(got("y = (a+b)(x)\n", "Call"), 4);
        // 嵌套调用两层 Call 同列（都挂在链首 f）
        assert_eq!(got("y = f(a)(b)\n", "Call"), 4);
        // 下标链：Call 列在链首 d
        assert_eq!(got("d['k'].system(x)\n", "Call"), 0);
        // 括号对字符串透明：列落在引号上
        assert_eq!(got("x = (\"ab\")\n", "Constant"), 5);
        // 前缀计入列：rb 的 r（0 基第 4 列）
        assert_eq!(got("x = rb'ab'\n", "Constant"), 4);
    }

    /// S84：字符串值解码——转义还原、raw 保形、bytes 收集、隐式拼接折叠。
    #[test]
    fn string_values_decode_like_python() {
        let val = |src: &str| -> CVal {
            let tree = parse_module(src).expect("parse");
            let mut out = Vec::new();
            fn go(n: &PyNode, out: &mut Vec<CVal>) {
                if n.kind == "Constant" {
                    out.push(n.cval.clone());
                }
                for c in &n.children {
                    go(c, out);
                }
            }
            go(&tree, &mut out);
            out.pop().expect("constant")
        };
        assert_eq!(val("x = 'a\\n\\t\\x41\\101\\\\'\n"), CVal::Str("a\n\tAA\\".into()));
        assert_eq!(val("x = r'a\\n'\n"), CVal::Str("a\\n".into()));
        assert_eq!(val("x = b'ab'\n"), CVal::Bytes(vec![b'a', b'b']));
        // 未知转义保形
        assert_eq!(val("x = '\\q'\n"), CVal::Str("\\q".into()));
        // 隐式拼接
        assert_eq!(val("x = 'ab' 'cd'\n"), CVal::Str("abcd".into()));
        // bytes 与 str 混排：CPython 同款报错
        assert!(parse_module("x = b'a' 'b'\n").is_err());
        // 数字原文入 cval
        assert_eq!(val("x = 0x1F\n"), CVal::Num("0x1F".into()));
    }

    /// S84：算子名入节点——dump_expr 依赖 BinOp.name / Compare.names / BoolOp.name。
    #[test]
    fn operator_names_recorded() {
        let tree = parse_module("r = a // 2 if x < 1 and not y else None\n").expect("parse");
        let mut bins = Vec::new();
        let mut cmps = Vec::new();
        let mut bools = Vec::new();
        fn go(n: &PyNode, b: &mut Vec<String>, c: &mut Vec<Vec<String>>, d: &mut Vec<String>) {
            match n.kind {
                "BinOp" => b.push(n.name.clone()),
                "Compare" => c.push(n.names.clone()),
                "BoolOp" => d.push(n.name.clone()),
                _ => {}
            }
            for ch in &n.children {
                go(ch, b, c, d);
            }
        }
        go(&tree, &mut bins, &mut cmps, &mut bools);
        assert_eq!(bins, vec!["FloorDiv"]);
        assert_eq!(cmps, vec![vec!["Lt"]]);
        assert_eq!(bools, vec!["And"]);
    }

    #[test]
    fn multiline_bracket_expressions_keep_line_flow() {
        parse_module("x = ([1] +\n     [2])\nprint(x)\n").expect("括号续行");
        parse_module("def f():\n    d = {\n        \"a\": 1}\n    print(d)\n").expect("字典续行");
        parse_module(
            "def m(pairs, dd):\n    for i, (tag, src) in enumerate([(\"prog\", pairs)] +\n\
             ([(\"data\", dd)] if dd else [])):\n        res = run_one(tag, src)\n    print(1)\n",
        )
        .expect("for 目标元组解包 + 续行");
    }
}
