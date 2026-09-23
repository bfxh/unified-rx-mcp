//! astscan 子模块（S168 从 astscan.rs 拆出；纯搬移，未改语义）。
use super::*;

/// \b(ident)\b 的极大 ASCII 词游程；前后边界按 Python \w 口径（unicode 字母也算词）。
pub(crate) fn ident_finditer(ln: &str) -> Vec<(String, usize)> {
    let cs: Vec<char> = ln.chars().collect();
    let n = cs.len();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < n {
        if (cs[i].is_ascii_alphabetic() || cs[i] == '_') && (i == 0 || !is_py_word(cs[i - 1])) {
            let mut e = i + 1;
            while e < n && is_rust_word(cs[e]) {
                e += 1;
            }
            if e >= n || !is_py_word(cs[e]) {
                out.push((cs[i..e].iter().collect(), i));
            }
            i = e;
            continue;
        }
        i += 1;
    }
    out
}

/// 行内存在 \bfn\s+name\b（定义行判定第一半）。
pub(crate) fn line_has_fn_def(ln: &str, name: &str) -> bool {
    let cs: Vec<char> = ln.chars().collect();
    let nc: Vec<char> = name.chars().collect();
    let n = cs.len();
    let mut i = 0usize;
    while i + 2 <= n {
        if cs[i] == 'f' && cs[i + 1] == 'n' && (i == 0 || !is_py_word(cs[i - 1])) {
            let mut j = i + 2;
            if j >= n || !is_js_space(cs[j]) {
                i += 1;
                continue;
            }
            while j < n && is_js_space(cs[j]) {
                j += 1;
            }
            if j + nc.len() <= n && cs[j..j + nc.len()] == nc[..] {
                let e = j + nc.len();
                if e >= n || !is_py_word(cs[e]) {
                    return true;
                }
            }
        }
        i += 1;
    }
    false
}

pub(crate) struct RustDef {

    pub(crate) name: String,
    pub(crate) file: String,
    pub(crate) line: usize,
    pub(crate) test: bool,}

pub(crate) fn rust_defs_and_refs(masked: &[char], fp: &str, is_test_file: bool) -> (Vec<RustDef>, Vec<(String, i128, i128)>) {
    let text: String = masked.iter().collect();
    let lines: Vec<&str> = text.split('\n').collect();
    let mut defs: Vec<RustDef> = Vec::new();
    let mut refs: Vec<(String, i128, i128)> = Vec::new(); // (name, prod, test)
    let mut in_test_mod = false;
    let mut test_mod_depth: i64 = -1;
    let mut brace_depth: i64 = 0;
    for (idx, ln) in lines.iter().enumerate() {
        let idx = idx + 1;
        let stripped = ln.trim();
        if !in_test_mod && mod_test_attr_search(stripped) {
            in_test_mod = true;
            test_mod_depth = brace_depth;
        } else if in_test_mod && stripped.starts_with('}') && brace_depth <= test_mod_depth {
            in_test_mod = false;
        }
        let ctx_test = is_test_file || in_test_mod;
        // 定义：finditer 全部命中
        {
            let cs: Vec<char> = ln.chars().collect();
            let n = cs.len();
            let mut i = 0usize;
            while i + 2 <= n {
                if cs[i] == 'f' && cs[i + 1] == 'n' && (i == 0 || !is_py_word(cs[i - 1])) {
                    let mut j = i + 2;
                    if j >= n || !is_js_space(cs[j]) {
                        i += 1;
                        continue;
                    }
                    while j < n && is_js_space(cs[j]) {
                        j += 1;
                    }
                    if j < n && (cs[j].is_ascii_alphabetic() || cs[j] == '_') {
                        let mut e = j + 1;
                        while e < n && is_rust_word(cs[e]) {
                            e += 1;
                        }
                        defs.push(RustDef {
                            name: cs[j..e].iter().collect(),
                            file: fp.to_string(),
                            line: idx,
                            test: ctx_test,
                        });
                        i = e;
                        continue;
                    }
                }
                i += 1;
            }
        }
        // 引用（排除定义处本身：行内存在 fn\s+name 且 ident 在最后那个 "fn " 之后）
        for (name, mstart) in ident_finditer(ln) {
            if RUST_KEYWORDS.contains(&name.as_str()) {
                continue;
            }
            let char_pos = ln.chars().take(mstart).count();
            let rfind_fn = {
                // rfind("fn ", 0, m.start())
                let head: String = ln.chars().take(char_pos).collect();
                head.rfind("fn ")
            };
            let is_def_here = line_has_fn_def(ln, &name)
                && matches!(rfind_fn, Some(p) if char_pos > p);
            if is_def_here {
                continue;
            }
            match refs.iter_mut().find(|r| r.0 == name) {
                Some(r) => {
                    if ctx_test {
                        r.2 += 1;
                    } else {
                        r.1 += 1;
                    }
                }
                None => refs.push((name, if ctx_test { 0 } else { 1 }, if ctx_test { 1 } else { 0 })),
            }
        }
        brace_depth += ln.matches('{').count() as i64 - ln.matches('}').count() as i64;
    }
    (defs, refs)
}

/// 每个函数的可达性边：fn -> [(file, line, reach)]
pub(crate) type FnReach = (String, Vec<(String, usize, &'static str)>);

pub(crate) struct ReachResult {

    pub(crate) lmap: Vec<FnReach>,
    pub(crate) helpers: Vec<Value>,}

/// 单文件的「defs + 引用计数」（S169：并行阶段的产物类型；抽别名同时过 clippy::type_complexity）。
type FileReach = (Vec<RustDef>, Vec<(String, i128, i128)>);

/// 单文件的「掩码 + 取 defs/refs」（S169：抽成纯函数才能按文件并行，无共享状态）。
fn reach_one(f: &(String, String, bool)) -> FileReach {
    let (fp, src, is_test_dir) = f;
    let (masked, _, _) = mask_rust(src);
    rust_defs_and_refs(&masked, fp, *is_test_dir)
}


pub(crate) fn rust_reach(rs_sources: &[(String, String, bool)]) -> ReachResult {
    // S169：逐文件阶段**分块并行 + 按块序合并**（同本仓 S166/S157 范式）——它本是纯逐文件工作
    // （实测：本仓语料上 astscan 的串行第二阶段主要成本就在这里）。合并顺序 = 文件序 ⇒ 输出逐字节同。
    let n = crate::par::par_degree(0);
    let per_file: Vec<FileReach> =
        if rs_sources.len() < 8 || n <= 1 {
            rs_sources.iter().map(reach_one).collect()
        } else {
            let chunk = rs_sources.len().div_ceil(n);
            std::thread::scope(|s| {
                let hs: Vec<_> = rs_sources
                    .chunks(chunk)
                    .map(|c| s.spawn(move || c.iter().map(reach_one).collect::<Vec<_>>()))
                    .collect();
                let mut out = Vec::new();
                for h in hs {
                    out.extend(h.join().unwrap_or_default());
                }
                out
            })
        };
    let mut all_defs: Vec<RustDef> = Vec::new();
    let mut merged: Vec<(String, i128, i128)> = Vec::new();
    // S169：两个**索引**取代线性查找——原实现每读一个引用/每个 def 都要扫一遍已累积的向量
    // （实测：本仓语料上 `merged` 数千项 × `defs` 数千项 ⇒ 千万级字符串比较）。索引只用于**查表**，
    // 向量的追加顺序一字未改 ⇒ 输出逐字节同。
    let mut merged_idx: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for (defs, refs) in per_file {
        all_defs.extend(defs);
        for (name, prod, test) in refs {
            match merged_idx.get(&name).copied() {
                Some(i) => {
                    merged[i].1 += prod;
                    merged[i].2 += test;
                }
                None => {
                    merged_idx.insert(name.clone(), merged.len());
                    merged.push((name, prod, test));
                }
            }
        }
    }
    let mut lmap: Vec<FnReach> = Vec::new();
    let mut helpers: Vec<Value> = Vec::new();
    let mut lmap_idx: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for d in &all_defs {
        if d.test {
            continue;
        }
        let (prod, test) = match merged_idx.get(&d.name) {
            Some(&i) => (merged[i].1, merged[i].2),
            None => (0, 0),
        };
        let v: &'static str = if prod > 0 {
            "prod"
        } else if test > 0 {
            helpers.push(o(vec![
                ("fn", s(&d.name)),
                ("file", s(&d.file)),
                ("line", i(d.line)),
            ]));
            "test_only"
        } else {
            "unreferenced"
        };
        let li = match lmap_idx.get(&d.name) {
            Some(&i) => i,
            None => {
                lmap.push((d.name.clone(), Vec::new()));
                lmap_idx.insert(d.name.clone(), lmap.len() - 1);
                lmap.len() - 1
            }
        };
        lmap[li].1.push((d.file.clone(), d.line, v));
    }
    ReachResult { lmap, helpers }
}

// ---------- 编排 ----------

