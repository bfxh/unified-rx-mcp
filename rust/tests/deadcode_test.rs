//! deadcode 原生化契约测试（S135）：引用面/PEP 豁免/装饰器/字符串嫌疑/排序。
//! 夹具与 tools/ide_deadcode.py 的 Python 版对照实验同构（八字段逐字节一致）。

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rxrs::deadcode;
use rxrs::json::Value;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-deadcode-test-{}-{}", tag, n));
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
        other => panic!("{} 应为整数，实得 {:?}", k, other),
    }
}

fn names_of(v: &Value, key: &str) -> Vec<String> {
    match v.get(key) {
        Some(Value::Arr(a)) => a
            .iter()
            .map(|e| match e.get("name") {
                Some(Value::Str(s)) => s.clone(),
                other => panic!("name 应为字符串: {:?}", other),
            })
            .collect(),
        other => panic!("{} 应为数组: {:?}", key, other),
    }
}

fn fixture(root: &Path) {
    write_rel(root, "pkg/a.py", &conv(&[
        "import os", "", "",
        "def used_by_b():", "    return 1", "", "",
        "def totally_orphan():", "    return 2", "", "",
        "def dispatched_by_name():", "    return 3", "", "",
        "class Widget:", "    def _private_unused(self):", "        return 4", "",
        "    def public_api(self):", "        return 5", "",
        "    def __dunder__(self):", "        return 6", "", "",
        "@decorator", "def framework_registered():", "    return 7", "", "",
        "def outer():", "    def nested():", "        return 8", "    return nested", "", "",
        "async def async_orphan():", "    return 9", "",
    ]));
    write_rel(root, "pkg/b.py", &conv(&[
        "from pkg.a import used_by_b", "", "",
        "def call_it():", "    return used_by_b()", "", "",
        "marker = \"dispatched_by_name\"", "",
    ]));
    write_rel(root, "pkg/broken.py", "def broken(:\n    pass\n");
    write_rel(root, "conftest.py", "def pytest_configure(config):\n    pass\n");
    write_rel(root, "tests/test_x.py", "def test_something():\n    assert 1\n");
    write_rel(root, "pkg/c.py", &conv(&[
        "import pkg.a as m", "", "",
        "def use_attr():", "    return m.used_by_b", "",
    ]));
}

fn conv(lines: &[&str]) -> String {
    lines.join("\n")
}

#[test]
fn s135_dead_and_suspect_semantics() {
    let d = TempDir::new("dc");
    fixture(d.path());
    let res = deadcode::dead_code_scan(d.path(), 2000, 200, false);
    assert!(res.get("error").is_none(), "{:?}", res);

    assert_eq!(get_i128(&res, "files_scanned"), 5);
    assert_eq!(get_i128(&res, "defs_total"), 12);
    assert_eq!(get_i128(&res, "dead_count"), 7);
    assert_eq!(get_i128(&res, "exempted_decorated"), 1);
    assert_eq!(get_i128(&res, "exempted_pytest_entry"), 2);
    assert!(matches!(res.get("truncated"), Some(Value::Bool(false))));

    let dead = names_of(&res, "dead");
    for expect in ["Widget", "_private_unused", "async_orphan", "call_it",
                   "outer", "totally_orphan", "use_attr"] {
        assert!(dead.contains(&expect.to_string()), "缺 {expect}: {dead:?}");
    }
    // 属性引用（m.used_by_b）算活；公有方法/dunder/嵌套不查；pytest/装饰器豁免
    for absent in ["used_by_b", "public_api", "__dunder__", "nested",
                   "framework_registered", "test_something", "pytest_configure"] {
        assert!(!dead.contains(&absent.to_string()), "误判死: {absent}: {dead:?}");
    }
    let suspect = names_of(&res, "suspect_dynamic");
    assert_eq!(suspect, vec!["dispatched_by_name".to_string()], "{suspect:?}");
    // 排序：(file, line) 升序（非全局行号单调——跨文件按文件名段）
    let pairs: Vec<(String, i128)> = match res.get("dead") {
        Some(Value::Arr(a)) => a
            .iter()
            .map(|e| {
                let f = match e.get("file") {
                    Some(Value::Str(s)) => s.clone(),
                    other => panic!("file: {:?}", other),
                };
                (f, get_i128(e, "line"))
            })
            .collect(),
        other => panic!("dead: {:?}", other),
    };
    let mut sorted = pairs.clone();
    sorted.sort();
    assert_eq!(pairs, sorted, "dead 应按 (file, line) 排序");

    // parse_errors：坏文件计数与形状
    match res.get("parse_errors") {
        Some(Value::Arr(a)) => {
            assert_eq!(a.len(), 1, "{:?}", a);
            assert!(matches!(a[0].get("file"), Some(Value::Str(s)) if s.ends_with("broken.py")));
        }
        other => panic!("parse_errors: {:?}", other),
    }
}

#[test]
fn s135_include_decorated_includes_framework() {
    let d = TempDir::new("dc2");
    fixture(d.path());
    let res = deadcode::dead_code_scan(d.path(), 2000, 200, true);
    assert_eq!(get_i128(&res, "exempted_decorated"), 0);
    assert_eq!(get_i128(&res, "dead_count"), 8, "{:?}", res);
    let dead = names_of(&res, "dead");
    assert!(dead.contains(&"framework_registered".to_string()), "{dead:?}");
}

#[test]
fn s135_missing_dir_error_envelope() {
    let res = deadcode::dead_code_scan(Path::new("Z:/rx-deadcode-no-such"), 10, 10, false);
    match res.get("error") {
        Some(Value::Str(s)) => assert!(s.contains("目录不存在"), "{}", s),
        other => panic!("应为错误包络: {:?}", other),
    }
}
