//! LSE (Learning to Self-Evolve) engine for unified-rx-mcp.
//!
//! Implements the three core ideas from "Learning to Self-Evolve"
//! (Mila & Snowflake, arXiv:2603.18620) as a zero-dependency Rust engine:
//!
//! 1. **Delta reward** — utility scores that only credit improvement deltas,
//!    used by `lesson_recall` (lesson utility) and `std_check` (rule weights).
//! 2. **UCB tree search** — upper-confidence-bound branch selection with
//!    backtracking, used by `bug_locate` to explore causal branches.
//! 3. **Cross-model experience** — experiences tagged with model/context
//!    fingerprints, reusable across editors and models.
//!
//! State is persisted as JSON at `~/.unified-rx/lse-state.json` (single-user
//! local store). CLI protocol: `stdin {cmd, payload} -> stdout {ok, result}`.

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

// ---------------------------------------------------------------------------
// State model
// ---------------------------------------------------------------------------

/// One evolutionary lesson: a recalled lesson with a utility score.
#[derive(Clone, Debug)]
pub struct Lesson {
    pub id: String,
    pub utility: f64,
    pub recall_count: u64,
    pub archived: bool,
}

/// Adaptive rule weight (std_check rules).
#[derive(Clone, Debug)]
pub struct RuleWeight {
    pub weight: f64,
    pub adopted: u64,
    pub ignored: u64,
}

/// A tree-search node (bug_locate causal branch).
#[derive(Clone, Debug)]
pub struct TreeNode {
    pub id: String,
    pub reward_sum: f64,
    pub visits: u64,
    pub children: Vec<String>,
}

/// A cross-model experience card.
#[derive(Clone, Debug)]
pub struct Experience {
    pub id: String,
    pub model_fingerprint: String,
    pub context_hash: String,
    pub delta_score: f64,
    pub summary: String,
}

/// Full persistent engine state.
#[derive(Clone, Debug, Default)]
pub struct EngineState {
    pub lessons: BTreeMap<String, Lesson>,
    pub rules: BTreeMap<String, RuleWeight>,
    pub tree: BTreeMap<String, TreeNode>,
    pub experiences: BTreeMap<String, Experience>,
}

impl EngineState {
    pub fn new() -> Self {
        Self::default()
    }

    /// Load state from `~/.unified-rx/lse-state.json` (missing -> empty state).
    pub fn load() -> Self {
        let path = Self::state_path();
        match fs::read_to_string(&path) {
            Ok(s) => Self::from_json(&s).unwrap_or_default(),
            Err(_) => Self::default(),
        }
    }

    /// Persist state to `~/.unified-rx/lse-state.json` (best-effort, creates dir).
    /// 原子写（tmp + rename）：parallel/to_thread 并发子进程场景防撕裂/丢更新（security review MEDIUM）。
    pub fn save(&self) {
        let path = Self::state_path();
        if let Some(dir) = path.parent() {
            let _ = fs::create_dir_all(dir);
        }
        if let Ok(s) = self.to_json() {
            let tmp = path.with_extension("json.tmp");
            let _ = fs::write(&tmp, &s);
            let _ = fs::rename(&tmp, &path);
        }
    }

    fn state_path() -> PathBuf {
        // LSE_STATE 环境变量覆盖（测试隔离用）；缺省 ~/.unified-rx/lse-state.json
        if let Ok(override_path) = std::env::var("LSE_STATE") {
            if !override_path.trim().is_empty() {
                return PathBuf::from(override_path);
            }
        }
        let home = std::env::var("USERPROFILE")
            .or_else(|_| std::env::var("HOME"))
            .unwrap_or_else(|_| ".".to_string());
        Path::new(&home).join(".unified-rx").join("lse-state.json")
    }

    // --- serialization (hand-rolled minimal JSON: object/array/string/number) --

    pub fn to_json(&self) -> Result<String, String> {
        let mut lessons = Vec::new();
        for (id, l) in &self.lessons {
            lessons.push(format!(
                "\"{}\":{{\"utility\":{},\"recall\":{},\"archived\":{}}}",
                json_escape(id),
                fmt_f64(l.utility),
                l.recall_count,
                l.archived
            ));
        }
        let mut rules = Vec::new();
        for (id, r) in &self.rules {
            rules.push(format!(
                "\"{}\":{{\"weight\":{},\"adopted\":{},\"ignored\":{}}}",
                json_escape(id),
                fmt_f64(r.weight),
                r.adopted,
                r.ignored
            ));
        }
        let mut tree = Vec::new();
        for (id, t) in &self.tree {
            let children = t
                .children
                .iter()
                .map(|c| format!("\"{}\"", json_escape(c)))
                .collect::<Vec<_>>()
                .join(",");
            tree.push(format!(
                "\"{}\":{{\"reward\":{},\"visits\":{},\"children\":[{}]}}",
                json_escape(id),
                fmt_f64(t.reward_sum),
                t.visits,
                children
            ));
        }
        let mut exp = Vec::new();
        for (id, e) in &self.experiences {
            exp.push(format!(
                "\"{}\":{{\"model\":\"{}\",\"ctx\":\"{}\",\"delta\":{},\"summary\":\"{}\"}}",
                json_escape(id),
                json_escape(&e.model_fingerprint),
                json_escape(&e.context_hash),
                fmt_f64(e.delta_score),
                json_escape(&e.summary)
            ));
        }
        Ok(format!(
            "{{\"lessons\":{{{}}},\"rules\":{{{}}},\"tree\":{{{}}},\"experiences\":{{{}}}}}",
            lessons.join(","),
            rules.join(","),
            tree.join(","),
            exp.join(",")
        ))
    }

    pub fn from_json(s: &str) -> Result<Self, String> {
        let mut state = Self::default();
        // Extremely small parser: find sections by key then parse key:"{...}"
        if let Some(sec) = json_section(s, "lessons") {
            for pair in json_pairs(&sec) {
                let (id, inner) = pair;
                let lid = id.clone();
                state.lessons.insert(
                    id,
                    Lesson {
                        utility: json_num_field(&inner, "utility").unwrap_or(0.5),
                        recall_count: json_u64_field(&inner, "recall").unwrap_or(0),
                        archived: json_bool_field(&inner, "archived").unwrap_or(false),
                        id: lid,
                    },
                );
            }
        }
        if let Some(sec) = json_section(s, "rules") {
            for pair in json_pairs(&sec) {
                let (id, inner) = pair;
                state.rules.insert(
                    id,
                    RuleWeight {
                        weight: json_num_field(&inner, "weight").unwrap_or(1.0),
                        adopted: json_u64_field(&inner, "adopted").unwrap_or(0),
                        ignored: json_u64_field(&inner, "ignored").unwrap_or(0),
                    },
                );
            }
        }
        if let Some(sec) = json_section(s, "tree") {
            for pair in json_pairs(&sec) {
                let (id, inner) = pair;
                let tid = id.clone();
                state.tree.insert(
                    id,
                    TreeNode {
                        reward_sum: json_num_field(&inner, "reward").unwrap_or(0.0),
                        visits: json_u64_field(&inner, "visits").unwrap_or(0),
                        children: json_str_array_field(&inner, "children"),
                        id: tid,
                    },
                );
            }
        }
        if let Some(sec) = json_section(s, "experiences") {
            for pair in json_pairs(&sec) {
                let (id, inner) = pair;
                let eid = id.clone();
                state.experiences.insert(
                    id,
                    Experience {
                        model_fingerprint: json_str_field(&inner, "model").unwrap_or_default(),
                        context_hash: json_str_field(&inner, "ctx").unwrap_or_default(),
                        delta_score: json_num_field(&inner, "delta").unwrap_or(0.0),
                        summary: json_str_field(&inner, "summary").unwrap_or_default(),
                        id: eid,
                    },
                );
            }
        }
        Ok(state)
    }
}

// ---------------------------------------------------------------------------
// Delta reward operations
// ---------------------------------------------------------------------------

/// Update a lesson's utility with a delta reward: `utility += delta` clamped to
/// [0, 1]. Lessons below `archive_threshold` are archived (降权).
pub fn delta_update_lesson(
    state: &mut EngineState,
    id: &str,
    delta: f64,
    archive_threshold: f64,
) -> Lesson {
    let lesson = state
        .lessons
        .entry(id.to_string())
        .or_insert_with(|| Lesson {
            id: id.to_string(),
            utility: 0.5,
            recall_count: 0,
            archived: false,
        });
    lesson.recall_count += 1;
    lesson.utility = (lesson.utility + delta).clamp(0.0, 1.0);
    if lesson.utility < archive_threshold {
        lesson.archived = true;
    }
    lesson.clone()
}

/// Update a rule's adaptive weight: `weight += delta` clamped to [0, 3].
/// `adopted=true` credits improvement; `ignored` penalizes.
pub fn delta_update_rule(
    state: &mut EngineState,
    id: &str,
    delta: f64,
    adopted: bool,
) -> RuleWeight {
    let rule = state
        .rules
        .entry(id.to_string())
        .or_insert_with(|| RuleWeight {
            weight: 1.0,
            adopted: 0,
            ignored: 0,
        });
    if adopted {
        rule.adopted += 1;
        rule.weight = (rule.weight + delta).clamp(0.0, 3.0);
    } else {
        rule.ignored += 1;
        rule.weight = (rule.weight - delta.abs()).clamp(0.0, 3.0);
    }
    rule.clone()
}

// ---------------------------------------------------------------------------
// UCB tree search
// ---------------------------------------------------------------------------

/// UCB1 selection: pick the child maximizing `reward/visits + c * sqrt(ln(parent)/visits)`.
/// Unexplored children get infinite score (must be visited at least once).
pub fn ucb_select(
    state: &mut EngineState,
    parent_id: &str,
    children: &[String],
    explore_c: f64,
) -> Option<String> {
    if children.is_empty() {
        return None;
    }
    let parent_visits = state
        .tree
        .get(parent_id)
        .map(|n| n.visits.max(1))
        .unwrap_or(1);
    let mut best: Option<(f64, String)> = None;
    for child in children {
        let node = state.tree.get(child);
        let (reward, visits) = match node {
            Some(n) => (n.reward_sum, n.visits),
            None => (0.0, 0),
        };
        let score = if visits == 0 {
            f64::INFINITY
        } else {
            reward / visits as f64
                + explore_c * (parent_visits as f64).ln().sqrt() / (visits as f64).sqrt()
        };
        if best.as_ref().is_none_or(|b| score > b.0) {
            best = Some((score, child.clone()));
        }
    }
    best.map(|(_, id)| id)
}

/// Register a node (idempotent) and attach it as a child of `parent` if new.
pub fn ucb_register(state: &mut EngineState, parent: &str, id: &str) {
    if !parent.is_empty() {
        state
            .tree
            .entry(parent.to_string())
            .or_insert_with(|| TreeNode {
                id: parent.to_string(),
                reward_sum: 0.0,
                visits: 0,
                children: Vec::new(),
            });
    }
    let exists = state.tree.contains_key(id);
    state
        .tree
        .entry(id.to_string())
        .or_insert_with(|| TreeNode {
            id: id.to_string(),
            reward_sum: 0.0,
            visits: 0,
            children: Vec::new(),
        });
    if !exists && !parent.is_empty() {
        if let Some(p) = state.tree.get_mut(parent) {
            if !p.children.contains(&id.to_string()) {
                p.children.push(id.to_string());
            }
        }
    }
}

/// Record the outcome of visiting a node: `reward` in [-1, 1] (success = +1).
pub fn ucb_backprop(state: &mut EngineState, id: &str, reward: f64) {
    if let Some(n) = state.tree.get_mut(id) {
        n.visits += 1;
        n.reward_sum += reward.clamp(-1.0, 1.0);
    }
}

// ---------------------------------------------------------------------------
// Experience store
// ---------------------------------------------------------------------------

/// Store an experience card tagged with model/context fingerprints.
/// Returns its id (stable: context_hash+model for dedup, else random-ish).
pub fn experience_store(
    state: &mut EngineState,
    model: &str,
    context_hash: &str,
    delta_score: f64,
    summary: &str,
) -> String {
    // 压缩学习：经验卡片 summary 截断（防状态文件膨胀），context_hash 归一化
    let id = format!("{}-{}", context_hash, model);
    let exp = Experience {
        id: id.clone(),
        model_fingerprint: model.to_string(),
        context_hash: context_hash.to_string(),
        delta_score,
        summary: compress_summary(summary, 200),
    };
    state.experiences.insert(id.clone(), exp);
    id
}

/// 压缩学习：摘要截断至 200 字符（保留关键信息，去冗余）。
pub fn compress_summary(s: &str, max_len: usize) -> String {    if s.chars().count() <= max_len {
        return s.to_string();
    }
    let mut out: String = s.chars().take(max_len).collect();
    out.push('…');
    out
}

/// 压缩学习：lesson_recall 用查询（delta=0 会污染 recall_count，故独立命令）。
pub fn lesson_recall_query(state: &EngineState, id: &str) -> Option<Lesson> {
    state.lessons.get(id).cloned()
}

/// Find reusable experiences matching a context hash, sorted by delta_score desc.
pub fn experience_match(state: &EngineState, context_hash: &str, limit: usize) -> Vec<Experience> {
    let mut hits: Vec<Experience> = state
        .experiences
        .values()
        .filter(|e| e.context_hash == context_hash)
        .cloned()
        .collect();
    hits.sort_by(|a, b| {
        b.delta_score
            .partial_cmp(&a.delta_score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    hits.truncate(limit);
    hits
}

// ---------------------------------------------------------------------------
// JSON helpers (minimal, safe)
// ---------------------------------------------------------------------------

fn fmt_f64(v: f64) -> String {
    if !v.is_finite() {
        return "0".to_string(); // P2: 防 NaN/Inf 泄漏非法 JSON
    }
    if v.fract() == 0.0 {
        format!("{:.0}", v)
    } else {
        format!("{:.6}", v)
    }
}

fn json_escape(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

/// Extract the object body of a top-level section: `"key":{...}` -> inner.
fn json_section(s: &str, key: &str) -> Option<String> {
    let pat = format!("\"{}\":", key);
    let start = s.find(&pat)? + pat.len();
    let bytes = s.as_bytes();
    let mut depth = 0i32;
    let mut i = start;
    while i < bytes.len() {
        match bytes[i] {
            b'{' => depth += 1,
            b'}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(s[start..=i].to_string());
                }
            }
            _ => {}
        }
        i += 1;
    }
    None
}

/// Split `"id":{...}` pairs inside an object body.
/// String-aware: braces inside quoted values do not affect depth (P1-2 fix).
fn json_pairs(section: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let bytes = section.as_bytes();
    let mut i = 0;
    let n = bytes.len();
    while i < n {
        // skip to opening quote of key
        while i < n && bytes[i] != b'"' {
            i += 1;
        }
        if i >= n {
            break;
        }
        i += 1;
        let key_start = i;
        let mut escaped = false;
        while i < n {
            let c = bytes[i];
            if escaped {
                escaped = false;
            } else if c == b'\\' {
                escaped = true;
            } else if c == b'"' {
                break;
            }
            i += 1;
        }
        let key = json_unescape(&section[key_start..i]);
        i += 1;
        // skip to '{'
        while i < n && bytes[i] != b'{' {
            i += 1;
        }
        if i >= n {
            break;
        }
        let inner_start = i;
        let mut depth = 0i32;
        let mut in_str = false;
        let mut escaped = false;
        while i < n {
            let c = bytes[i];
            if in_str {
                if escaped {
                    escaped = false;
                } else if c == b'\\' {
                    escaped = true;
                } else if c == b'"' {
                    in_str = false;
                }
                i += 1;
                continue;
            }
            match c {
                b'"' => in_str = true,
                b'{' => depth += 1,
                b'}' => {
                    depth -= 1;
                    if depth == 0 {
                        out.push((key, section[inner_start..=i].to_string()));
                        i += 1;
                        break;
                    }
                }
                _ => {}
            }
            i += 1;
        }
    }
    out
}

fn json_num_field(inner: &str, key: &str) -> Option<f64> {
    let val = json_field(inner, key)?;
    val.parse::<f64>().ok()
}

fn json_u64_field(inner: &str, key: &str) -> Option<u64> {
    let val = json_field(inner, key)?;
    val.parse::<u64>().ok()
}

fn json_bool_field(inner: &str, key: &str) -> Option<bool> {
    json_field(inner, key).map(|v| v == "true")
}

fn json_str_field(inner: &str, key: &str) -> Option<String> {
    json_field(inner, key)
}

/// Parse a string array field `"key":[...]` with quote-aware element splitting
/// (P2 fix: elements containing `,` or `]` no longer break parsing).
fn json_str_array_field(inner: &str, key: &str) -> Vec<String> {
    // 手写解析（空格兼容 2026-08-13 修复）：`"key":[` 与 `"key": [` 都接受
    let pat1 = format!("\"{}\":[", key);
    let pat2 = format!("\"{}\": [", key);
    let (start_rel, pat_len) = match inner.find(&pat1) {
        Some(i) => (i, pat1.len()),
        None => match inner.find(&pat2) {
            Some(i) => (i, pat2.len()),
            None => return Vec::new(),
        },
    };
    let start = start_rel + pat_len;
    let bytes = inner.as_bytes();
    let mut i = start;
    let mut in_str = false;
    let mut escaped = false;
    while i < bytes.len() {
        let c = bytes[i];
        if in_str {
            if escaped {
                escaped = false;
            } else if c == b'\\' {
                escaped = true;
            } else if c == b'"' {
                in_str = false;
            }
        } else {
            match c {
                b'"' => in_str = true,
                b']' => break,
                _ => {}
            }
        }
        i += 1;
    }
    let body = &inner[start..i];
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut q = false;
    let mut esc = false;
    for c in body.chars() {
        if q {
            if esc {
                esc = false;
                cur.push(c);
            } else if c == '\\' {
                esc = true;
            } else if c == '"' {
                q = false;
            } else {
                cur.push(c);
            }
        } else {
            match c {
                '"' => q = true,
                ',' => {
                    let s = cur.trim();
                    if !s.is_empty() {
                        out.push(s.to_string());
                    }
                    cur.clear();
                }
                _ => cur.push(c),
            }
        }
    }
    let s = cur.trim();
    if !s.is_empty() {
        out.push(s.to_string());
    }
    out
}

/// Extract a scalar field value: `"key":value` (value until , or }).
/// String values are quote-aware (escapes honored) and unescaped (P1-1/3 fix).
fn json_field(inner: &str, key: &str) -> Option<String> {
    let pat = format!("\"{}\":", key);
    let start = inner.find(&pat)? + pat.len();
    let rest = inner[start..].trim_start();
    if rest.starts_with('"') {
        // string value: scan to closing quote honoring backslash escapes
        let bytes = rest.as_bytes();
        let mut i = 1;
        let mut escaped = false;
        while i < bytes.len() {
            let c = bytes[i];
            if escaped {
                escaped = false;
            } else if c == b'\\' {
                escaped = true;
            } else if c == b'"' {
                return Some(json_unescape(&rest[1..i]));
            }
            i += 1;
        }
        return None; // unterminated string
    }
    let mut end = rest.len();
    for (idx, c) in rest.char_indices() {
        if c == ',' || c == '}' {
            end = idx;
            break;
        }
    }
    Some(rest[..end].trim().to_string())
}

/// Unescape JSON string escapes (P1-3 fix): \" -> " and \\ -> \.
fn json_unescape(s: &str) -> String {
    if !s.contains('\\') {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars();
    while let Some(c) = chars.next() {
        if c == '\\' {
            match chars.next() {
                Some('"') => out.push('"'),
                Some('\\') => out.push('\\'),
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some(other) => {
                    out.push('\\');
                    out.push(other);
                }
                None => out.push('\\'),
            }
        } else {
            out.push(c);
        }
    }
    out
}

// ---------------------------------------------------------------------------
// CLI protocol
// ---------------------------------------------------------------------------

/// Handle one JSON command line; returns the JSON response.
pub fn handle_command(line: &str, state: &mut EngineState) -> String {
    let cmd = json_str_field(line, "cmd").unwrap_or_default();
    let payload = json_section(line, "payload").unwrap_or_default();
    match cmd.as_str() {
        "delta_update" => {
            let kind = json_str_field(&payload, "kind").unwrap_or_default();
            let id = json_str_field(&payload, "id").unwrap_or_default();
            let delta = json_num_field(&payload, "delta").unwrap_or(0.0);
            match kind.as_str() {
                "lesson" => {
                    let threshold = json_num_field(&payload, "threshold").unwrap_or(0.1);
                    let l = delta_update_lesson(state, &id, delta, threshold);
                    format!(
                        "{{\"ok\":true,\"result\":{{\"id\":\"{}\",\"utility\":{},\"recall\":{},\"archived\":{}}}}}",
                        json_escape(&l.id), fmt_f64(l.utility), l.recall_count, l.archived
                    )
                }
                "rule" => {
                    let adopted = json_bool_field(&payload, "adopted").unwrap_or(true);
                    let r = delta_update_rule(state, &id, delta, adopted);
                    format!(
                        "{{\"ok\":true,\"result\":{{\"id\":\"{}\",\"weight\":{},\"adopted\":{},\"ignored\":{}}}}}",
                        json_escape(&id), fmt_f64(r.weight), r.adopted, r.ignored
                    )
                }
                _ => format!(
                    "{{\"ok\":false,\"error\":\"unknown delta_update kind: {}\"}}",
                    json_escape(&kind)
                ),
            }
        }
        "ucb_select" => {
            let parent = json_str_field(&payload, "parent").unwrap_or_default();
            let children = json_str_array_field(&payload, "children");
            let c = json_num_field(&payload, "c").unwrap_or(1.41);
            for ch in &children {
                ucb_register(state, &parent, ch);
            }
            match ucb_select(state, &parent, &children, c) {
                Some(sel) => format!(
                    "{{\"ok\":true,\"result\":{{\"selected\":\"{}\"}}}}",
                    json_escape(&sel)
                ),
                None => "{\"ok\":false,\"error\":\"no children\"}".to_string(),
            }
        }
        "ucb_backprop" => {
            let id = json_str_field(&payload, "id").unwrap_or_default();
            let reward = json_num_field(&payload, "reward").unwrap_or(0.0);
            ucb_backprop(state, &id, reward);
            format!(
                "{{\"ok\":true,\"result\":{{\"id\":\"{}\",\"reward\":{}}}}}",
                json_escape(&id),
                fmt_f64(reward)
            )
        }
        "experience_store" => {
            let model = json_str_field(&payload, "model").unwrap_or_default();
            let ctx = json_str_field(&payload, "ctx").unwrap_or_default();
            let delta = json_num_field(&payload, "delta").unwrap_or(0.0);
            let summary = json_str_field(&payload, "summary").unwrap_or_default();
            let id = experience_store(state, &model, &ctx, delta, &summary);
            format!(
                "{{\"ok\":true,\"result\":{{\"id\":\"{}\"}}}}",
                json_escape(&id)
            )
        }
        "experience_match" => {
            let ctx = json_str_field(&payload, "ctx").unwrap_or_default();
            let limit = json_u64_field(&payload, "limit").unwrap_or(5) as usize;
            let hits = experience_match(state, &ctx, limit);
            let items = hits
                .iter()
                .map(|e| format!(
                    "{{\"id\":\"{}\",\"model\":\"{}\",\"ctx\":\"{}\",\"delta\":{},\"summary\":\"{}\"}}",
                    json_escape(&e.id), json_escape(&e.model_fingerprint), json_escape(&e.context_hash),
                    fmt_f64(e.delta_score), json_escape(&e.summary)
                ))
                .collect::<Vec<_>>()
                .join(",");
            format!("{{\"ok\":true,\"result\":{{\"items\":[{}]}}}}", items)
        }
        "state_get" => match state.to_json() {
            Ok(inner) => format!("{{\"ok\":true,\"result\":{}}}", inner),
            Err(e) => format!("{{\"ok\":false,\"error\":\"{}\"}}", e),
        },
        "lesson_recall" => {
            // 查询单条教训（不触发 recall_count++，防查询污染枢纽信号）
            let id = json_str_field(&payload, "id").unwrap_or_default();
            match lesson_recall_query(state, &id) {
                Some(l) => format!(
                    "{{\"ok\":true,\"result\":{{\"id\":\"{}\",\"utility\":{},\"recall\":{},\"archived\":{}}}}}",
                    json_escape(&l.id), fmt_f64(l.utility), l.recall_count, l.archived
                ),
                None => format!(
                    "{{\"ok\":false,\"error\":\"lesson not found: {}\"}}",
                    json_escape(&id)
                ),
            }
        }
        _ => format!(
            "{{\"ok\":false,\"error\":\"unknown cmd: {}\"}}",
            json_escape(&cmd)
        ),
    }
}

/// Main loop: read lines from stdin, respond per line, persist state each command.
pub fn run_cli() -> io::Result<()> {
    let mut state = EngineState::load();
    let stdin = io::stdin();
    let mut input = String::new();
    loop {
        input.clear();
        match stdin.read_line(&mut input) {
            Ok(0) => break, // EOF
            Ok(_) => {}
            Err(_) => break,
        }
        let line = input.trim();
        if line.is_empty() {
            continue;
        }
        let resp = handle_command(line, &mut state);
        state.save();
        println!("{}", resp);
        io::stdout().flush()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn delta_update_lesson_archive() {
        let mut s = EngineState::new();
        let l = delta_update_lesson(&mut s, "bug:null-deref", -0.5, 0.1);
        assert!(l.archived);
        assert!(l.utility < 0.1);
        let l2 = delta_update_lesson(&mut s, "bug:null-deref", 0.3, 0.1);
        assert!(l2.recall_count == 2);
    }

    #[test]
    fn delta_update_rule_adopt_ignore() {
        let mut s = EngineState::new();
        let r = delta_update_rule(&mut s, "magic_number", 0.2, true);
        assert_eq!(r.weight, 1.2);
        let r2 = delta_update_rule(&mut s, "magic_number", 0.5, false);
        assert!((r2.weight - 0.7).abs() < 1e-9);
        assert_eq!(r2.ignored, 1);
    }

    #[test]
    fn ucb_explores_unvisited_first() {
        let mut s = EngineState::new();
        s.tree.insert(
            "root".into(),
            TreeNode {
                id: "root".into(),
                reward_sum: 0.0,
                visits: 2,
                children: vec![],
            },
        );
        let picked = ucb_select(&mut s, "root", &["a".into(), "b".into()], 1.41);
        assert!(picked.is_some());
        // unvisited -> infinite score; either a or b is fine
    }

    #[test]
    fn ucb_prefers_high_reward_after_visits() {
        let mut s = EngineState::new();
        s.tree.insert(
            "root".into(),
            TreeNode {
                id: "root".into(),
                reward_sum: 0.0,
                visits: 10,
                children: vec![],
            },
        );
        s.tree.insert(
            "good".into(),
            TreeNode {
                id: "good".into(),
                reward_sum: 9.0,
                visits: 10,
                children: vec![],
            },
        );
        s.tree.insert(
            "bad".into(),
            TreeNode {
                id: "bad".into(),
                reward_sum: 1.0,
                visits: 10,
                children: vec![],
            },
        );
        let picked = ucb_select(&mut s, "root", &["good".into(), "bad".into()], 0.0).unwrap();
        assert_eq!(picked, "good");
    }

    #[test]
    fn experience_store_and_match() {
        let mut s = EngineState::new();
        experience_store(&mut s, "model-a", "ctx:rust:bevy", 0.8, "fix ui");
        experience_store(&mut s, "model-b", "ctx:rust:bevy", 0.2, "fix ui alt");
        experience_store(&mut s, "model-c", "ctx:python", 0.9, "other");
        let hits = experience_match(&s, "ctx:rust:bevy", 5);
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].model_fingerprint, "model-a"); // delta desc
    }

    #[test]
    fn roundtrip_persist() {
        let mut s = EngineState::new();
        delta_update_rule(&mut s, "ui_hardcode", 0.3, true);
        experience_store(&mut s, "m1", "ctx1", 0.5, "s1");
        let json = s.to_json().unwrap();
        let back = EngineState::from_json(&json).unwrap();
        assert_eq!(back.rules["ui_hardcode"].weight, 1.3);
        assert_eq!(back.experiences.len(), 1);
        assert_eq!(
            back.experiences.values().next().unwrap().context_hash,
            "ctx1"
        );
    }

    #[test]
    fn cli_delta_command() {
        let mut s = EngineState::new();
        let resp = handle_command(
            r#"{"cmd":"delta_update","payload":{"kind":"lesson","id":"L1","delta":-0.5}}"#,
            &mut s,
        );
        assert!(resp.contains("\"archived\":true"));
    }

    // ── 压缩学习 + 枢纽优先（Nature Communications 启发）──
    #[test]
    fn lesson_recall_query_does_not_pollute_recall() {
        // 查询不得触发 recall_count++（delta=0 查询会污染枢纽信号）
        let mut s = EngineState::new();
        delta_update_lesson(&mut s, "L1", 0.2, 0.1); // recall=1
        let r1 = lesson_recall_query(&s, "L1").unwrap();
        let r2 = lesson_recall_query(&s, "L1").unwrap();
        assert_eq!(r1.recall_count, 1);
        assert_eq!(r2.recall_count, 1, "查询不增 recall");
    }

    #[test]
    fn cli_lesson_recall_command() {
        let mut s = EngineState::new();
        delta_update_lesson(&mut s, "L1", 0.2, 0.1);
        let resp = handle_command(r#"{"cmd":"lesson_recall","payload":{"id":"L1"}}"#, &mut s);
        assert!(resp.contains("\"ok\":true"), "{}", resp);
        assert!(resp.contains("\"recall\":1"), "{}", resp);
        let miss = handle_command(r#"{"cmd":"lesson_recall","payload":{"id":"NOPE"}}"#, &mut s);
        assert!(miss.contains("\"ok\":false"), "{}", miss);
    }

    #[test]
    fn experience_summary_compressed() {
        // 压缩学习：长 summary 截断至 200 字符 + 省略号
        let mut s = EngineState::new();
        let long = "x".repeat(500);
        experience_store(&mut s, "m1", "c1", -0.2, &long);
        let e = s.experiences.values().next().unwrap();
        assert!(e.summary.chars().count() <= 201, "压缩后长度: {}", e.summary.chars().count());
        assert!(e.summary.ends_with('…'), "压缩需省略号标记");
        // 短 summary 原样保留
        experience_store(&mut s, "m2", "c2", 0.5, "short ok");
        let short = s.experiences.values().find(|e| e.model_fingerprint == "m2").unwrap();
        assert_eq!(short.summary, "short ok");
    }

    // ── P1/P2 回归：JSON 解析健壮性 ─────────────────────────
    #[test]
    fn p1_roundtrip_string_with_comma_and_brace() {
        // summary 含 ASCII 逗号 + 未配对 }：必须完整往返（P1-1/2）
        let mut s = EngineState::new();
        experience_store(&mut s, "m1", "c1", 0.5, "fix ui, add } tests");
        let json = s.to_json().unwrap();
        let back = EngineState::from_json(&json).unwrap();
        assert_eq!(back.experiences.len(), 1, "条目不得丢失");
        let e = back.experiences.values().next().unwrap();
        assert_eq!(e.summary, "fix ui, add } tests");
        assert_eq!(e.context_hash, "c1");
        assert_eq!(e.delta_score, 0.5);
    }

    #[test]
    fn p1_roundtrip_backslash_does_not_double() {
        // Windows 路径反斜杠不得渐进翻倍（P1-3）
        let mut s = EngineState::new();
        experience_store(&mut s, "m2", "c2", 0.1, "C:\\Users\\foo\\bar");
        let json1 = s.to_json().unwrap();
        let back1 = EngineState::from_json(&json1).unwrap();
        assert_eq!(
            back1.experiences.values().next().unwrap().summary,
            "C:\\Users\\foo\\bar"
        );
        let json2 = back1.to_json().unwrap();
        assert_eq!(json1, json2, "load→save 必须幂等");
    }

    #[test]
    fn p2_nan_injection_sanitized() {
        // NaN 注入 → 输出不得含非法 JSON 字面量
        let mut s = EngineState::new();
        let lesson = Lesson {
            id: "LNaN".into(),
            utility: f64::NAN,
            recall_count: 1,
            archived: false,
        };
        s.lessons.insert("LNaN".into(), lesson);
        let json = s.to_json().unwrap();
        assert!(!json.contains(":NaN"), "不得输出 NaN 值: {json}");
        assert!(!json.contains(":Infinity"), "不得输出 Infinity 值: {json}");
        // 且能正常往返（NaN 被钳为 0）
        let back = EngineState::from_json(&json).unwrap();
        assert_eq!(back.lessons["LNaN"].utility, 0.0);
    }

    #[test]
    fn p2_children_array_with_special_chars() {
        // children 元素含 ] 与 , 不得错位（P2-5）
        let mut s = EngineState::new();
        ucb_register(&mut s, "root", "a]b");
        ucb_register(&mut s, "root", "c,d");
        ucb_register(&mut s, "root", "plain");
        let json = s.to_json().unwrap();
        let back = EngineState::from_json(&json).unwrap();
        let children = back.tree["root"].children.clone();
        assert_eq!(children.len(), 3);
        assert!(children.contains(&"a]b".to_string()));
        assert!(children.contains(&"c,d".to_string()));
        assert!(children.contains(&"plain".to_string()));
    }
}
