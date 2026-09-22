//! nameres 子模块（S168 从 nameres.rs 拆出；纯搬移，未改语义）。
use super::*;


impl Resolver {
    pub(crate) fn cur(&mut self) -> &mut Scope {
        self.scopes.last_mut().expect("scope stack 非空")
    }

    /// 记录绑定（作用域路径 + 名字 + 行 + 种类），S108 跨文件与 symtable oracle 用。
    /// 子作用域标签：父标签 + "." + 名字（模块层直接名字）。
    pub(crate) fn child_label(&self, name: &str) -> String {
        let parent = self.scopes.last().map(|s| s.label.clone()).unwrap_or_default();
        if parent == "module" || parent.is_empty() {
            name.to_string()
        } else {
            format!("{}.{}", parent, name)
        }
    }

    pub(crate) fn bind(&mut self, name: &str, line: usize, kind: &'static str) {
        if name.is_empty() {
            return;
        }
        // global/nonlocal 声明：绑定落到声明的目标作用域（规则 4 的写侧）
        let n = self.scopes.len();
        let mut target = n - 1;
        for i in (0..n).rev() {
            if matches!(self.scopes[i].kind, SK::Func | SK::Comp) {
                if self.scopes[i].globals.contains(name) {
                    target = 0;
                } else if self.scopes[i].nonlocals.contains(name) {
                    for j in (0..i).rev() {
                        if self.scopes[j].kind == SK::Class {
                            continue;
                        }
                        target = j;
                        break;
                    }
                }
                break;
            }
        }
        let label = self.scopes[target].label.clone();
        let parent = if target > 0 {
            self.scopes[target - 1].label.clone()
        } else {
            String::new()
        };
        self.scopes[target].bind(name, line, kind);
        self.bindings.push(Value::Obj(vec![
            ("scope".into(), Value::Str(label)),
            ("parent".into(), Value::Str(parent)),
            ("name".into(), Value::Str(name.to_string())),
            ("line".into(), Value::Int(line as i128)),
            ("kind".into(), Value::Str(kind.to_string())),
        ]));
        // S125：调用图节点（def/class 才成节点；lambda/嵌套闭包按定义点计）
        if self.collect && (kind == "def" || kind == "class") {
            let node_kind = if kind == "class" {
                "class"
            } else if self.scopes[target].kind == SK::Class {
                "method"
            } else {
                "func"
            };
            let qual = self.qual_at(target, name);
            self.nodes.push(Value::Obj(vec![
                ("qual".into(), Value::Str(qual)),
                ("file".into(), Value::Str(self.file.clone())),
                ("line".into(), Value::Int(line as i128)),
                ("kind".into(), Value::Str(node_kind.into())),
            ]));
        }
    }

    /// 解析 `name` → (作用域下标, 绑定行, 绑定种类)。
    pub(crate) fn resolve(&self, name: &str) -> Option<(usize, usize, &'static str)> {
        let n = self.scopes.len();
        if n == 0 {
            return None;
        }
        // 就近函数类作用域（Func/Comp）的 global / nonlocal 声明优先
        let mut func_idx = None;
        for i in (0..n).rev() {
            if matches!(self.scopes[i].kind, SK::Func | SK::Comp) {
                func_idx = Some(i);
                break;
            }
        }
        if let Some(fi) = func_idx {
            if self.scopes[fi].globals.contains(name) {
                return self.scopes[0].bindings.get(name).map(|(l, k)| (0, *l, *k));
            }
            if self.scopes[fi].nonlocals.contains(name) {
                for i in (0..fi).rev() {
                    if self.scopes[i].kind == SK::Class {
                        continue;
                    }
                    if let Some((l, k)) = self.scopes[i].bindings.get(name) {
                        return Some((i, *l, *k));
                    }
                }
                return None;
            }
        }
        // 常规上溯：最内层含自身；再往外跳过 class 作用域（规则 1）
        for i in (0..n).rev() {
            let innermost = i == n - 1;
            if self.scopes[i].kind == SK::Class && !innermost {
                continue;
            }
            if let Some((l, k)) = self.scopes[i].bindings.get(name) {
                return Some((i, *l, *k));
            }
        }
        None
    }

    pub(crate) fn load(&mut self, name: &str, line: usize) {
        if name.is_empty() {
            return;
        }
        match self.resolve(name) {
            Some((idx, to_line, _bk)) => {
                let kind = if idx == 0 { "module" } else { "local" };
                match kind {
                    "module" => self.n_module += 1,
                    _ => self.n_local += 1,
                }
                self.edges.push(Value::Obj(vec![
                    ("file".into(), Value::Str(self.file.clone())),
                    ("line".into(), Value::Int(line as i128)),
                    ("name".into(), Value::Str(name.to_string())),
                    ("kind".into(), Value::Str(kind.to_string())),
                    ("to_line".into(), Value::Int(to_line as i128)),
                ]));
            }
            None => {
                if is_builtin(name) {
                    self.n_builtin += 1;
                    self.edges.push(Value::Obj(vec![
                        ("file".into(), Value::Str(self.file.clone())),
                        ("line".into(), Value::Int(line as i128)),
                        ("name".into(), Value::Str(name.to_string())),
                        ("kind".into(), Value::Str("builtin".into())),
                        ("to_line".into(), Value::Int(0)),
                    ]));
                } else {
                    self.n_unresolved += 1;
                    self.unresolved.push(Value::Obj(vec![
                        ("line".into(), Value::Int(line as i128)),
                        ("name".into(), Value::Str(name.to_string())),
                        ("reason".into(), Value::Str("not_found".into())),
                    ]));
                }
            }
        }
    }

    // ---------- 绑定位置 ----------

    /// 目标绑定：Name/Tuple/List/Starred 记绑定；Attribute/Subscript 只解析基名。
    pub(crate) fn bind_target(&mut self, n: &PyNode, kind: &'static str) {
        match n.kind {
            "Name" => {
                let (name, line) = (n.name.clone(), n.line);
                self.bind(&name, line, kind);
            }
            "Tuple" | "List" | "Starred" => {
                for c in &n.children {
                    self.bind_target(c, kind);
                }
            }
            "Attribute" => {
                if let Some(base) = n.children.first() {
                    self.expr(base);
                }
            }
            "Subscript" => {
                if let Some(base) = n.children.first() {
                    self.expr(base);
                }
                for c in n.children.iter().skip(1) {
                    self.expr(c);
                }
            }
            _ => {
                for c in &n.children {
                    self.expr(c);
                }
            }
        }
    }

    // ---------- 表达式 ----------

    pub(crate) fn expr(&mut self, n: &PyNode) {
        match n.kind {
            "Call" if self.collect => self.call(n),
            "Name" => {
                if matches!(n.ctx, Ctx::Load) {
                    let (name, line) = (n.name.clone(), n.line);
                    self.load(&name, line);
                }
            }
            "Attribute" => {
                self.n_attr += 1;
                if let Some(base) = n.children.first() {
                    self.expr(base);
                }
            }
            "Lambda" => self.lambda_(n),
            "ListComp" | "SetComp" | "GeneratorExp" | "DictComp" => self.comp(n),
            "NamedExpr" => {
                // 海象运算符：绑定到最近的函数/模块作用域（跳过推导式作用域）
                if let Some(t) = n.children.first()
                    && t.kind == "Name" {
                        let (name, line) = (t.name.clone(), t.line);
                        let mut target = 0usize;
                        for i in (0..self.scopes.len()).rev() {
                            if matches!(self.scopes[i].kind, SK::Module | SK::Func) {
                                target = i;
                                break;
                            }
                        }
                        self.scopes[target].bind(&name, line, "assign");
                    }
                for c in n.children.iter().skip(1) {
                    self.expr(c);
                }
            }
            _ => {
                for c in &n.children {
                    self.expr(c);
                }
            }
        }
    }

    // ---------- S125：调用点采集与解析 ----------

    pub(crate) fn call(&mut self, n: &PyNode) {
        self.n_calls += 1;
        let line = n.line;
        let caller = self.cur_def();
        if let Some(func) = n.children.first() {
            match func.kind {
                "Name" => self.call_name(func, line, &caller),
                "Attribute" => self.call_attr(func, line, &caller),
                _ => {
                    let expr = expr_text(func);
                    self.calls_unresolved.push(self.ures(line, &caller, &expr, "expr"));
                }
            }
        }
        // func 与实参照常下钻（Name(Load) 仍记 load 边，口径与旧路径一致）
        for c in &n.children {
            self.expr(c);
        }
    }

    pub(crate) fn ures(&self, line: usize, caller: &str, expr: &str, reason: &str) -> Value {
        Value::Obj(vec![
            ("file".into(), Value::Str(self.file.clone())),
            ("line".into(), Value::Int(line as i128)),
            ("caller".into(), Value::Str(caller.to_string())),
            ("expr".into(), Value::Str(expr.to_string())),
            ("reason".into(), Value::Str(reason.to_string())),
        ])
    }

    pub(crate) fn edge(&self, line: usize, caller: &str, callee: &str, to_file: &str,
            to_line: usize, kind: &str) -> Value {
        Value::Obj(vec![
            ("file".into(), Value::Str(self.file.clone())),
            ("line".into(), Value::Int(line as i128)),
            ("caller".into(), Value::Str(caller.to_string())),
            ("callee".into(), Value::Str(callee.to_string())),
            ("to_file".into(), Value::Str(to_file.to_string())),
            ("to_line".into(), Value::Int(to_line as i128)),
            ("kind".into(), Value::Str(kind.to_string())),
        ])
    }

    /// 裸名调用 `f()`：同文件解析完成，或转跨文件待定，或如实未解析。
    pub(crate) fn call_name(&mut self, f: &PyNode, line: usize, caller: &str) {
        let name = f.name.clone();
        match self.resolve(&name) {
            Some((idx, to_line, bk)) => match bk {
                "def" | "class" => {
                    let qual = self.qual_at(idx, &name);
                    let kind = if bk == "class" { "class" } else { "name" };
                    let to_file = self.file.clone();
                    self.calls.push(self.edge(line, caller, &qual, &to_file, to_line, kind));
                }
                "import" => self.defer_import(&name, line, caller, ""),
                _ => {
                    let (e, r) = (name.clone(), "var_call");
                    self.calls_unresolved.push(self.ures(line, caller, &e, r));
                }
            },
            None => {
                if is_builtin(&name) {
                    self.n_builtin_calls += 1;
                } else {
                    let reason = if self.star_import { "star_import" } else { "not_found" };
                    let expr = name.clone();
                    self.calls_unresolved.push(self.ures(line, caller, &expr, reason));
                }
            }
        }
    }

    /// 属性调用 `base.attr()`：self/cls 方法、同文件类方法、跨文件模块成员，或如实未解析。
    pub(crate) fn call_attr(&mut self, f: &PyNode, line: usize, caller: &str) {
        let attr = f.name.clone();
        let Some(base) = f.children.first() else { return };
        let expr = expr_text(f);
        if base.kind != "Name" {
            // 链式调用（a.b.c() / f()()）：根是 import 的延迟到 stitch 区分 external/
            // attr_chain（对外部模块的链式调用单列为 external，噪声归类更诚实）
            let mut root = base;
            while matches!(root.kind, "Attribute" | "Call" | "Subscript") {
                match root.children.first() {
                    Some(c) => root = c,
                    None => break,
                }
            }
            if root.kind == "Name"
                && matches!(self.resolve(&root.name), Some((_, _, "import"))) {
                    let rn = root.name.clone();
                    self.defer_chain(&rn, line, caller, &expr);
            } else {
                self.calls_unresolved.push(self.ures(line, caller, &expr, "attr_chain"));
            }
            return;
        }
        let bname = base.name.clone();
        // self.m() / cls.m()：基名是某方法的参数且方法直接位于类体
        if let Some(cls_label) = self.self_like_class(&bname) {
            match self.class_tables.get(&cls_label).and_then(|t| t.get(&attr)).copied() {
                Some((tl, tk)) if tk == "def" || tk == "class" => {
                    let qual = if self.modname.is_empty() {
                        format!("{}.{}", cls_label, attr)
                    } else {
                        format!("{}.{}.{}", self.modname, cls_label, attr)
                    };
                    let to_file = self.file.clone();
                    self.calls.push(self.edge(line, caller, &qual, &to_file, tl, "self_attr"));
                }
                Some(_) => self.calls_unresolved.push(self.ures(line, caller, &expr, "self_attr_var")),
                None => self.calls_unresolved.push(self.ures(line, caller, &expr, "self_attr_missing")),
            }
            return;
        }
        match self.resolve(&bname) {
            Some((idx, _l, bk)) => match bk {
                "import" => self.defer_import(&bname, line, caller, &attr),
                "class" => {
                    let cls_label = self.class_label_at(idx, &bname);
                    match self.class_tables.get(&cls_label).and_then(|t| t.get(&attr)).copied() {
                        Some((tl, tk)) if tk == "def" || tk == "class" => {
                            let qual = if self.modname.is_empty() {
                                format!("{}.{}", cls_label, attr)
                            } else {
                                format!("{}.{}.{}", self.modname, cls_label, attr)
                            };
                            let to_file = self.file.clone();
                            self.calls.push(self.edge(line, caller, &qual, &to_file, tl, "module_attr"));
                        }
                        Some(_) => self.calls_unresolved.push(self.ures(line, caller, &expr, "attr_var")),
                        None => self.calls_unresolved.push(self.ures(line, caller, &expr, "attr_missing")),
                    }
                }
                // 对普通变量取属性调用（含函数对象）：无类型推断，不猜
                _ => self.calls_unresolved.push(self.ures(line, caller, &expr, "receiver_var")),
            },
            None => {
                let reason = if self.star_import { "star_import" } else { "not_found" };
                self.calls_unresolved.push(self.ures(line, caller, &expr, reason));
            }
        }
    }

}
