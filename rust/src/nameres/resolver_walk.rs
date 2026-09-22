//! nameres 子模块（S168 从 nameres.rs 拆出；纯搬移，未改语义）。
use super::*;


impl Resolver {
    /// import 基名/名字 → 跨文件待定（stitch 阶段查模块索引）。
    /// attr 为空 = 名字调用（`f()`）；非空 = `m.f()` 形态的成员调用（记 base）。
    pub(crate) fn defer_import(&mut self, bound: &str, line: usize, caller: &str, attr: &str) {
        let fact = self.imports.iter().find(|i| {
            let b = if i.is_from {
                if i.asname.is_empty() { i.name.clone() } else { i.asname.clone() }
            } else if i.asname.is_empty() {
                i.module.split('.').next().unwrap_or("").to_string()
            } else {
                i.asname.clone()
            };
            b == bound
        }).map(|i| (i.is_from, i.level, i.module.clone(), i.name.clone()));
        let (expr, reason) = if attr.is_empty() {
            (bound.to_string(), "not_found")
        } else {
            (format!("{}.{}", bound, attr), "attr_missing")
        };
        let Some((is_from, level, module, fname)) = fact else {
            // 绑定在但 import 事实缺失（罕见序）→ 如实未解析
            self.calls_unresolved.push(self.ures(line, caller, &expr, reason));
            return;
        };
        if attr.is_empty() && !is_from {
            // `import a.b` 的裸名 `a()`：模块对象不可调用
            self.calls_unresolved.push(self.ures(line, caller, &expr, "var_call"));
            return;
        }
        // 目标模块名（相对导入按层级上溯包；公式与 resolve_dir 一致）
        let target_mod = if is_from && level > 0 {
            let mut base = pkg_of(&self.modname);
            for _ in 1..level {
                base = pkg_of(&base);
            }
            if module.is_empty() { base }
            else if base.is_empty() { module.clone() }
            else { format!("{}.{}", base, module) }
        } else {
            module.clone()
        };
        let (name, base_f, kind) = if attr.is_empty() {
            (fname, String::new(), "from_import")
        } else {
            (attr.to_string(), bound.to_string(), "module_attr")
        };
        self.deferred.push(Value::Obj(vec![
            ("file".into(), Value::Str(self.file.clone())),
            ("line".into(), Value::Int(line as i128)),
            ("caller".into(), Value::Str(caller.to_string())),
            ("expr".into(), Value::Str(expr)),
            ("target_mod".into(), Value::Str(target_mod)),
            ("base".into(), Value::Str(base_f)),
            ("name".into(), Value::Str(name)),
            ("kind".into(), Value::Str(kind.into())),
        ]));
    }

    /// 链式调用的根 import → 待定（仅用于 external/attr_chain 归类）。
    pub(crate) fn defer_chain(&mut self, root_bound: &str, line: usize, caller: &str, expr: &str) {
        let fact = self.imports.iter().find(|i| {
            let b = if i.is_from {
                if i.asname.is_empty() { i.name.clone() } else { i.asname.clone() }
            } else if i.asname.is_empty() {
                i.module.split('.').next().unwrap_or("").to_string()
            } else {
                i.asname.clone()
            };
            b == root_bound
        }).map(|i| (i.is_from, i.level, i.module.clone()));
        let Some((is_from, level, module)) = fact else {
            self.calls_unresolved.push(self.ures(line, caller, expr, "attr_chain"));
            return;
        };
        let target_mod = if is_from && level > 0 {
            let mut base = pkg_of(&self.modname);
            for _ in 1..level {
                base = pkg_of(&base);
            }
            if module.is_empty() { base }
            else if base.is_empty() { module.clone() }
            else { format!("{}.{}", base, module) }
        } else {
            module.clone()
        };
        self.deferred.push(Value::Obj(vec![
            ("file".into(), Value::Str(self.file.clone())),
            ("line".into(), Value::Int(line as i128)),
            ("caller".into(), Value::Str(caller.to_string())),
            ("expr".into(), Value::Str(expr.to_string())),
            ("target_mod".into(), Value::Str(target_mod)),
            ("base".into(), Value::Str(root_bound.to_string())),
            ("name".into(), Value::Str(String::new())),
            ("kind".into(), Value::Str("attr_chain_root".into())),
        ]));
    }

    pub(crate) fn lambda_(&mut self, n: &PyNode) {
        // 默认值在外层求值（规则 6）
        if let Some(args) = n.children.first() {
            for c in &args.children {
                if !matches!(c.kind, "arg" | "vararg" | "kwarg") {
                    self.expr(c);
                }
            }
        }
        let label = format!("<lambda@{}>", n.line);
        self.scopes.push(Scope::new(SK::Func, &label));
        if let Some(args) = n.children.first() {
            for c in &args.children {
                if matches!(c.kind, "arg" | "vararg" | "kwarg") {
                    let (name, line) = (c.name.clone(), c.line);
                    self.bind(&name, line, "param");
                }
            }
        }
        for c in n.children.iter().skip(1) {
            self.expr(c);
        }
        self.scopes.pop();
    }

    pub(crate) fn comp(&mut self, n: &PyNode) {
        let comps: Vec<&PyNode> = n.children.iter().filter(|c| c.kind == "comprehension").collect();
        // 规则 2：首个 comprehension 的 iter 在外层求值
        if let Some(first) = comps.first()
            && let Some(iter) = first.children.get(1) {
                self.expr(iter);
            }
        self.scopes.push(Scope::new(SK::Comp, "<comp>"));
        for (gi, g) in comps.iter().enumerate() {
            // 目标绑定进推导式作用域；后续 iter 也在其中
            if let Some(t) = g.children.first() {
                self.bind_target(t, "comp");
            }
            if gi > 0
                && let Some(iter) = g.children.get(1) {
                    self.expr(iter);
                }
            for c in g.children.iter().skip(2) {
                self.expr(c);
            }
        }
        // 元素表达式（ListComp/SetComp/GenExp = children[0]；DictComp = key+value）
        let head = if n.kind == "DictComp" { 2 } else { 1 };
        for c in n.children.iter().take(head) {
            self.expr(c);
        }
        self.scopes.pop();
    }

    // ---------- 语句 ----------

    pub(crate) fn stmt(&mut self, n: &PyNode) {
        match n.kind {
            "FunctionDef" | "AsyncFunctionDef" => self.funcdef(n),
            "ClassDef" => self.classdef(n),
            "Import" => {
                for a in &n.children {
                    let bound = if a.name2.is_empty() {
                        a.name.split('.').next().unwrap_or("").to_string()
                    } else {
                        a.name2.clone()
                    };
                    self.imports.push(ImportFact {
                        line: a.line, level: 0, module: a.name.clone(),
                        name: String::new(), asname: a.name2.clone(), is_from: false,
                    });
                    self.bind(&bound, a.line, "import");
                }
            }
            "ImportFrom" => {
                for a in &n.children {
                    self.imports.push(ImportFact {
                        line: a.line, level: n.aux, module: n.name.clone(),
                        name: a.name.clone(), asname: a.name2.clone(), is_from: true,
                    });
                    if a.name == "*" {
                        // 规则 8：star import 静态不可解，如实标记
                        self.star_import = true;
                        self.n_unresolved += 1;
                        self.unresolved.push(Value::Obj(vec![
                            ("line".into(), Value::Int(a.line as i128)),
                            ("name".into(), Value::Str("*".into())),
                            ("reason".into(), Value::Str("star_import".into())),
                        ]));
                    } else {
                        let bound = if a.name2.is_empty() { a.name.clone() } else { a.name2.clone() };
                        self.bind(&bound, a.line, "import");
                    }
                }
            }
            "Global" | "Nonlocal" => {
                let names = n.names.clone();
                let g = n.kind == "Global";
                let cur = self.cur();
                for nm in names {
                    if g {
                        cur.globals.insert(nm);
                    } else {
                        cur.nonlocals.insert(nm);
                    }
                }
            }
            "Assign" => {
                let last = n.children.len().saturating_sub(1);
                for (i, c) in n.children.iter().enumerate() {
                    if i == last {
                        self.expr(c);
                    } else {
                        self.bind_target(c, "assign");
                    }
                }
            }
            "AnnAssign" => {
                if let Some(t) = n.children.first() {
                    self.bind_target(t, "assign");
                }
                for c in n.children.iter().skip(1) {
                    self.expr(c);
                }
            }
            "AugAssign" => {
                if let Some(t) = n.children.first() {
                    // x += 1 既读又写：先按读取解析，再记绑定
                    if t.kind == "Name" {
                        let (name, line) = (t.name.clone(), t.line);
                        self.load(&name, line);
                    }
                    self.bind_target(t, "aug");
                }
                for c in n.children.iter().skip(1) {
                    self.expr(c);
                }
            }
            "For" | "AsyncFor" => {
                if let Some(t) = n.children.first() {
                    self.bind_target(t, "for");
                }
                for c in n.children.iter().skip(1) {
                    if is_stmt(c.kind) {
                        self.stmt(c);
                    } else {
                        self.expr(c);
                    }
                }
            }
            "With" | "AsyncWith" => {
                for c in &n.children {
                    if c.kind == "withitem" {
                        if let Some(ctx) = c.children.first() {
                            self.expr(ctx);
                        }
                        if let Some(t) = c.children.get(1) {
                            self.bind_target(t, "with");
                        }
                    } else if is_stmt(c.kind) {
                        self.stmt(c);
                    } else {
                        self.expr(c);
                    }
                }
            }
            "Try" | "TryStar" => {
                for c in &n.children {
                    if c.kind == "ExceptHandler" {
                        if !c.name.is_empty() {
                            let (name, line) = (c.name.clone(), c.line);
                            self.bind(&name, line, "except");
                        }
                        for b in &c.children {
                            self.stmt(b);
                        }
                    } else if is_stmt(c.kind) {
                        self.stmt(c);
                    } else {
                        self.expr(c);
                    }
                }
            }
            "Match" => {
                // Match children = [subject, match_case...]；
                // match_case children = [pattern, guard?, body...]
                for (i, c) in n.children.iter().enumerate() {
                    if i == 0 {
                        self.expr(c);
                    } else if c.kind == "match_case" {
                        if let Some(p) = c.children.first() {
                            self.match_node(p);
                        }
                        for b in c.children.iter().skip(1) {
                            if is_stmt(b.kind) {
                                self.stmt(b);
                            } else {
                                self.expr(b); // guard 表达式
                            }
                        }
                    } else {
                        self.match_node(c);
                    }
                }
            }
            "Delete" => {
                for c in &n.children {
                    self.expr(c);
                }
            }
            _ => {
                for c in &n.children {
                    if is_stmt(c.kind) {
                        self.stmt(c);
                    } else {
                        self.expr(c);
                    }
                }
            }
        }
    }

    /// 模式节点：捕获名进当前作用域；类名/值表达式按读取解析。
    pub(crate) fn match_node(&mut self, n: &PyNode) {
        match n.kind {
            "MatchAs" => {
                if !n.name.is_empty() {
                    let (name, line) = (n.name.clone(), n.line);
                    self.bind(&name, line, "match");
                }
                for c in &n.children {
                    self.match_node(c);
                }
            }
            "MatchStar" | "MatchMapping" => {
                if !n.name.is_empty() {
                    let (name, line) = (n.name.clone(), n.line);
                    self.bind(&name, line, "match");
                }
                for c in &n.children {
                    self.match_node(c);
                }
            }
            "MatchValue" | "MatchClass" => {
                for c in &n.children {
                    self.expr(c);
                }
            }
            _ => {
                for c in &n.children {
                    self.match_node(c);
                }
            }
        }
    }

    pub(crate) fn funcdef(&mut self, n: &PyNode) {
        // 名字绑到外层
        let (fname, fline) = (n.name.clone(), n.line);
        self.bind(&fname, fline, "def");
        // 默认值 / 装饰器 / 返回注解在外层求值（规则 6）
        if let Some(args) = n.children.first() {
            for c in &args.children {
                if !matches!(c.kind, "arg" | "vararg" | "kwarg") {
                    self.expr(c);
                }
            }
        }
        for c in n.children.iter().skip(1) {
            if !is_stmt(c.kind) {
                self.expr(c); // 装饰器 / 返回注解
            }
        }
        // 新函数作用域：参数绑定 + 函数体
        let label = self.child_label(&n.name);
        self.scopes.push(Scope::new(SK::Func, &label));
        // S125：定义栈（调用者归属）；lambda 不入栈（归属最近的外层定义）
        if self.collect {
            self.def_stack.push(label.clone());
        }
        if let Some(args) = n.children.first() {
            for c in &args.children {
                if matches!(c.kind, "arg" | "vararg" | "kwarg") {
                    let (name, line) = (c.name.clone(), c.line);
                    self.bind(&name, line, "param");
                }
            }
        }
        for c in n.children.iter().skip(1) {
            if is_stmt(c.kind) {
                self.stmt(c);
            }
        }
        if self.collect {
            self.def_stack.pop();
        }
        self.scopes.pop();
    }

    pub(crate) fn classdef(&mut self, n: &PyNode) {
        let (cname, cline) = (n.name.clone(), n.line);
        self.bind(&cname, cline, "class");
        // bases（前 aux 个）与关键字实参在外层求值
        for c in n.children.iter().take(n.aux) {
            self.expr(c);
        }
        let label = self.child_label(&n.name);
        self.scopes.push(Scope::new(SK::Class, &label));
        // S125：类作用域预种子——self.m() 的 m 可能在方法之后定义（前向引用）
        if self.collect
            && let Some(tbl) = self.class_tables.get(&label) {
                for (k, v) in tbl {
                    self.scopes.last_mut().expect("scope 非空").bindings.insert(k.clone(), *v);
                }
            }
        // S125：类体直接执行的代码（如类级常量计算）归属该类
        if self.collect {
            self.def_stack.push(label.clone());
        }
        for c in n.children.iter().skip(n.aux) {
            if c.kind == "keyword" {
                self.expr(c);
            } else if is_stmt(c.kind) {
                self.stmt(c);
            } else {
                self.expr(c);
            }
        }
        if self.collect {
            self.def_stack.pop();
        }
        self.scopes.pop();
    }
}
