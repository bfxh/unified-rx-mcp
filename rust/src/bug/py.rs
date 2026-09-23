//! bug 子模块（S168 从 bug.rs 拆出；纯搬移，未改语义）。
use super::*;

pub(crate) fn scan_python(src: &str, path: &str) -> Vec<Issue> {
    let tree = match pyast::parse_module(src) {
        Ok(t) => t,
        Err(e) => {
            return vec![Issue {
                line: e.line,
                rule: "syntax_error",
                msg: format!("语法错误: {}", e.msg),
                file: path.to_string(),
                sev: None,
                kind: None,
            }];
        }
    };
    let mut defined: HashSet<String> = HashSet::new();
    let lines: Vec<&str> = src.split('\n').collect();  // S131：行上下文规则用
    // imported：first-occurrence 序 + last-occurrence 行号（Python dict 语义）
    let mut imported: Vec<(String, usize)> = Vec::new();

    // 走一：定义收集
    let mut q: VecDeque<&PyNode> = VecDeque::new();
    q.push_back(&tree);
    while let Some(n) = q.pop_front() {
        for c in &n.children {
            q.push_back(c);
        }
        match n.kind {
            "FunctionDef" | "AsyncFunctionDef" => {
                defined.insert(n.name.clone());
                if let Some(args) = n.children.first() {
                    for a in &args.children {
                        if matches!(a.kind, "arg" | "vararg" | "kwarg") {
                            defined.insert(a.name.clone());
                        }
                    }
                }
            }
            "ClassDef" => {
                defined.insert(n.name.clone());
                // 怪癖保真：bases 里的 Name 无条件入 defined（Load 也收）；keywords 不收
                for b in n.children.iter().take(n.aux) {
                    collect_names(b, &mut defined);
                }
            }
            "Name" if n.ctx == Ctx::Store => {
                defined.insert(n.name.clone());
            }
            "Import" => {
                for a in &n.children {
                    let key = if !a.name2.is_empty() {
                        a.name2.clone()
                    } else {
                        a.name.split('.').next().unwrap_or("").to_string()
                    };
                    upsert_import(&mut imported, key, n.line);
                }
            }
            "ImportFrom" => {
                // oracle 契约：绑定/遮蔽检查都用 asname（a.asname or a.name）
                for a in &n.children {
                    let key = if !a.name2.is_empty() {
                        a.name2.clone()
                    } else {
                        a.name.clone()
                    };
                    upsert_import(&mut imported, key, n.line);
                }
            }
            "ExceptHandler" if !n.name.is_empty() => {
                defined.insert(n.name.clone());
            }
            "Lambda" => {
                // 怪癖保真：只收 args+kwonly（vararg/kwarg 不算定义）
                // → `lambda *a: a` 报"未定义变量 'a'"，与 Python 版一致
                if let Some(args) = n.children.first() {
                    for a in &args.children {
                        if a.kind == "arg" {
                            defined.insert(a.name.clone());
                        }
                    }
                }
            }
            "Global" | "Nonlocal" => {
                for s in &n.names {
                    defined.insert(s.clone());
                }
            }
            _ => {}
        }
    }
    for (k, _) in &imported {
        defined.insert(k.clone());
    }
    defined.extend(BUILTINS.iter().map(|s| s.to_string()));
    defined.extend(SPECIAL.iter().map(|s| s.to_string()));

    // 走二：问题点（同树同序 BFS——同文件同行 tie 的次序依赖它）
    let mut issues: Vec<Issue> = Vec::new();
    let mut q: VecDeque<&PyNode> = VecDeque::new();
    q.push_back(&tree);
    while let Some(n) = q.pop_front() {
        for c in &n.children {
            q.push_back(c);
        }
        if n.kind == "ExceptHandler" && n.aux == 0 {
            issues.push(Issue {
                line: n.line,
                rule: "bare_except",
                msg: "裸 except（吞掉所有异常）".to_string(),
                file: path.to_string(),
                sev: None,
                kind: None,
            });
        }
        if n.kind == "Call"
            && let Some(f) = n.children.first()
                && f.kind == "Name" && matches!(f.name.as_str(), "eval" | "exec" | "compile") {
                    // 只查裸 Name 调用：re.compile 等 Attribute 成员调用天然排除（S61 教训）
                    let hot = f.name == "eval" || f.name == "exec";
                    issues.push(Issue {
                        line: n.line,
                        rule: "eval_exec",
                        msg: format!("python 动态执行 {}()——注入面（裸调用）", f.name),
                        file: path.to_string(),
                        sev: Some(if hot { "high" } else { "med" }),
                        kind: Some(if hot { "definite" } else { "clue" }),
                    });
                }
        if n.kind == "Name" && n.ctx == Ctx::Load && !defined.contains(&n.name) {
            issues.push(Issue {
                line: n.line,
                rule: "undefined_name",
                msg: format!("未定义变量 '{}'", n.name),
                file: path.to_string(),
                sev: None,
                kind: None,
            });
        }
        // ---- S131（CONSOLIDATION §四 P2-B）：注入/反序列化/竞态八条新规则 ----
        // 严重度口径：definite=模式本身即危险构造；clue=需上下文确认。
        if n.kind == "Call"
            && let Some(f) = n.children.first() {
                let (cname, recv) = if f.kind == "Name" {
                    (f.name.clone(), "")
                } else if f.kind == "Attribute" {
                    (f.name.clone(),
                     f.children.first().map(|v| v.name.as_str()).unwrap_or(""))
                } else {
                    (String::new(), "")
                };
                // 1) subprocess shell=True：以 keyword 节点行定位值（跨行调用也可判）
                if recv == "subprocess"
                    && matches!(cname.as_str(), "run" | "call" | "check_call"
                                                   | "check_output" | "Popen")
                    && let Some(kw) = n.children.iter()
                        .find(|c| c.kind == "keyword" && c.name == "shell")
                    && lines.get(kw.line.saturating_sub(1)).copied().unwrap_or("")
                        .contains("True") {
                            issues.push(Issue {
                                line: n.line,
                                rule: "py_shell_true",
                                msg: "subprocess shell=True——参数经 shell 解析（命令注入面）"
                                    .to_string(),
                                file: path.to_string(),
                                sev: Some("high"), kind: Some("definite"),
                            });
                        }
                // 2) pickle 反序列化
                if recv == "pickle" && matches!(cname.as_str(), "loads" | "load") {
                    issues.push(Issue {
                        line: n.line,
                        rule: "pickle_loads",
                        msg: format!("pickle.{}()——不可信数据反序列化可致任意代码执行", cname),
                        file: path.to_string(),
                        sev: Some("high"), kind: Some("clue"),
                    });
                }
                // 3) yaml.load 无 Loader / unsafe_load
                if recv == "yaml"
                    && (cname == "unsafe_load"
                        || (cname == "load"
                            && !n.children.iter().any(|c| c.kind == "keyword"
                                                            && c.name == "Loader")))
                {
                    issues.push(Issue {
                        line: n.line,
                        rule: "yaml_unsafe_load",
                        msg: format!("yaml.{}() 未指定 Loader——可构造任意对象", cname),
                        file: path.to_string(),
                        sev: Some("high"), kind: Some("clue"),
                    });
                }
                // 4) 弱哈希用于口令场景（凭同行 password/pwd/密码 等词提示；非口令用途不报）
                if recv == "hashlib" && matches!(cname.as_str(), "md5" | "sha1") {
                    let t = lines.get(n.line.saturating_sub(1)).copied().unwrap_or("")
                        .to_lowercase();
                    if ["password", "passwd", "pwd", "pw", "密码", "口令"]
                        .iter().any(|k| t.contains(k))
                    {
                        issues.push(Issue {
                            line: n.line,
                            rule: "weak_hash_password",
                            msg: format!("hashlib.{} 用于口令场景——弱哈希不得用于口令",
                                         cname),
                            file: path.to_string(),
                            sev: Some("med"), kind: Some("clue"),
                        });
                    }
                }
                // 5) SQL 拼接执行（实参含 BinOp/JoinedStr 即拼接/格式化）
                if cname == "execute"
                    && n.children.iter().skip(1)
                        .any(|a| matches!(a.kind, "BinOp" | "JoinedStr"))
                {
                    issues.push(Issue {
                        line: n.line,
                        rule: "sql_concat",
                        msg: "SQL execute() 实参含字符串拼接/格式化——改参数化查询"
                            .to_string(),
                        file: path.to_string(),
                        sev: Some("med"), kind: Some("clue"),
                    });
                }
                // 6) tempfile.mktemp 竞态（名字与创建分离）
                if cname == "mktemp" {
                    issues.push(Issue {
                        line: n.line,
                        rule: "mktemp_race",
                        msg: "tempfile.mktemp——名字与创建分离（TOCTOU），用 mkstemp"
                            .to_string(),
                        file: path.to_string(),
                        sev: Some("med"), kind: Some("clue"),
                    });
                }
                // 7) zip/tar extractall（未做成员路径校验时即 zip-slip 面）
                if cname == "extractall" {
                    issues.push(Issue {
                        line: n.line,
                        rule: "zip_extractall",
                        msg: ".extractall()——成员路径未校验时存在 zip-slip".to_string(),
                        file: path.to_string(),
                        sev: Some("low"), kind: Some("clue"),
                    });
                }
            }
        // 8) 有类型 except 且体为 pass（裸 except 已由 bare_except 覆盖，二者互补不重复）
        if n.kind == "ExceptHandler" && n.aux > 0
            && n.children.iter().any(|c| c.kind == "Pass")
        {
            issues.push(Issue {
                line: n.line,
                rule: "except_pass",
                msg: "except 体为 pass——错误被静默吞掉".to_string(),
                file: path.to_string(),
                sev: Some("low"), kind: Some("clue"),
            });
        }
    }
    // 导入遮蔽内建
    for (name, lineno) in &imported {
        if BUILTINS.contains(&name.as_str()) {
            issues.push(Issue {
                line: *lineno,
                rule: "redefined_import",
                msg: format!("导入 '{}' 遮蔽内建名", name),
                file: path.to_string(),
                sev: None,
                kind: None,
            });
        }
    }
    issues
}

pub(crate) fn collect_names(n: &PyNode, out: &mut HashSet<String>) {
    let mut q: VecDeque<&PyNode> = VecDeque::new();
    q.push_back(n);
    while let Some(x) = q.pop_front() {
        for c in &x.children {
            q.push_back(c);
        }
        if x.kind == "Name" {
            out.insert(x.name.clone());
        }
    }
}

pub(crate) fn upsert_import(imported: &mut Vec<(String, usize)>, key: String, line: usize) {
    if let Some(e) = imported.iter_mut().find(|(k, _)| *k == key) {
        e.1 = line;
    } else {
        imported.push((key, line));
    }
}

// ---------- Rust 生产规则 ----------

