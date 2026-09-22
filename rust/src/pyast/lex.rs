//! pyast 词法域（S167 从 pyast.rs 拆出；纯搬移，未改语义）。
use super::*;

pub(crate) struct Lexer<'a> {
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
    pub(crate) fn peek(&self) -> Option<u8> {
        self.b.get(self.pos).copied()
    }

    pub(crate) fn peek_at(&self, k: usize) -> Option<u8> {
        self.b.get(self.pos + k).copied()
    }

    pub(crate) fn push(&mut self, kind: Tok) {
        let col = self.col_at(self.tok_start);
        self.line_open = true;
        self.out.push(TokOut { kind, line: self.line, col });
    }

    /// tok_start 到行首的字符数（0 基列号）。按字节回扫、跳过 UTF-8 续字节，
    /// 与 CPython 在 decode 后文本上的字符列一致（\n 恒在字符边界）。
    pub(crate) fn col_at(&self, start: usize) -> usize {
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

    pub(crate) fn run(mut self) -> Result<Vec<TokOut>, PyErr> {
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

    pub(crate) fn lex_name(&mut self) -> Result<(), PyErr> {
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

    pub(crate) fn lex_number(&mut self) {
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

    pub(crate) fn lex_string(&mut self, prefix: String, lit_start: usize) -> Result<(), PyErr> {
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
    pub(crate) fn decode_str_lit(lit: &str, prefix: &str) -> Result<StrLit, String> {
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
    pub(crate) fn lex_fstring(&mut self, triple: bool, q: u8, open_line: usize) -> Result<(), PyErr> {
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
    pub(crate) fn fstring_region(&mut self) -> Result<(FRegion, Vec<FRegion>), PyErr> {
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
                    sq = sq.saturating_sub(1);
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
    pub(crate) fn format_spec(&mut self, out: &mut Vec<FRegion>) -> Result<(), PyErr> {
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
    pub(crate) fn region_string(&mut self) -> Result<(), PyErr> {
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

    pub(crate) fn lex_op(&mut self) -> Result<(), PyErr> {
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
            Some(")") | Some("]") | Some("}")
                if self.opens.pop().is_none() => {
                    let ch = self.out.last().unwrap().kind.text_or_empty();
                    return Err(PyErr { line: self.line, msg: format!("unmatched '{}'", ch) });
                }
            _ => {}
        }
        Ok(())
    }
}

impl Tok {
    pub(crate) fn text_or_empty(&self) -> &str {
        match self {
            Tok::Op(s) => s.as_str(),
            _ => "",
        }
    }
}

pub(crate) fn tokenize(src: &str) -> Result<Vec<TokOut>, PyErr> {
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

