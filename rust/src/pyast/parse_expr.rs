//! pyast 语法域·表达式优先级段（S167 从 parse.rs 拆出；纯搬移，未改语义）。
use super::*;
use super::parse::*;   // S167：parse.rs 里的自由函数/常量（AUG_OPS / ast_op_name / parse_region_expr）

impl Parser {
    pub(crate) fn parse_expr(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn lambda_expr(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_or(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_and(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_not(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_comparison(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_bitor(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_bitxor, &["|"])
    }

    pub(crate) fn parse_bitxor(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_bitand, &["^"])
    }

    pub(crate) fn parse_bitand(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_shift, &["&"])
    }

    pub(crate) fn parse_shift(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_arith, &["<<", ">>"])
    }

    /// 左结合二元链：children 按 [左, 右, 右, …] 嵌套（= ast 的 BinOp 树形）；
    /// name 记 ast 算子名（S84：dump_expr 需要 Add/FloorDiv 等原名）
    pub(crate) fn binop_chain(
        &mut self,
        sub: fn(&mut Parser) -> Result<PyNode, PyErr>,
        ops: &[&str],
    ) -> Result<PyNode, PyErr> {
        let mut node = sub(self)?;
        while let Some(op) = ops.iter().find(|op| self.at_op(op)) {
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

    pub(crate) fn parse_arith(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_term, &["+", "-"])
    }

    pub(crate) fn parse_term(&mut self) -> Result<PyNode, PyErr> {
        self.binop_chain(Self::parse_factor, &["*", "/", "//", "%", "@"])
    }

    pub(crate) fn parse_factor(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_power(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_postfix(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn call_args(&mut self, func: PyNode) -> Result<PyNode, PyErr> {
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

    pub(crate) fn subscript(&mut self, value: PyNode) -> Result<PyNode, PyErr> {
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

    pub(crate) fn parse_slice_item(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn atom(&mut self) -> Result<PyNode, PyErr> {
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
    pub(crate) fn strings(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn paren_atom(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn list_atom(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn star_elt(&mut self) -> Result<PyNode, PyErr> {
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
    pub(crate) fn star_named(&mut self) -> Result<PyNode, PyErr> {
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

    pub(crate) fn dictset_atom(&mut self) -> Result<PyNode, PyErr> {
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
    pub(crate) fn comp_tail(&mut self) -> Result<Vec<PyNode>, PyErr> {
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
    pub(crate) fn parse_target_elem(&mut self) -> Result<PyNode, PyErr> {
        let line = self.cur_line();
        if self.eat_op("*") {
            let child = self.parse_target_elem()?;
            let mut n = PyNode::new("Starred", line);
            n.children.push(child);
            return Ok(n);
        }
        self.parse_factor()
    }

    pub(crate) fn parse_target_list(&mut self) -> Result<PyNode, PyErr> {
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
    pub(crate) fn parse_target(&mut self) -> Result<PyNode, PyErr> {
        let e = self.parse_target_elem()?;
        self.mark_target(e, true)
    }

    pub(crate) fn peek2_is_op(&self, s: &str) -> bool {
        matches!(&self.t.get(self.i + 1).map(|t| &t.kind), Some(Tok::Op(x)) if x == s)
    }

    pub(crate) fn peek2_is_kw(&self, s: &str) -> bool {
        matches!(&self.t.get(self.i + 1).map(|t| &t.kind), Some(Tok::Kw(k)) if *k == s)
    }
}
