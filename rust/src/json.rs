//! rx-json —— 手写零依赖 JSON（S78）。
//!
//! 为什么手写：迁移红线禁止第三方 crate；JSON-RPC stdio 是整个 Rust 化的地基，
//! 解析器必须对敌意输入免疫——深度限 512 防栈溢出，数字/字符串/转义严格按 RFC 8259，
//! 唯一放宽：`\u` 代理对不配对时落 U+FFFD（Python json 允许孤代理，Rust String 装不下）。
//!
//! 序列化保序（Obj 用 Vec<(String, Value)>），id 的整型/浮点/字符串类型往返保真——
//! JSON-RPC 宿主靠 id 配对响应，类型不能变。

use std::fmt;

/// 解析错误：带字节偏移，便于 fuzz 报告定位。
#[derive(Clone, Debug, PartialEq)]
pub struct ParseError {
    pub pos: usize,
    pub msg: String,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} (字节 {})", self.msg, self.pos)
    }
}

/// 嵌套深度上限：超过即报错而不是栈溢出。fuzz 电池的 1 万层嵌套会打在这里。
const MAX_DEPTH: usize = 512;

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i128),
    /// i64 装不下的整型或带小数/指数的数。
    Float(f64),
    Str(String),
    Arr(Vec<Value>),
    /// 保序键值对；重复键查找取首个（与写入序一致）。
    Obj(Vec<(String, Value)>),
}

impl Value {
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Obj(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn type_name(&self) -> &'static str {
        match self {
            Value::Null => "null",
            Value::Bool(_) => "bool",
            Value::Int(_) | Value::Float(_) => "number",
            Value::Str(_) => "string",
            Value::Arr(_) => "array",
            Value::Obj(_) => "object",
        }
    }

    /// 紧凑序列化。浮点非有限（NaN/Inf）按 JSON 规范落 null。
    pub fn to_json(&self) -> String {
        let mut s = String::with_capacity(64);
        self.write(&mut s);
        s
    }

    fn write(&self, out: &mut String) {
        match self {
            Value::Null => out.push_str("null"),
            Value::Bool(true) => out.push_str("true"),
            Value::Bool(false) => out.push_str("false"),
            Value::Int(i) => out.push_str(&i.to_string()),
            Value::Float(f) => {
                if f.is_finite() {
                    // Rust 的 {} 对 f64 输出最短往返表示（含 1.0 → "1"，合法 JSON）
                    out.push_str(&format!("{}", f));
                } else {
                    out.push_str("null");
                }
            }
            Value::Str(s) => write_escaped(s, out),
            Value::Arr(items) => {
                out.push('[');
                for (i, v) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    v.write(out);
                }
                out.push(']');
            }
            Value::Obj(pairs) => {
                out.push('{');
                for (i, (k, v)) in pairs.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_escaped(k, out);
                    out.push(':');
                    v.write(out);
                }
                out.push('}');
            }
        }
    }
}

fn write_escaped(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c), // 其余（含中文/emoji）原样 UTF-8 输出
        }
    }
    out.push('"');
}

/// 严格解析一整个 JSON 值：尾部只允许空白（stdio 场景一行一值）。
pub fn parse(s: &str) -> Result<Value, ParseError> {
    let mut p = Parser { b: s.as_bytes(), pos: 0, depth: 0 };
    p.skip_ws();
    let v = p.value()?;
    p.skip_ws();
    if p.pos != p.b.len() {
        return Err(p.err("尾随内容"));
    }
    Ok(v)
}

struct Parser<'a> {
    b: &'a [u8],
    pos: usize,
    depth: usize,
}

impl<'a> Parser<'a> {
    fn err(&self, msg: &str) -> ParseError {
        ParseError { pos: self.pos, msg: msg.to_string() }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn lit(&mut self, word: &str, v: Value) -> Result<Value, ParseError> {
        if self.b[self.pos..].starts_with(word.as_bytes()) {
            self.pos += word.len();
            Ok(v)
        } else {
            Err(self.err("字面量不完整"))
        }
    }

    fn value(&mut self) -> Result<Value, ParseError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(self.err("嵌套过深"));
        }
        let r = match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Value::Str(self.string()?)),
            Some(b't') => self.lit("true", Value::Bool(true)),
            Some(b'f') => self.lit("false", Value::Bool(false)),
            Some(b'n') => self.lit("null", Value::Null),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => Err(self.err("意外字符")),
        };
        self.depth -= 1;
        r
    }

    fn object(&mut self) -> Result<Value, ParseError> {
        self.pos += 1; // '{'
        let mut pairs = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Value::Obj(pairs));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(self.err("对象键必须是字符串"));
            }
            let k = self.string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(self.err("缺少 ':'"));
            }
            self.pos += 1;
            self.skip_ws();
            let v = self.value()?;
            pairs.push((k, v));
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(Value::Obj(pairs));
                }
                _ => return Err(self.err("对象缺少 ',' 或 '}'")),
            }
        }
    }

    fn array(&mut self) -> Result<Value, ParseError> {
        self.pos += 1; // '['
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Value::Arr(items));
        }
        loop {
            self.skip_ws();
            items.push(self.value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b']') => {
                    self.pos += 1;
                    return Ok(Value::Arr(items));
                }
                _ => return Err(self.err("数组缺少 ',' 或 ']'")),
            }
        }
    }

    /// 字符串字面量：输出保证是合法 UTF-8（孤代理落 U+FFFD）。
    fn string(&mut self) -> Result<String, ParseError> {
        self.pos += 1; // 开引号
        let mut out: Vec<u8> = Vec::new();
        loop {
            let c = match self.peek() {
                Some(c) => c,
                None => return Err(self.err("字符串未闭合")),
            };
            self.pos += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    let e = self.peek().ok_or_else(|| self.err("转义未闭合"))?;
                    self.pos += 1;
                    match e {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(0x08),
                        b'f' => out.push(0x0c),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let cp = self.hex4()?;
                            let ch = if (0xD800..0xDC00).contains(&cp) {
                                // 高代理：必须紧跟 \uDC00-\uDFFF 才能配对
                                if self.b[self.pos..].starts_with(b"\\u") {
                                    let save = self.pos;
                                    self.pos += 2;
                                    let lo = self.hex4()?;
                                    if (0xDC00..0xE000).contains(&lo) {
                                        let combined = 0x10000
                                            + ((cp - 0xD800) << 10)
                                            + (lo - 0xDC00);
                                        char::from_u32(combined).unwrap_or('\u{FFFD}')
                                    } else {
                                        self.pos = save;
                                        '\u{FFFD}'
                                    }
                                } else {
                                    '\u{FFFD}'
                                }
                            } else if (0xDC00..0xE000).contains(&cp) {
                                '\u{FFFD}' // 孤低代理
                            } else {
                                char::from_u32(cp).unwrap_or('\u{FFFD}')
                            };
                            let mut buf = [0u8; 4];
                            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                        }
                        _ => return Err(self.err("非法转义")),
                    }
                }
                c if c < 0x20 => return Err(self.err("字符串内裸控制字符")),
                c => out.push(c), // 输入本就是合法 UTF-8，逐字节复制保持边界
            }
        }
        String::from_utf8(out).map_err(|_| self.err("内部 UTF-8 错误"))
    }

    fn hex4(&mut self) -> Result<u32, ParseError> {
        if self.pos + 4 > self.b.len() {
            return Err(self.err("\\u 转义不完整"));
        }
        let s = std::str::from_utf8(&self.b[self.pos..self.pos + 4])
            .map_err(|_| self.err("\\u 非法"))?;
        let v = u32::from_str_radix(s, 16).map_err(|_| self.err("\\u 非十六进制"))?;
        self.pos += 4;
        Ok(v)
    }

    /// 严格数字文法：-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?
    /// 无小数/指数且 i64 装得下 → Int；否则 Float（含前导 0、01 这类非法文法直接报错）。
    fn number(&mut self) -> Result<Value, ParseError> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek() {
            Some(b'0') => {
                self.pos += 1;
                if matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                    return Err(self.err("前导零"));
                }
            }
            Some(c) if c.is_ascii_digit() => {
                while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                    self.pos += 1;
                }
            }
            _ => return Err(self.err("数字文法错误")),
        }
        let mut is_float = false;
        if self.peek() == Some(b'.') {
            is_float = true;
            self.pos += 1;
            if !matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                return Err(self.err("小数点后必须有数字"));
            }
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            is_float = true;
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if !matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                return Err(self.err("指数后必须有数字"));
            }
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.pos += 1;
            }
        }
        let text = std::str::from_utf8(&self.b[start..self.pos]).unwrap_or("");
        if !is_float {
            // i128 保真：JSON-RPC id 配对要求原样回显，i64 溢出转 f64 会丢精度
            // （S78 首测实锤 2**70 回显成 1.18e21）
            if let Ok(i) = text.parse::<i128>() {
                return Ok(Value::Int(i));
            }
        }
        text.parse::<f64>()
            .map(Value::Float)
            .map_err(|_| self.err("数字溢出"))
    }
}
