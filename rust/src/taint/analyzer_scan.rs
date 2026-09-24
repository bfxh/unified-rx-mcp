//! taint 子模块（S168 从 taint.rs 拆出；纯搬移，未改语义）。
use super::*;

/// 挂起的 `def`：(名字, 形参, def 行缩进层级, def 行号, 入口)。
type PendingDef = (String, Vec<String>, usize, usize, bool);

impl Analyzer {
    pub(crate) fn new(file: &str, src: &str, naive: bool) -> Self {
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

    pub(crate) fn lookup(&self, scope: usize, var: &str) -> Option<TSrc> {
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
    pub(crate) fn pass1(&mut self) {
        let mut scope_stack: Vec<usize> = vec![0];
        let mut ind_level = 0usize;
        let mut pending_def: Option<PendingDef> = None;
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
                                origin: None,
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
                    let (next, pend) = self.pass1_def_arm(i, ind_level);
                    pending_def = pend;
                    i = next;
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

    /// `def` 行解析：名字 / 形参 / 单行体 / 入口标记（`def` 前的装饰器段里出现 `tool(`
    /// = MCP 宿主可达边界）。返回 (下一 token 下标, 挂起的 def)；单行体不建作用域 ⇒ `None`。
    fn pass1_def_arm(&self, i: usize, ind_level: usize) -> (usize, Option<PendingDef>) {
        let line = self.toks[i].line;
        let mut j = i + 1;
        let mut name = String::new();
        if let Some(Tk::Id(n)) = self.toks.get(j).map(|t| &t.tk) {
            name = n.clone();
            j += 1;
        }
        let mut params = Vec::new();
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
        if saw_body {
            (j, None)
        } else {
            (j, Some((name, params, ind_level, line, entry)))
        }
    }

    /// 单语句：找赋值/for/with + 记录全部调用 + 传播污点。
    pub(crate) fn scan_stmt(&mut self, scope: usize, start: usize, end: usize, line: usize) {
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
            if !targets.is_empty() && !rhs_ends_sanitized
                && let Some(hit) = self.expr_taint(scope, k + 1, end) {
                    for t in &targets {
                        self.scopes[scope].taint.insert(t.clone(), TSrc {
                            line: hit.line, kind: hit.kind.clone(),
                            interproc: hit.interproc, definite: hit.definite,
                            origin: hit.origin.clone(), // S128：跨文件链随赋值传播
                        });
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
            if !targets.is_empty()
                && let Some(hit) = self.expr_taint(scope, n + 1, end) {
                    for t in &targets {
                        self.scopes[scope].taint.insert(t.clone(), TSrc {
                            line: hit.line, kind: hit.kind.clone(),
                            interproc: hit.interproc, definite: hit.definite,
                            origin: hit.origin.clone(), // S128：跨文件链随赋值传播
                        });
                    }
                }
            return;
        }
        // 4) with expr as var:
        if let Some(a) = with_as {
            let targets = self.lhs_targets(a + 1, end);
            if !targets.is_empty()
                && let Some(hit) = self.expr_taint(scope, start, a) {
                    for t in &targets {
                        self.scopes[scope].taint.insert(t.clone(), TSrc {
                            line: hit.line, kind: hit.kind.clone(),
                            interproc: hit.interproc, definite: hit.definite,
                            origin: hit.origin.clone(), // S128：跨文件链随赋值传播
                        });
                    }
                }
            return;
        }
        // 5) return expr（记录供跨函数返回传播）
        if let Tk::Id(w) = &self.toks[start].tk
            && w == "return" && start + 1 < end {
                self.scopes[scope].rets.push((start + 1, end, line));
            }
    }

    /// 赋值/for/with 目标：区间内的标识符（跳过属性位/关键字/括号内的 kwarg 名）。
    /// 注意 `x = ...` 的目标后跟 `=` 是赋值目标不是 kwarg——kwarg 名只在括号内出现，
    /// 深度必须 >0 才跳过（S78 首测实锤：无深度判断时所有赋值目标被吞，污点全断）。
    pub(crate) fn lhs_targets(&self, start: usize, end: usize) -> Vec<String> {
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
    pub(crate) fn collect_calls(&mut self, scope: usize, start: usize, end: usize) {
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
                    while let Some(Tk::Id(w)) = self.toks.get(j).map(|t| &t.tk) {
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
    pub(crate) fn recv_start(&self, stmt_start: usize, dot: usize) -> usize {
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
    pub(crate) fn parse_args(&self, open: usize, end: usize) -> (Vec<ArgSlice>, usize) {
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
    pub(crate) fn arg_kw(&self, a: &mut ArgSlice) {
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
    pub(crate) fn dotted_at(&self, k: usize, _end: usize) -> Option<(String, usize)> {
        let mut parts: Vec<String> = Vec::new();
        let mut j = k;
        while let Some(Tk::Id(w)) = self.toks.get(j).map(|t| &t.tk) {
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
        if parts.len() >= 2 {
            Some((parts.join("."), j))
        } else {
            None
        }
    }

    /// `(` 的配对 `)` 下标。
    pub(crate) fn match_close(&self, open: usize, end: usize) -> Option<usize> {
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

}
