//! pyast 语法域·match 模式段（S167 从 parse.rs 拆出；纯搬移，未改语义）。
use super::*;
use super::parse::*;   // S167：parse.rs 里的自由函数/常量（AUG_OPS / ast_op_name / parse_region_expr）

impl Parser {
    /// 软关键字 match 语句：宽松解析（捕获名标 Store；class-pattern 成员名 Load）。
    pub(crate) fn match_stmt(&mut self) -> Result<PyNode, PyErr> {
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
    pub(crate) fn parse_case_pattern(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_as_pattern(&mut self) -> Result<PyNode, PyErr> {
        let p = self.parse_or_pattern()?;
        if self.eat_kw("as") {
            let (nm, l) = self.expect_name()?;
            let mut a = PyNode::with_name("MatchAs", l, nm);
            a.children.push(p);
            return Ok(a);
        }
        Ok(p)
    }

    pub(crate) fn parse_or_pattern(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_closed_pattern(&mut self) -> Result<PyNode, PyErr> {
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

}
