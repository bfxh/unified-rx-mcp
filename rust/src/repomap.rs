//! repomap —— repo_map（S102）：符号级"个人化 PageRank"上下文地图。
//!
//! 算法源自 aider 的 repo map（定义/引用图 + Personalized PageRank + token 预算
//! 裁剪），零依赖自研：定义复用 `ide::symbol_spans` 的四语言同口径行级匹配器，
//! 引用 = 全仓词法扫描命中已知定义名。图结构：
//!   file 节点 --引用次数--> def 节点；def 节点 --1.0--> 所属 file 节点（包含边，
//!   让随机游走能"从定义回到文件、再走到该文件引用的其他定义"）。
//! 个人化向量：focus 命中的文件权重 50，其余 1（aider 同款 50x 偏置）。
//!
//! 与 aider 的差异（简化，如实记录）：
//! - 引用归属算给"包含引用的文件"，而非"包含引用的定义"（省去作用域消歧）；
//! - 同名定义共享引用权重（aider 按作用域/导入消歧）；
//! - token 估计 = 字符数/4 的近似（aider 用真实分词器）。
//! 定位：给 agent 一张"按相关度裁剪的仓库骨架"，不是精确调用图。

use std::collections::HashMap;
use std::path::Path;

use crate::ide::{ide_lang_of, iter_files_ide, split_py_lines, symbol_spans};
use crate::json::Value;

const DAMPING: f64 = 0.85;
const ITERATIONS: usize = 30;
const FOCUS_WEIGHT: f64 = 50.0;
/// 聚焦文件内定义的最终排名乘数（遥传偏置之外的第二道偏置，见排序处注释）
const FOCUS_BOOST: f64 = 50.0;
const DEF_CAP: usize = 20_000;

struct Def {
    file: usize,
    line: usize,
    kind: &'static str,
    name: String,
}

fn err_obj(msg: &str) -> Value {
    Value::Obj(vec![("error".into(), Value::Str(msg.into()))])
}

fn rel_of(root: &Path, p: &Path) -> String {
    match p.strip_prefix(root) {
        Ok(r) => r.to_string_lossy().replace('\\', "/"),
        Err(_) => p.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default(),
    }
}

fn focused(focus_lc: &[String], rel_lc: &str, full_lc: &str) -> bool {
    // 匹配口径（都小写、'/' 分隔）：相对/绝对全等、路径后缀（"fs.py"）、
    // 目录前缀（"tools" → tools/ 下全部）、文件名词干（"fs" → fs.py）
    let stem = rel_lc.rsplit('/').next().unwrap_or(rel_lc);
    let stem = stem.split('.').next().unwrap_or(stem);
    focus_lc.iter().any(|f| {
        let f = f.trim_start_matches("./");
        rel_lc == f
            || full_lc == f
            || rel_lc.ends_with(f)
            || full_lc.ends_with(f)
            || rel_lc.starts_with(&format!("{}/", f))
            || stem == f
    })
}

/// 主入口。root 已由调用方过沙盒；focus 为路径片段（大小写不敏感、后缀匹配）。
pub fn repo_map(
    root: &Path,
    focus: &[String],
    budget_tokens: usize,
    max_files: i64,
) -> Result<Value, String> {
    if !root.is_dir() {
        return Ok(err_obj(&format!("不是目录: {}", root.display())));
    }
    let focus_lc: Vec<String> =
        focus.iter().map(|f| f.replace('\\', "/").to_lowercase()).collect();
    // S102 补记：max_files 按遍历序截断时，focus 文件可能被大目录（如
    // bench/manual_snaps）挤掉——先以更大的"发现上限"走全仓，把 focus 命中的
    // 文件提到队首再截断（读取/解析仍只对入选的 max_files 个文件做）；
    // 无 focus 时保持原遍历序语义。
    let walk_cap = max_files.saturating_mul(4).max(2000);
    let all = iter_files_ide(root, walk_cap);
    let mut files: Vec<String> = Vec::new();
    if focus_lc.is_empty() {
        files = all.into_iter().take(max_files.max(0) as usize).collect();
    } else {
        let (hot, cold): (Vec<String>, Vec<String>) = all.into_iter().partition(|fp| {
            let p = Path::new(fp);
            let rel_lc = rel_of(root, p).to_lowercase();
            let full_lc = fp.to_lowercase();
            focused(&focus_lc, &rel_lc, &full_lc)
        });
        files.extend(hot);
        files.extend(cold);
        files.truncate(max_files.max(0) as usize);
    }

    // ---- 读文件 + 提取定义 ----
    let mut defs: Vec<Def> = Vec::new();
    let mut file_rel: Vec<String> = Vec::new();
    let mut file_lines: Vec<Vec<String>> = Vec::new();
    for fp in &files {
        let p = Path::new(fp);
        let lang = ide_lang_of(fp);
        let text = match std::fs::read(p) {
            Ok(b) => String::from_utf8_lossy(&b).into_owned(),
            Err(_) => continue, // 读取失败跳过（与 ide 工具族同口径）
        };
        let lines = split_py_lines(&text);
        let fi = file_lines.len();
        for sp in symbol_spans(&lines, lang) {
            if defs.len() >= DEF_CAP {
                break;
            }
            defs.push(Def { file: fi, line: sp.start, kind: sp.kind, name: sp.name });
        }
        file_rel.push(rel_of(root, p));
        file_lines.push(lines);
    }
    let n_files = file_lines.len();

    // ---- 引用扫描：file → def 边（按出现次数加权）----
    let mut by_name: HashMap<&str, Vec<usize>> = HashMap::new();
    for (di, d) in defs.iter().enumerate() {
        by_name.entry(d.name.as_str()).or_default().push(di);
    }
    let mut out_edges: Vec<Vec<(usize, f64)>> = vec![Vec::new(); n_files];
    for fi in 0..n_files {
        let mut counts: HashMap<usize, f64> = HashMap::new();
        for (li, line) in file_lines[fi].iter().enumerate() {
            for (name, _s, _e) in ident_runs(line) {
                let Some(ids) = by_name.get(name.as_str()) else { continue };
                for &di in ids {
                    // 跳过定义自身所在行（声明行不算引用）
                    if defs[di].file == fi && defs[di].line == li {
                        continue;
                    }
                    *counts.entry(di).or_insert(0.0) += 1.0;
                }
            }
        }
        out_edges[fi] = counts.into_iter().collect();
    }

    // ---- 图：节点 0..n_files = 文件，n_files..n_files+defs = 定义 ----
    let n_nodes = n_files + defs.len();
    let mut out: Vec<Vec<(usize, f64)>> = vec![Vec::new(); n_nodes];
    for fi in 0..n_files {
        out[fi] = out_edges[fi].clone();
    }
    // 包含边 file → 自己的每个 def（权重 1）：让"聚焦文件"的权重能流到
    // 它的定义上（aider 的 scope 边对位；没有它，未被引用的聚焦定义拿不到分）
    for (di, d) in defs.iter().enumerate() {
        out[d.file].push((n_files + di, 1.0));
        out[n_files + di] = vec![(d.file, 1.0)];
    }
    // 个人化向量：均匀 1；聚焦文件的文件节点与其全部定义 ×50（aider 同款偏置）
    let mut pers = vec![1.0f64; n_nodes];
    for fi in 0..n_files {
        let rel_lc = file_rel[fi].to_lowercase();
        let full_lc = Path::new(&files[fi]).to_string_lossy().to_lowercase();
        if focused(&focus_lc, &rel_lc, &full_lc) {
            pers[fi] *= FOCUS_WEIGHT;
            for (di, d) in defs.iter().enumerate() {
                if d.file == fi {
                    pers[n_files + di] *= FOCUS_WEIGHT;
                }
            }
        }
    }
    let psum: f64 = pers.iter().sum();
    if psum > 0.0 {
        for v in pers.iter_mut() {
            *v /= psum;
        }
    }

    // ---- 幂迭代 PageRank（含悬挂节点重分配）----
    let mut rank = pers.clone();
    let out_sum: Vec<f64> = out.iter().map(|e| e.iter().map(|(_, w)| w).sum()).collect();
    for _ in 0..ITERATIONS {
        let mut next = vec![0.0f64; n_nodes];
        let mut dangling = 0.0f64;
        for u in 0..n_nodes {
            if out_sum[u] <= 0.0 {
                dangling += rank[u];
                continue;
            }
            let share = DAMPING * rank[u] / out_sum[u];
            for &(v, w) in &out[u] {
                next[v] += share * w;
            }
        }
        for v in 0..n_nodes {
            next[v] += DAMPING * dangling * pers[v] + (1.0 - DAMPING) * pers[v];
        }
        let delta: f64 = next.iter().zip(rank.iter()).map(|(a, b)| (a - b).abs()).sum();
        rank = next;
        if delta < 1e-10 {
            break;
        }
    }

    // ---- 排序 + 预算裁剪渲染 ----
    // S102 补记：仅靠遥传（teleport）偏置，在"内部互引密集的大文件簇"
    // （如快照语料）面前会被图结构淹没——最终排名再乘聚焦系数（可预测）。
    let mut order: Vec<usize> = (0..defs.len()).collect();
    let key = |di: usize| -> f64 {
        let d = &defs[di];
        let rel_lc = file_rel[d.file].to_lowercase();
        let full_lc = Path::new(&files[d.file]).to_string_lossy().to_lowercase();
        let boost = if focused(&focus_lc, &rel_lc, &full_lc) { FOCUS_BOOST } else { 1.0 };
        rank[n_files + di] * boost
    };
    order.sort_by(|&a, &b| {
        key(b)
            .partial_cmp(&key(a))
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| file_rel[defs[a].file].cmp(&file_rel[defs[b].file]))
            .then_with(|| defs[a].line.cmp(&defs[b].line))
            .then_with(|| defs[a].name.cmp(&defs[b].name))
    });

    let budget_chars = budget_tokens.max(1) * 4;
    let mut map = String::new();
    let mut shown = 0usize;
    for &di in &order {
        let d = &defs[di];
        let line = format!("{}:{} {} {}\n", file_rel[d.file], d.line + 1, d.kind, d.name);
        if map.len() + line.len() > budget_chars && shown > 0 {
            break;
        }
        map.push_str(&line);
        shown += 1;
    }
    Ok(Value::Obj(vec![
        ("root".into(), Value::Str(root.to_string_lossy().into_owned())),
        ("focus".into(), Value::Arr(focus.iter().map(|f| Value::Str(f.clone())).collect())),
        ("files_scanned".into(), Value::Int(n_files as i128)),
        ("defs_total".into(), Value::Int(defs.len() as i128)),
        ("defs_shown".into(), Value::Int(shown as i128)),
        ("tokens_est".into(), Value::Int(((map.len() + 3) / 4) as i128)),
        ("engine".into(), Value::Str("pagerank".into())),
        ("truncated".into(), Value::Bool(shown < defs.len())),
        ("map".into(), Value::Str(map)),
    ]))
}

/// 行内标识符段（ASCII 字母/数字/下划线，≥2 字符）——引用扫描用。
fn ident_runs(line: &str) -> Vec<(String, usize, usize)> {
    let cs: Vec<char> = line.chars().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < cs.len() {
        if cs[i].is_ascii_alphabetic() || cs[i] == '_' {
            let start = i;
            i += 1;
            while i < cs.len() && (cs[i].is_ascii_alphanumeric() || cs[i] == '_') {
                i += 1;
            }
            if i - start >= 2 {
                out.push((cs[start..i].iter().collect::<String>(), start, i));
            }
        } else {
            i += 1;
        }
    }
    out
}
