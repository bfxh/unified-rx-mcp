//! code_search Rust 原生实现的契约测试（S80）。
//! 直接打 rxrs::search 库函数；walk 顺序判别用 201 文件法（先文件后目录 vs
//! 字母序混排 DFS 在 200 上限下结论相反）。

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rxrs::json::Value;
use rxrs::search;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-search-test-{}-{}", tag, n));
        fs::create_dir_all(&p).unwrap();
        TempDir(p)
    }
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn write_rel(root: &Path, rel: &str, content: &str) {
    let p = root.join(rel);
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(&p, content).unwrap();
}

fn get_str<'a>(v: &'a Value, k: &str) -> &'a str {
    match v.get(k) {
        Some(Value::Str(s)) => s,
        other => panic!("{} 应为字符串，实得 {:?}", k, other),
    }
}

fn get_i128(v: &Value, k: &str) -> i128 {
    match v.get(k) {
        Some(Value::Int(i)) => *i,
        other => panic!("{} 应为整数，实得 {:?}", k, other),
    }
}

fn get_f64(v: &Value, k: &str) -> f64 {
    match v.get(k) {
        Some(Value::Float(f)) => *f,
        other => panic!("{} 应为浮点，实得 {:?}", k, other),
    }
}

fn get_arr<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    match v.get(k) {
        Some(Value::Arr(a)) => a,
        other => panic!("{} 应为数组，实得 {:?}", k, other),
    }
}

#[test]
fn tokenize_camel_snake_and_whole_word() {
    let toks = search::tokenize("HTTPServer");
    for t in ["http", "server", "httpserver"] {
        assert!(toks.iter().any(|x| x == t), "HTTPServer 缺 {}，实得 {:?}", t, toks);
    }
    let toks = search::tokenize("auth_gate_sweep");
    for t in ["auth", "gate", "sweep", "auth_gate_sweep"] {
        assert!(toks.iter().any(|x| x == t), "snake 缺 {}，实得 {:?}", t, toks);
    }
    // 部件全为单字符 → 只剩整词小写
    assert_eq!(search::tokenize("V2F"), vec!["v2f".to_string()]);
}

#[test]
fn tokenize_cjk_bigram_and_stopwords() {
    let toks = search::tokenize("授权门检查");
    for t in ["授权", "权门", "门检", "检查", "授权门检查"] {
        assert!(toks.iter().any(|x| x == t), "CJK 缺 {}，实得 {:?}", t, toks);
    }
    // 停用词与单字符全部过滤
    assert!(search::tokenize("the server").iter().all(|t| t != "the"));
    assert!(search::tokenize("it is a test").iter().all(|t| t == "test"));
    assert!(search::tokenize("a b cd").iter().all(|t| t == "cd"));
}

#[test]
fn raw_terms_min_lengths() {
    let rts = search::raw_terms("load_module_defs ab 字 中文词");
    assert!(rts.iter().any(|x| x == "load_module_defs"));
    assert!(rts.iter().any(|x| x == "中文词"));
    assert!(rts.iter().all(|x| x != "ab" && x != "字"));
}

#[test]
fn mixed_query_hits_right_file() {
    let td = TempDir::new("mixed");
    write_rel(td.path(), "a.py", "fn compute_damage() -> i32 { 42 }\n");
    write_rel(td.path(), "b.py", "fn heal(amount: i32) {}\n");
    let res = search::code_search(td.path(), "damage 计算", 10);
    assert!(res.get("error").is_none(), "不应报错");
    assert!(get_i128(&res, "total") >= 1);
    let hit = &get_arr(&res, "hits")[0];
    assert!(get_str(hit, "file").ends_with("a.py"));
    assert_eq!(get_i128(hit, "line"), 1);
    assert!(get_f64(hit, "score") > 0.0);
}

#[test]
fn line_rerank_prefers_exact_symbol() {
    let td = TempDir::new("rerank");
    // 前 4 行只有散件 token，第 5 行有精确符号原文 → raw boost +6 应压过散件行
    write_rel(td.path(), "m.py", "auth gate here\nx2\ny2\nz2\nlet m = AUTH_GATE_SWEEP_MARKER;\n");
    let res = search::code_search(td.path(), "auth_gate_sweep", 10);
    assert_eq!(get_i128(&res, "total"), 1);
    assert_eq!(get_i128(&get_arr(&res, "hits")[0], "line"), 5);
}

#[test]
fn not_a_dir_is_error_object() {
    let td = TempDir::new("notdir");
    let res = search::code_search(&td.path().join("nope"), "q", 10);
    match res.get("error") {
        Some(Value::Str(e)) => assert!(e.contains("不是目录"), "实得 {}", e),
        other => panic!("应为 error 对象，实得 {:?}", other),
    }
}

#[test]
fn empty_query_yields_zero_hits() {
    let td = TempDir::new("emptyq");
    write_rel(td.path(), "a.py", "fn something() {}\n");
    let res = search::code_search(td.path(), "", 10);
    assert_eq!(get_i128(&res, "total"), 0);
}

#[test]
fn k_caps_results() {
    let td = TempDir::new("kcap");
    for i in 0..5 {
        write_rel(td.path(), &format!("f{}.py", i), "def widget_part(): pass\n");
    }
    let res = search::code_search(td.path(), "widget part", 2);
    assert_eq!(get_i128(&res, "total"), 2);
}

#[test]
fn walk_files_before_dirs_under_cap() {
    // 201 个候选：a.py + z.py + sub/199 个。若按字母序混排 DFS（sub 排在 z.py
    // 前），名额全被 sub 烧光、z.py 落榜；每层先文件后目录则 z.py 必在语料。
    let td = TempDir::new("walkorder");
    write_rel(td.path(), "a.py", "roota alphamarker_base\n");
    write_rel(td.path(), "z.py", "zeta_zfind_fn\n");
    for i in 0..199 {
        write_rel(td.path(), &format!("sub/f{:03}.py", i), "def filler(): pass\n");
    }
    let res = search::code_search(td.path(), "zeta_zfind_fn", 10);
    assert_eq!(get_i128(&res, "total"), 1, "z.py 必须入选语料");
    assert!(get_str(&get_arr(&res, "hits")[0], "file").ends_with("z.py"));
}

#[test]
fn skips_git_dir_and_txt() {
    let td = TempDir::new("skip");
    write_rel(td.path(), ".git/x.py", "uniquemarker123\n");
    write_rel(td.path(), "d.txt", "uniquemarker123\n");
    let res = search::code_search(td.path(), "uniquemarker123", 10);
    assert_eq!(get_i128(&res, "total"), 0, "跳过目录与非收录扩展名都不得命中");
}
