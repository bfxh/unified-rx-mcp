//! pyast 语法域·简单语句段（S167 从 parse.rs 拆出；纯搬移，未改语义）。
use super::*;
use super::parse::*;   // S167：parse.rs 里的自由函数/常量（AUG_OPS / ast_op_name / parse_region_expr）

impl Parser {
    /// 值模式/映射键的字面量：-/+ 一元、字面量 atom、a.b 点链（Load 语境）
    pub(crate) fn parse_value_expr(&mut self) -> Result<PyNode, PyErr> {
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
    pub(crate) fn parse_dotted_name(&mut self) -> Result<PyNode, PyErr> {
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
    pub(crate) fn class_pattern_with(&mut self, cls: PyNode) -> Result<PyNode, PyErr> {
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

    pub(crate) fn simple_line(&mut self) -> Result<Vec<PyNode>, PyErr> {
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

    pub(crate) fn small_stmt(&mut self) -> Result<PyNode, PyErr> {
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
            Tok::Kw("from") => self.parse_from_import(),
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

    pub(crate) fn import_stmt(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_from_import(&mut self) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        self.i += 1;
        // S108：记录相对导入层级（前导点数）到 aux——dump/ast_scan 不消费 aux，
        // 不影响 S83/S84 oracle；nameres 跨文件解析用。
        let mut level = 0usize;
        while self.eat_op(".") {
            level += 1;
        }
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
        n.aux = level;
        n.children = aliases;
        Ok(n)
    }

    pub(crate) fn import_alias(&mut self, line: usize) -> Result<PyNode, PyErr> {
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

    pub(crate) fn del_stmt(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn mark_del(&self, node: &mut PyNode) -> Result<(), PyErr> {
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

    pub(crate) fn expr_stmt(&mut self) -> Result<PyNode, PyErr> {
        let e1 = self.parse_testlist_star()?;
        let aug = matches!(&self.cur().kind, Tok::Op(s) if AUG_OPS.contains(&s.as_str()));
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
    pub(crate) fn mark_target(&self, node: PyNode, tuple_ok: bool) -> Result<PyNode, PyErr> {
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

    pub(crate) fn yield_expr(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_namedexpr(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_testlist_star(&mut self) -> Result<PyNode, PyErr> {
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

}
