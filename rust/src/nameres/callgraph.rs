//! nameres 子模块（S168 从 nameres.rs 拆出；纯搬移，未改语义）。
use super::*;


/// 目录级调用图（S125）：`{root, files, nodes, edges, unresolved, stats}`。
/// 节点 = def/class 定义；边 = 调用点 → 定义（同文件直接裁决，跨文件 stitch）；
/// 不可解析调用如实入 unresolved（reason 分类）。路径均相对 root。
pub fn callgraph_dir(root: &Path, max_files: usize) -> Value {
    let mut py: Vec<std::path::PathBuf> = Vec::new();
    walk_py(root, &mut py, max_files);

    let prefix = if root.join("__init__.py").is_file() {
        root.file_name().map(|s| s.to_string_lossy().into_owned())
    } else {
        None
    };

    // 阶段 1：预扫描（S156：**分块并行 + 有序收集**——逐文件独立：读+解析+预扫描；
    // 收集按块序 = 文件序，阶段 2 消费顺序不变 → 输出逐字节同，金标准/审计测试锁）
    let _dbg = std::env::var("UNIFIED_RX_DEBUG_TIMING").is_ok();
    let _t0 = std::time::Instant::now();
    let mut pre: Vec<CgPre> = prescan_parallel(&py, root, prefix.as_deref());

    // 模块索引
    let mut index: HashMap<String, usize> = HashMap::new();
    for (i, f) in pre.iter().enumerate() {
        if !f.modname.is_empty() {
            index.insert(f.modname.clone(), i);
        }
    }

    if _dbg { eprintln!("NM_TIMING phase1={}ms", _t0.elapsed().as_millis()); }
    let _t1 = std::time::Instant::now();
    // 阶段 2：主遍历（种子 + 调用采集）——S157：**分块并行**（每文件自包含：
    // Resolver 只吃本文件 ps+tree，共享只读 index 仅 stitch 用）；按块序合并 =
    // 文件序，输出逐字节同（金标准/审计测试锁）。
    let mut nodes: Vec<Value> = Vec::new();
    let mut edges: Vec<Value> = Vec::new();
    let mut unresolved: Vec<Value> = Vec::new();
    let mut deferred: Vec<Value> = Vec::new();
    let (mut n_calls, mut n_builtin_calls) = (0usize, 0usize);
    {
        let n_thr = crate::par::par_degree(0); // S167：0 = 用满可用并行度
        if pre.len() < 8 || n_thr <= 1 {
            for f in pre.iter_mut() {
                let o = phase2_one(f);
                nodes.extend(o.nodes);
                edges.extend(o.calls);
                unresolved.extend(o.unresolved);
                deferred.extend(o.deferred);
                n_calls += o.n_calls;
                n_builtin_calls += o.n_builtin_calls;
            }
        } else {
            let chunk = pre.len().div_ceil(n_thr);
            std::thread::scope(|s| {
                let handles: Vec<_> = pre.chunks_mut(chunk).map(|c| {
                    s.spawn(move || {
                        let mut o = Phase2Out::default();
                        for f in c {
                            let x = phase2_one(f);
                            o.nodes.extend(x.nodes);
                            o.calls.extend(x.calls);
                            o.unresolved.extend(x.unresolved);
                            o.deferred.extend(x.deferred);
                            o.n_calls += x.n_calls;
                            o.n_builtin_calls += x.n_builtin_calls;
                        }
                        o
                    })
                }).collect();
                for h in handles {
                    let o = h.join().unwrap_or_default();
                    nodes.extend(o.nodes);
                    edges.extend(o.calls);
                    unresolved.extend(o.unresolved);
                    deferred.extend(o.deferred);
                    n_calls += o.n_calls;
                    n_builtin_calls += o.n_builtin_calls;
                }
            });
        }
    }

    if _dbg { eprintln!("NM_TIMING phase2={}ms", _t1.elapsed().as_millis()); }
    // stitch：跨文件待定调用裁决（S157：分块并行）
    // S157：stitch **分块并行**（每条 deferred 独立、只读 index/pre）——按 deferred
    // 序合并，与串行逐字节同（金标准/审计测试锁）。
    let mut n_stitched = 0usize;
    {
        let n_thr = crate::par::par_degree(8);
        if deferred.len() < 8 || n_thr <= 1 {
            for d in &deferred {
                let (e, u, st) = stitch_one(d, &index, &pre);
                if let Some(e) = e { edges.push(e); }
                if let Some(u) = u { unresolved.push(u); }
                if st { n_stitched += 1; }
            }
        } else {
            let chunk = deferred.len().div_ceil(n_thr);
            let idx_ref: &HashMap<String, usize> = &index;
            let pre_ref: &[CgPre] = &pre;
            std::thread::scope(|s| {
                let handles: Vec<_> = deferred.chunks(chunk).map(|c| {
                    s.spawn(move || {
                        let mut e_v: Vec<Value> = Vec::new();
                        let mut u_v: Vec<Value> = Vec::new();
                        let mut st_n = 0usize;
                        for d in c {
                            let (e, u, st) = stitch_one(d, idx_ref, pre_ref);
                            if let Some(e) = e { e_v.push(e); }
                            if let Some(u) = u { u_v.push(u); }
                            if st { st_n += 1; }
                        }
                        (e_v, u_v, st_n)
                    })
                }).collect();
                for h in handles {
                    let (mut e_v, mut u_v, st_n) = h.join().unwrap_or_default();
                    edges.append(&mut e_v);
                    unresolved.append(&mut u_v);
                    n_stitched += st_n;
                }
            });
        }
    }

    // 稳定排序 + 统计
    nodes.sort_by_key(|a| (vstr(a, "file"), vint(a, "line"), vstr(a, "qual")));
    edges.sort_by_key(|a| (vstr(a, "file"), vint(a, "line"), vstr(a, "callee")));
    unresolved.sort_by_key(|a| (vstr(a, "file"), vint(a, "line"), vstr(a, "expr")));
    let mut by_reason: HashMap<String, usize> = HashMap::new();
    for u in &unresolved {
        *by_reason.entry(vstr(u, "reason")).or_insert(0) += 1;
    }
    let mut reasons: Vec<(String, usize)> = by_reason.into_iter().collect();
    reasons.sort();
    let n_nodes = nodes.len();
    let n_resolved = edges.len();
    let n_unresolved = unresolved.len();

    Value::Obj(vec![
        ("root".into(), Value::Str(root.to_string_lossy().into_owned())),
        ("files".into(), Value::Int(pre.len() as i128)),
        ("nodes".into(), Value::Arr(nodes)),
        ("edges".into(), Value::Arr(edges)),
        ("unresolved".into(), Value::Arr(unresolved)),
        ("stats".into(), Value::Obj(vec![
            ("files".into(), Value::Int(pre.len() as i128)),
            ("nodes".into(), Value::Int(n_nodes as i128)),
            ("calls".into(), Value::Int(n_calls as i128)),
            ("resolved".into(), Value::Int(n_resolved as i128)),
            ("unresolved".into(), Value::Int(n_unresolved as i128)),
            ("builtin_calls".into(), Value::Int(n_builtin_calls as i128)),
            ("stitched".into(), Value::Int(n_stitched as i128)),
            ("by_reason".into(), Value::Obj(reasons.into_iter()
                .map(|(k, c)| (k, Value::Int(c as i128))).collect())),
        ])),
    ])
}

/// S157：阶段 2 单文件结果（合并顺序 = 文件序）。
#[derive(Default)]
pub(crate) struct Phase2Out {
    pub(crate) nodes: Vec<Value>,    pub(crate) calls: Vec<Value>,    pub(crate) unresolved: Vec<Value>,    pub(crate) deferred: Vec<Value>,    pub(crate) n_calls: usize,    pub(crate) n_builtin_calls: usize,}

/// S157：阶段 2 单文件主遍历（原循环体逐字搬入；含绑定表放回）。
pub(crate) fn phase2_one(f: &mut CgPre) -> Phase2Out {
    let tree = &f.tree;
    let mut r = Resolver::new(&f.rel, &f.modname, true);
    r.scopes[0].bindings = std::mem::take(&mut f.ps.module);
    r.class_tables = std::mem::take(&mut f.ps.classes);
    r.imports = std::mem::take(&mut f.ps.imports);
    r.star_import = f.ps.star;
    for c in &tree.children {
        r.stmt(c);
    }
    let out = Phase2Out {
        nodes: r.nodes,
        calls: r.calls,
        unresolved: r.calls_unresolved,
        deferred: r.deferred,
        n_calls: r.n_calls,
        n_builtin_calls: r.n_builtin_calls,
    };
    // 主遍历后的模块绑定表放回（stitch 用）
    f.ps.module = std::mem::take(&mut r.scopes[0].bindings);
    out
}

