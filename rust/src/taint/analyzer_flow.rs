//! taint 子模块（S168 从 taint.rs 拆出；纯搬移，未改语义）。
use super::*;

impl Analyzer {
    /// 表达式污点判定：来源 / 污点变量 / 净化区。返回第一个未净化命中。
    pub(crate) fn expr_taint(&self, scope: usize, start: usize, end: usize) -> Option<Hit> {
        if self.naive || start >= end {
            return None;
        }
        // 净化区：SANITIZERS 调用的括号内部
        let mut zones: Vec<(usize, usize)> = Vec::new();
        for k in start..end {
            if let Tk::Id(w) = &self.toks[k].tk
                && SANITIZERS.contains(&w.as_str())
                    && matches!(self.toks.get(k + 1).map(|t| &t.tk), Some(Tk::Op(o)) if o == "(")
                        && let Some(cl) = self.match_close(k + 1, end) {
                            zones.push((k + 2, cl));
                        }
        }
        let in_zone = |x: usize| zones.iter().any(|(a, b)| x >= *a && x < *b);
        let mut k = start;
        while k < end {
            match &self.toks[k].tk {
                Tk::Id(w) => {
                    // 属性名不参与污点；var.name/stem 是净化
                    if k > start && matches!(&self.toks[k - 1].tk, Tk::Op(o) if o == ".") {
                        k += 1;
                        continue;
                    }
                    // 关键字实参名（x=...）跳过
                    if matches!(self.toks.get(k + 1).map(|t| &t.tk), Some(Tk::Op(o)) if o == "=")
                        && !KEYWORDS.contains(&w.as_str())
                        && SANITIZERS.iter().all(|s| s != w)
                    {
                        k += 1;
                        continue;
                    }
                    // var.name / var.stem：净化（该 var 的这次出现不算）
                    if (w == "name" || w == "stem") && k >= start + 2
                        && let Tk::Id(base) = &self.toks[k - 2].tk
                            && self.lookup(scope, base).is_some() {
                                k += 1;
                                continue;
                            }
                    // 点路径来源
                    if let Some((dotted, next)) = self.dotted_at(k, end) {
                        if let Some(hit) = self.expr_taint_chain(scope, k, next, &zones, &dotted) {
                            return Some(hit);
                        }
                        k = next;
                        continue;
                    }
                    // 单段来源调用：input(...) / raw_input(...)（无点路径可走）
                    if (w == "input" || w == "raw_input")
                        && matches!(self.toks.get(k + 1).map(|t| &t.tk), Some(Tk::Op(o)) if o == "(")
                        && !in_zone(k)
                    {
                        return Some(Hit {
                            var: w.clone(),
                            line: self.toks[k].line,
                            kind: "input".into(),
                            interproc: false,
                            definite: true,
                            origin: None,
                        });
                    }
                    // 网络来源 .recv(
                    if (w == "recv" || w == "recvfrom")
                        && matches!(self.toks.get(k + 1).map(|t| &t.tk), Some(Tk::Op(o)) if o == "(")
                    {
                        return Some(Hit {
                            var: w.clone(),
                            line: self.toks[k].line,
                            kind: "net".into(),
                            interproc: false,
                            definite: true,
                            origin: None,
                        });
                    }
                    // 裸 argv（from sys import argv）
                    if w == "argv" && !in_zone(k) {
                        return Some(Hit {
                            var: "argv".into(),
                            line: self.toks[k].line,
                            kind: "argv".into(),
                            interproc: false,
                            definite: true,
                            origin: None,
                        });
                    }
                    // 污点变量
                    if !in_zone(k)
                        && let Some(t) = self.lookup(scope, w) {
                            return Some(Hit {
                                var: w.clone(),
                                line: t.line,
                                kind: t.kind,
                                interproc: t.interproc,
                                definite: t.definite,
                                origin: t.origin.clone(),
                            });
                        }
                    k += 1;
                }
                _ => k += 1,
            }
        }
        None
    }

    /// 点路径链的命中判定（来源链 / `.name`·`.stem` 属性净化 / 基变量污染）；
    /// `Some` = 命中即返回，`None` = 未命中——链条一律由调用方消费（`dotted_at` 已吃掉整条链）。
    fn expr_taint_chain(
        &self,
        scope: usize,
        k: usize,
        next: usize,
        zones: &[(usize, usize)],
        dotted: &str,
    ) -> Option<Hit> {
        let in_zone = |x: usize| zones.iter().any(|(a, b)| x >= *a && x < *b);
        let is_call = matches!(
            self.toks.get(next).map(|t| &t.tk),
            Some(Tk::Op(o)) if o == "("
        );
        let src = if dotted == "sys.argv" || dotted.starts_with("sys.argv.") {
            Some("argv")
        } else if SOURCE_DOTTED
            .iter()
            .any(|s| dotted == *s || dotted.starts_with(&format!("{}.", s)))
        {
            Some("env")
        } else if dotted.starts_with("request.") {
            Some("request")
        } else if is_call && SOURCE_CALLS.contains(&dotted) {
            Some("input")
        } else {
            None
        };
        if let Some(kind) = src
            && !in_zone(k)
        {
            return Some(Hit {
                var: dotted.to_string(),
                line: self.toks[k].line,
                kind: kind.to_string(),
                interproc: false,
                definite: true,
                origin: None,
            });
        }
        // 链尾 .name/.stem：属性访问取出的就是净化值，整条链不算污点
        if dotted.ends_with(".name") || dotted.ends_with(".stem") {
            return None;
        }
        // 非来源链：基变量污染则整条链的值视为污染
        // （p.write_text 的接收者、tainted.strip() 等——dotted_at 已吃掉
        // 整条链，不在这里查基变量接收者污点就永远轮不到）
        if !in_zone(k)
            && let Some(base) = dotted.split('.').next()
            && let Some(t) = self.lookup(scope, base)
        {
            return Some(Hit {
                var: base.to_string(),
                line: t.line,
                kind: t.kind,
                interproc: t.interproc,
                definite: t.definite,
                origin: t.origin.clone(),
            });
        }
        None
    }

    // ---- 跨函数一跳：实参→形参、污染返回值→调用点（不动点 ≤3 轮）

    pub(crate) fn pass2_interproc(&mut self) {
        let mut fns: HashMap<String, usize> = HashMap::new();
        for (id, sc) in self.scopes.iter().enumerate() {
            if id > 0 {
                fns.insert(sc.name.clone(), id);
            }
        }
        for _round in 0..3 {
            // 返回值污点重算（先算后写，避免借用冲突）
            let mut ret_flags = vec![false; self.scopes.len()];
            for (id, sc) in self.scopes.iter().enumerate() {
                let rets = sc.rets.clone();
                for (s, e, _) in &rets {
                    if self.expr_taint(id, *s, *e).is_some() {
                        ret_flags[id] = true;
                        break;
                    }
                }
            }
            for (id, f) in ret_flags.iter().enumerate() {
                self.scopes[id].ret_tainted = *f;
            }
            let mut seeds: Vec<(usize, String, TSrc)> = Vec::new();
            for call in &self.calls {
                let base = call.callee.rsplit('.').next().unwrap_or("").to_string();
                if SANITIZERS.contains(&base.as_str()) {
                    continue; // 净化器调用不吃污点、不吐污点
                }
                if let Some(&fid) = fns.get(&base) {
                    // 实参 → 形参（按位置 / 按关键字名）
                    for (ai, a) in call.args.iter().enumerate() {
                        if let Some(hit) = self.expr_taint(call.scope, a.start, a.end) {
                            let pname = match &a.kw {
                                Some(kw) => Some(kw.clone()),
                                None => self.scopes[fid].params.get(ai).cloned(),
                            };
                            if let Some(p) = pname {
                                seeds.push((fid, p, TSrc {
                                    line: hit.line, kind: hit.kind, interproc: true,
                                    definite: hit.definite,
                                    origin: hit.origin.clone(), // S128：跨文件来源随跳保留
                                }));
                            }
                        }
                    }
                    // 污染返回值 → 调用点赋值目标
                    if self.scopes[fid].ret_tainted {
                        for t in &call.lhs {
                            seeds.push((call.scope, t.clone(), TSrc {
                                line: call.line, kind: "ret".into(), interproc: true,
                                definite: self.scopes[fid].entry,
                                origin: None,
                            }));
                        }
                    }
                }
            }
            if seeds.is_empty() {
                break;
            }
            for (scope, var, src) in seeds {
                // 只升级不降级：入口实锤（arg 带实锤）覆盖内部形参的普通来源
                let upgrade = match self.scopes[scope].taint.get(&var) {
                    Some(cur) => src.definite && !cur.definite,
                    None => true,
                };
                if upgrade {
                    self.scopes[scope].taint.insert(var, src);
                }
            }
        }
    }

    /// 汇点产出发现。
    pub(crate) fn pass3_findings(&self) -> Vec<Finding> {
        let mut out = Vec::new();
        let mut seen: HashSet<(String, usize, String, String)> = HashSet::new();
        for call in &self.calls {
            let class_sev = if call.method {
                if call.callee == "execute" {
                    Some(("sql", "med"))
                } else if METHOD_SINKS.contains(&call.callee.as_str()) {
                    Some(("path", "med"))
                } else {
                    None
                }
            } else if EXEC_SINKS.contains(&call.callee.as_str())
                || call.callee.starts_with("subprocess.")
            {
                Some(("exec", "high"))
            } else if PATH_SINKS.contains(&call.callee.as_str()) {
                Some(("path", "med"))
            } else {
                None
            };
            let (class, sev) = match class_sev {
                Some(x) => x,
                None => continue,
            };
            if SANITIZERS.contains(&call.callee.as_str()) {
                continue;
            }
            let hit = if self.naive {
                // 模式匹配基线：实参/接收者含任何标识符即报
                let any_id = call.args.iter().any(|a| {
                    (a.start..a.end).any(|k| matches!(self.toks[k].tk, Tk::Id(_)))
                }) || (call.method && {
                    let (s, e) = call.recv;
                    (s..e).any(|k| matches!(self.toks[k].tk, Tk::Id(_)))
                });
                any_id.then(|| Hit {
                    var: "<expr>".into(),
                    line: call.line,
                    kind: "naive".into(),
                    interproc: false,
                    definite: false,
                    origin: None,
                })
            } else {
                let mut h = None;
                if call.method {
                    let (s, e) = call.recv;
                    h = self.expr_taint(call.scope, s, e);
                    if h.is_none() && class == "sql" {
                        for a in &call.args {
                            if let Some(x) = self.expr_taint(call.scope, a.start, a.end) {
                                h = Some(x);
                                break;
                            }
                        }
                    }
                } else {
                    for a in &call.args {
                        if let Some(x) = self.expr_taint(call.scope, a.start, a.end) {
                            h = Some(x);
                            break;
                        }
                    }
                }
                h
            };
            if let Some(hit) = hit {
                let key = (self.file.clone(), call.line, call.callee.clone(), hit.var.clone());
                if seen.insert(key) {
                    // S128：链证据优先——跨文件链 > 文件内跨函数 > 直接流
                    let flow = if hit.origin.is_some() {
                        "cross"
                    } else if hit.interproc {
                        "interproc"
                    } else {
                        "direct"
                    };
                    // 判级：入口可达 = 实锤 definite；内部形参流 = clue（待人工确认）
                    let kind = if self.naive {
                        "naive"
                    } else if hit.definite {
                        "definite"
                    } else {
                        "clue"
                    };
                    out.push(Finding {
                        file: self.file.clone(),
                        line: call.line,
                        sink: if call.method {
                            format!(".{}", call.callee)
                        } else {
                            call.callee.clone()
                        },
                        var: hit.var,
                        source_line: hit.line,
                        source_kind: hit.kind,
                        flow: flow.into(),
                        severity: sev.into(),
                        kind: kind.into(),
                        origin: hit.origin,
                    });
                }
            }
        }
        out
    }

// ---------------------------------------------------------------- 入口

}
