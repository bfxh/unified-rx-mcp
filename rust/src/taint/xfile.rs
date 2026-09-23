//! taint 子模块（S168 从 taint.rs 拆出；纯搬移，未改语义）。
use super::*;

/// S128：跨文件链证据行——head 已含前序链时继续追加一跳。
/// 字符级截断（路径可含中文，禁止字节切片），保持单链 ≤ ~200 字符可读。
pub(crate) fn chain_head(file: &str, hit: &Hit) -> String {
    match &hit.origin {
        Some(o) => o.clone(),
        None => format!("{}:{} {}({})", file, hit.line, hit.var, hit.kind),
    }
}

pub(crate) fn chain_push(head: &str, dst_file: &str, func: &str, param: &str) -> String {
    let mut h = head.to_string();
    if h.chars().count() > 140 {
        h = h.chars().take(140).collect::<String>() + "…";
    }
    format!("{} → {}:{}.{}", h, dst_file, func, param)
}

/// S128：调用图（nameres）解析结果 → { (file, line) → [被调函数基础名] }。
///
/// 只取"调用点 → 被调定义"这一层，实参/数据流仍由污点引擎自己管——两个引擎
/// 各司其职（S125 纪律：同名解析不另起第二份实现，复用 nameres 的作用域引擎）。
/// 同一行多个调用解析出多个目标时如实保留列表，消费侧"不等一即不猜"。
pub(crate) fn callgraph_targets(root: &Path) -> HashMap<(String, usize), Vec<String>> {
    let mut map: HashMap<(String, usize), Vec<String>> = HashMap::new();
    if !root.is_dir() {
        return map; // 单文件扫描无目录上下文，退回文本名匹配
    }
    let v = nameres::callgraph_dir(root, 2000);
    let json::Value::Obj(pairs) = v else { return map };
    let Some((_, json::Value::Arr(edges))) =
        pairs.iter().find(|(k, _)| k == "edges") else { return map };
    for e in edges {
        let json::Value::Obj(ep) = e else { continue };
        let get_str = |k: &str| {
            ep.iter().find(|(kk, _)| kk == k).and_then(|(_, vv)| match vv {
                json::Value::Str(s) => Some(s.clone()),
                _ => None,
            })
        };
        let line = ep.iter().find(|(kk, _)| kk == "line").and_then(|(_, vv)| match vv {
            json::Value::Int(i) => Some(*i as usize),
            _ => None,
        });
        if let (Some(file), Some(line), Some(callee)) =
            (get_str("file"), line, get_str("callee"))
        {
            let base = callee.rsplit('.').next().unwrap_or("").to_string();
            if !base.is_empty() {
                let slot = map.entry((file, line)).or_default();
                if !slot.contains(&base) {
                    slot.push(base);
                }
            }
        }
    }
    map
}

/// S128：跨文件污点传播（不动点 ≤4 轮）。
///
/// 规则（与 pass2 同纪律，"只升级不降级"）：
/// - 连边条件：被调函数名**全扫描集唯一**才连（多义如实跳过并计数，不猜）；
///   同文件调用由 pass2 处理，此处只跨文件；
/// - 实参→形参：实参在调用者上下文污点命中时，把被调函数形参标污
///   （按位置 / 关键字名），来源信息与链证据随跳保留；
/// - 污染返回值→调用点赋值目标：被调函数 ret_tainted 时污染 caller 的 lhs，
///   来源取被调函数第一条污染 return 的真实 Hit（kind/行号更真）；
/// - 升级规则：实锤升级非实锤照旧；**同级别只补链证据**（cur 无 origin 而
///   seed 有 → 替换一次，补完即止，不会往复振荡）；
/// - 净化跨界：实参表达式被 SANITIZERS 包裹时 expr_taint 返回 None（净化区），
///   seed 自然不产生——callee 内部再净化同理挡在 pass3；
/// - 环安全：轮次上限 + 只升级语义，互递归/自递归天然收敛。
pub(crate) fn cross_file_propagate(
    units: &mut [Analyzer],
    targets: &HashMap<(String, usize), Vec<String>>,
) -> usize {
    let mut index: HashMap<String, Vec<(usize, usize)>> = HashMap::new();
    for (ui, a) in units.iter().enumerate() {
        for (sid, sc) in a.scopes.iter().enumerate() {
            if sid == 0 {
                continue;
            }
            index.entry(sc.name.clone()).or_default().push((ui, sid));
        }
    }
    let mut skipped_ambiguous = 0usize;
    for cands in index.values() {
        if cands.len() > 1 {
            skipped_ambiguous += 1;
        }
    }
    // S155：增量传播（反向索引 × 脏集合）替代"每轮全量重扫"。
    // 不动点语义不变：只有"自身被改"或"其被调方被改"的单元，下一轮才可能产生
    // 新种子；其余单元的种子与上轮逐字相同（已应用过），重扫纯属浪费。
    let mut callers_of: HashMap<usize, Vec<usize>> = HashMap::new();
    let mut active: Vec<usize> = (0..units.len()).collect();
    let mut round = 0usize;
    while round < 4 && !active.is_empty() {
        round += 1;
        let mut seeds: Vec<(usize, usize, String, TSrc)> = Vec::new();
        for &ui in &active {
            let mut local: Vec<(usize, usize, String, TSrc)> = Vec::new();
            {
                let a = &units[ui];
                for call in &a.calls {
                    // S154：热循环去克隆（每轮 × 每调用一次分配）——逻辑与取值不变
                    let textual = call.callee.rsplit('.').next().unwrap_or("");
                    if textual.is_empty() || SANITIZERS.contains(&textual) {
                        continue;
                    }
                    // S128：调用图解析结果优先（含别名/相对导入/模块属性调用），
                    // 无解析则回退文本名——同名多义由消费侧唯一性纪律兜底
                    let base: &str = match targets.get(&(a.file.clone(), call.line)) {
                        Some(v) if v.len() == 1 => v[0].as_str(),
                        Some(_) => continue, // 同一行多调用多目标：不猜
                        None => textual,
                    };
                    if SANITIZERS.contains(&base) {
                        continue;
                    }
                    let Some(cands) = index.get(base) else { continue };
                    if cands.len() != 1 {
                        continue; // 多义：如实不猜
                    }
                    let (tu, tsid) = cands[0];
                    if tu == ui {
                        continue; // 同文件已由 pass2 处理
                    }
                    // S155：记录"被调方 → 调用方"边（下一轮候选集用）
                    let e = callers_of.entry(tu).or_default();
                    if !e.contains(&ui) {
                        e.push(ui);
                    }
                    let tparams = units[tu].scopes[tsid].params.clone();
                    let tname = units[tu].scopes[tsid].name.clone();
                    let tfile = units[tu].file.clone();
                    let ret_t = units[tu].scopes[tsid].ret_tainted;
                    let t_entry = units[tu].scopes[tsid].entry;
                    for (ai, arg) in call.args.iter().enumerate() {
                        if let Some(hit) = a.expr_taint(call.scope, arg.start, arg.end) {
                            let pname = match &arg.kw {
                                Some(kw) => Some(kw.clone()),
                                None => tparams.get(ai).cloned(),
                            };
                            if let Some(p) = pname {
                                let head = chain_head(&a.file, &hit);
                                let origin = chain_push(&head, &tfile, &tname, &p);
                                local.push((tu, tsid, p, TSrc {
                                    line: hit.line, kind: hit.kind, interproc: true,
                                    definite: hit.definite, origin: Some(origin),
                                }));
                            }
                        }
                    }
                    if ret_t {
                        // 污染返回值跨界：调用点 lhs 标污。来源优先取被调函数
                        // 第一条污染 return 的真实 Hit（kind=env/argv… 比"ret"更真）
                        let rsrc: Option<Hit> = units[tu].scopes[tsid].rets.iter()
                            .find_map(|(s, e, _)| units[tu].expr_taint(tsid, *s, *e));
                        for t in &call.lhs {
                            let (kind, line, def, origin) = match &rsrc {
                                Some(h) => {
                                    let head = chain_head(&tfile, h);
                                    let o = chain_push(&head, &tfile, &tname, "<ret>");
                                    (h.kind.clone(), h.line, h.definite || t_entry, Some(o))
                                }
                                None => {
                                    let head = chain_head(&a.file, &Hit {
                                        var: t.clone(), line: call.line, kind: "ret".into(),
                                        interproc: true, definite: t_entry, origin: None,
                                    });
                                    let o = chain_push(&head, &tfile, &tname, "<ret>");
                                    ("ret".into(), call.line, t_entry, Some(o))
                                }
                            };
                            local.push((ui, call.scope, t.clone(), TSrc {
                                line, kind, interproc: true, definite: def, origin,
                            }));
                        }
                    }
                }
            }
            seeds.extend(local);
        }
        if seeds.is_empty() {
            break;
        }
        let mut touched: Vec<usize> = Vec::new();
        for (u, scope, var, src) in seeds {
            let upgrade = match units[u].scopes[scope].taint.get(&var) {
                Some(cur) => {
                    (src.definite && !cur.definite)
                        // 同级别只补链证据（无 origin → 有 origin，补完即止）
                        || (cur.origin.is_none() && src.origin.is_some()
                            && src.definite == cur.definite)
                }
                None => true,
            };
            if upgrade {
                units[u].scopes[scope].taint.insert(var, src);
                if !touched.contains(&u) {
                    touched.push(u);
                }
            }
        }
        if touched.is_empty() {
            break;
        }
        for &u in &touched {
            units[u].pass2_interproc(); // 接收侧文件内继续扩散 + 重算 ret_tainted
        }
        // 下一轮候选 = 被改单元 ∪ 它们的调用方（排序去重，确定性）
        let mut next: Vec<usize> = touched.clone();
        for u in &touched {
            if let Some(cs) = callers_of.get(u) {
                for c in cs {
                    if !next.contains(c) {
                        next.push(*c);
                    }
                }
            }
        }
        next.sort_unstable();
        active = next;
    }
    skipped_ambiguous
}

