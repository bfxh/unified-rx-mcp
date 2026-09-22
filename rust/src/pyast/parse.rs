//! pyast 语法域（S167 从 pyast.rs 拆出；纯搬移，未改语义）。
use super::*;
use super::lex::*;

pub(crate) struct Parser {
    pub(crate) t: Vec<TokOut>,
    pub(crate) i: usize,
}

pub(crate) const AUG_OPS: &[&str] = &[
    "+=", "-=", "*=", "/=", "//=", "%=", "**=", ">>=", "<<=", "&=", "|=", "^=", "@=",
];

impl Parser {
    pub(crate) fn cur(&self) -> &TokOut {
        &self.t[self.i.min(self.t.len() - 1)]
    }

    pub(crate) fn cur_line(&self) -> usize {
        self.cur().line
    }

    pub(crate) fn cur_col(&self) -> usize {
        self.t[self.i].col
    }

    pub(crate) fn err(&self, msg: &str) -> PyErr {
        PyErr { line: self.cur_line(), msg: msg.into() }
    }

    pub(crate) fn at_op(&self, s: &str) -> bool {
        matches!(&self.cur().kind, Tok::Op(x) if x == s)
    }

    pub(crate) fn at_kw(&self, s: &str) -> bool {
        matches!(&self.cur().kind, Tok::Kw(k) if *k == s)
    }

    pub(crate) fn at_name(&self) -> bool {
        matches!(self.cur().kind, Tok::Name(_))
    }

    pub(crate) fn at_end(&self) -> bool {
        matches!(self.cur().kind, Tok::End)
    }

    /// 语句边界判断（simple_stmt 循环与尾逗号终止用）
    pub(crate) fn at_stmt_end(&self) -> bool {
        matches!(
            self.cur().kind,
            Tok::Newline | Tok::End | Tok::Dedent
        ) || self.at_op(")")
            || self.at_op("]")
            || self.at_op("}")
    }

    pub(crate) fn eat_op(&mut self, s: &str) -> bool {
        if self.at_op(s) {
            self.i += 1;
            true
        } else {
            false
        }
    }

    pub(crate) fn eat_kw(&mut self, s: &str) -> bool {
        if self.at_kw(s) {
            self.i += 1;
            true
        } else {
            false
        }
    }

    pub(crate) fn expect_op(&mut self, s: &str) -> Result<(), PyErr> {
        if self.eat_op(s) {
            Ok(())
        } else {
            Err(self.err("invalid syntax"))
        }
    }

    pub(crate) fn expect_kw(&mut self, s: &str) -> Result<(), PyErr> {
        if self.eat_kw(s) {
            Ok(())
        } else {
            Err(self.err("invalid syntax"))
        }
    }

    pub(crate) fn expect_newline(&mut self) -> Result<(), PyErr> {
        if matches!(self.cur().kind, Tok::Newline) {
            self.i += 1;
            Ok(())
        } else {
            Err(self.err("invalid syntax"))
        }
    }

    pub(crate) fn expect_name(&mut self) -> Result<(String, usize), PyErr> {
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

    pub(crate) fn run_module(&mut self) -> Result<PyNode, PyErr> {
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
    pub(crate) fn block(&mut self, desc: &str, stmt_line: usize) -> Result<Vec<PyNode>, PyErr> {
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

    pub(crate) fn parse_statement(&mut self) -> Result<Vec<PyNode>, PyErr> {
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

    pub(crate) fn if_like(&mut self, kw: &'static str) -> Result<PyNode, PyErr> {
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

    pub(crate) fn else_tail(&mut self) -> Result<Vec<PyNode>, PyErr> {
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

    pub(crate) fn while_stmt(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn for_stmt(&mut self, is_async: bool) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.expect_kw("for")?;
        self.for_body(line, is_async)
    }

    pub(crate) fn for_body(&mut self, line: usize, is_async: bool) -> Result<PyNode, PyErr> {
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

    pub(crate) fn try_stmt(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn with_body(&mut self, line: usize, is_async: bool) -> Result<PyNode, PyErr> {
        let items = self.with_items()?;
        let body = self.block("'with' statement", line)?;
        let mut n = PyNode::new(if is_async { "AsyncWith" } else { "With" }, line);
        n.children.extend(items);
        n.children.extend(body);
        Ok(n)
    }

    pub(crate) fn with_items(&mut self) -> Result<Vec<PyNode>, PyErr> {
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

    pub(crate) fn try_paren_with_items(&mut self) -> Result<Vec<PyNode>, PyErr> {
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

    pub(crate) fn funcdef(&mut self, line: usize, is_async: bool, decs: Vec<PyNode>) -> Result<PyNode, PyErr> {
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
        n.deco = decs.len();
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
    pub(crate) fn params(&mut self, annot: bool) -> Result<PyNode, PyErr> {
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

    pub(crate) fn arg_node(&mut self, annot: bool, kind: &'static str) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        let name = self.expect_name()?.0;
        let mut n = PyNode::with_name(kind, line, name);
        if annot && self.eat_op(":") {
            n.children.push(self.parse_expr()?);
        }
        Ok(n)
    }

    pub(crate) fn classdef(&mut self, decs: Vec<PyNode>) -> Result<PyNode, PyErr> {
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
        n.deco = decs.len();
        n.children.extend(bases);
        n.children.extend(kws);
        n.children.extend(body);
        n.children.extend(decs);
        Ok(n)
    }

}

impl PyNode {
    pub(crate) fn with_children(kind: &'static str, line: usize, children: Vec<PyNode>) -> PyNode {
        let mut n = PyNode::new(kind, line);
        n.children = children;
        n
    }

    pub(crate) fn with_ctx(kind: &'static str, line: usize, name: String, ctx: Ctx) -> PyNode {
        let mut n = PyNode::with_name(kind, line, name);
        n.ctx = ctx;
        n
    }
}

/// f-string 区域表达式：允许顶层元组与 walrus（3.12+ f"{x := 1}"）；行号/列号映射
/// 回原文——区域内第 1 行的 col 加 base_col（'{' 列 + 1），第 2 行起 line 偏移、
/// col 从行首重计（= 物理列，多行区域实测）。
pub(crate) fn parse_region_expr(src: &str, base_line: usize, base_col: usize) -> Result<PyNode, PyErr> {
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
pub(crate) fn ast_op_name(op: &str) -> &'static str {
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

