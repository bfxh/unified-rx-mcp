//! repo_map（S102）契约测试：图/个人化偏置/预算裁剪。

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rxrs::json::Value;
use rxrs::repomap;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-repomap-test-{}-{}", tag, n));
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

fn get_i128(v: &Value, k: &str) -> i128 {
    match v.get(k) {
        Some(Value::Int(i)) => *i,
        other => panic!("{} 应为 Int，实得 {:?}", k, other),
    }
}

fn get_str<'a>(v: &'a Value, k: &str) -> &'a str {
    match v.get(k) {
        Some(Value::Str(s)) => s,
        other => panic!("{} 应为 Str，实得 {:?}", k, other),
    }
}

fn map_lines(v: &Value) -> Vec<String> {
    get_str(v, "map").lines().map(|s| s.to_string()).collect()
}

fn run(root: &Path, focus: &[&str], budget: usize) -> Value {
    let f: Vec<String> = focus.iter().map(|s| s.to_string()).collect();
    let v = repomap::repo_map(root, &f, budget, 200).unwrap();
    assert!(v.get("error").is_none(), "不应报错: {:?}", v);
    v
}

#[test]
fn references_drive_ranking() {
    let td = TempDir::new("rank");
    write_rel(td.path(), "lib_a.py", "def alpha():\n    pass\n");
    write_rel(td.path(), "lib_b.py", "def beta():\n    pass\n");
    write_rel(td.path(), "user.py", "alpha()\nalpha()\nalpha()\n");
    let v = run(td.path(), &[], 4000);
    let lines = map_lines(&v);
    assert!(!lines.is_empty());
    assert!(lines[0].contains("alpha"), "被引用者应排第一: {:?}", lines);
}

#[test]
fn focus_biases_ranking() {
    let td = TempDir::new("focus");
    write_rel(td.path(), "lib_a.py", "def alpha():\n    pass\n");
    write_rel(td.path(), "lib_b.py", "def beta():\n    pass\n");
    write_rel(td.path(), "user.py", "alpha()\nalpha()\nalpha()\n");
    let v = run(td.path(), &["lib_b"], 4000);
    let lines = map_lines(&v);
    assert!(lines[0].contains("beta"),
            "聚焦 lib_b 后 beta 应压过被引用三次的 alpha: {:?}", lines);
}

#[test]
fn budget_truncates_and_reports() {
    let td = TempDir::new("budget");
    let mut body = String::new();
    for i in 0..200 {
        body.push_str(&format!("def fn_{:03}():\n    pass\n", i));
    }
    write_rel(td.path(), "many.py", &body);
    let v = run(td.path(), &[], 60); // 60 token ≈ 240 字符
    assert!(get_i128(&v, "defs_total") >= 200);
    assert!(get_i128(&v, "defs_shown") < get_i128(&v, "defs_total"));
    assert_eq!(get_str(&v, "engine"), "pagerank");
    match v.get("truncated") {
        Some(Value::Bool(true)) => {}
        other => panic!("truncated 应为 true，实得 {:?}", other),
    }
    assert!(get_str(&v, "map").len() <= 60 * 4 + 64, "预算应生效（留一行余量）");
}

#[test]
fn four_language_symbols_collected() {
    let td = TempDir::new("langs");
    write_rel(td.path(), "a.py", "def py_fn():\n    pass\n");
    write_rel(td.path(), "b.rs", "pub fn rust_fn() {}\n");
    write_rel(td.path(), "c.go", "func goFn() {}\n");
    write_rel(td.path(), "d.js", "function jsFn() {}\n");
    let v = run(td.path(), &[], 4000);
    let map = get_str(&v, "map");
    for want in ["py_fn", "rust_fn", "goFn", "jsFn"] {
        assert!(map.contains(want), "四语言符号都应入图（缺 {}）: {}", want, map);
    }
}

#[test]
fn empty_or_notdir_is_reported() {
    let td = TempDir::new("empty");
    let v = repomap::repo_map(&td.path().join("nope"), &[], 100, 200).unwrap();
    assert!(v.get("error").is_some());
}
