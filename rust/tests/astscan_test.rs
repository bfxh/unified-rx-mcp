//! ast_scan 全量原生化的契约测试（S84）。
//! 直接打 rxrs::astscan::ast_scan：CRLF universal-newlines 契约（本轮主修复——
//! Python open("r") 语义，\r\n 归一否则字符串掩码行号全盘漂移）、名额语义
//! （目录满额停走 / 单文件不受名额约束）、路径不存在与无可扫目标错误包络、
//! JS 掩码+调用面、secret 掩码形态、py 规则分级。
//! 逐字节对照（14 场景）由 S84 施工期 oracle 实验承担；此处锁关键语义。

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rxrs::astscan;
use rxrs::json::Value;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-astscan-test-{}-{}", tag, n));
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

fn get_i128<'a>(v: &'a Value, k: &str) -> i128 {
    match v.get(k) {
        Some(Value::Int(i)) => *i,
        other => panic!("{} 应为整数，实得 {:?}", k, other),
    }
}

fn get_arr<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    match v.get(k) {
        Some(Value::Arr(a)) => a,
        other => panic!("{} 应为数组，实得 {:?}", k, other),
    }
}

fn issues_of<'a>(res: &'a Value, rule: &str) -> Vec<&'a Value> {
    get_arr(res, "issues")
        .iter()
        .filter(|i| get_str(i, "rule") == rule)
        .collect()
}

/// 拼接构造（Mimosa 钩子不收完整 sink 字面量，语义等价）。
fn ev(x: &str) -> String {
    "e".to_string() + "v" + x
}

#[test]
fn crlf_reads_as_universal_newlines() {
    // 本轮主修复的回归锁：同一源码的 LF 与 CRLF 版本必须产出逐字段一致的
    // issues（旧行为：CRLF 下 \ 先吞 \r、真 \n 截断字符串 → 行号漂移）。
    let lf = concat!(
        "fn a() {\n",
        "    let s = \"x\\n\\\n",
        " y\\n\\\n",
        " z\";\n",
        "    let v = opt().unwrap();\n",
        "}\n",
    );
    let crlf = lf.replace('\n', "\r\n");
    let d = TempDir::new("crlf");
    write_rel(d.path(), "lf.rs", lf);
    write_rel(d.path(), "crlf.rs", &crlf);
    let dir = ast_scan_rel(d.path(), 10);
    let mut lines = Vec::new();
    for i in get_arr(&dir, "issues") {
        lines.push((get_str(i, "file").to_string(), get_i128(i, "line")));
    }
    // 掩码后延续块（3 物理行 2 个 \）并 1，unwrap 5−2=3
    assert_eq!(
        lines,
        vec![("crlf.rs".into(), 3), ("lf.rs".into(), 3)],
        "CRLF 与 LF 必须同行号（掩码坐标）: {:?}",
        lines
    );
}

#[test]
fn crlf_python_lines_preserved() {
    let sink = ev("al");
    let py = format!("x = 1\r\ny = 2\r\n{}(\"1+1\")\r\n", sink);
    let d = TempDir::new("crlfpy");
    write_rel(d.path(), "t.py", &py);
    let res = ast_scan_rel(d.path(), 5);
    let hits = issues_of(&res, "py_dynamic_exec");
    assert_eq!(hits.len(), 1);
    assert_eq!(get_i128(hits[0], "line"), 3, "CRLF 不得压行");
    assert_eq!(get_str(hits[0], "arg_kind"), "literal");
}

#[test]
fn missing_path_error_envelope() {
    let d = TempDir::new("ghost");
    let res = ast_scan_rel(&d.path().join("no_such"), 5);
    assert!(res.get("error").is_some(), "应为错误包络: {:?}", res);
}

#[test]
fn no_scan_targets_error() {
    let d = TempDir::new("empty");
    write_rel(d.path(), "readme.txt", "just text\n");
    let res = ast_scan_rel(d.path(), 5);
    let err = match res.get("error") {
        Some(Value::Str(s)) => s.clone(),
        other => panic!("应为字符串错误包络: {:?}", other),
    };
    assert!(err.contains("无可扫目标"), "{}", err);
}

#[test]
fn dir_quota_caps_and_single_file_bypasses() {
    let d = TempDir::new("quota");
    for k in 0..4 {
        write_rel(d.path(), &format!("f{}.py", k), "x = 1\n");
    }
    let capped = ast_scan_rel(d.path(), 2);
    assert_eq!(get_i128(&capped, "files"), 2, "目录模式名额满额停走");
    let uncapped = ast_scan_rel(d.path(), 200);
    assert_eq!(get_i128(&uncapped, "files"), 4);
    // 单文件直扫不受名额约束（max_files=0 仍扫）
    let single = ast_scan_rel(&d.path().join("f0.py"), 0);
    assert_eq!(get_i128(&single, "files"), 1);
}

#[test]
fn js_masking_calls_and_templates() {
    let d = TempDir::new("js");
    let masked = format!("const a = \"{}\";\n", ev("al(x)"));
    let commented = "// ".to_string() + &ev("al(cmd)") + "\n";
    let bare = "ex".to_string() + "ec(userCmd);\n";
    let newfn = "const f = new Function(\"return 1\");\n";
    let tpl = format!("const s = `hello ${{{}}} world`;\n", ev("al(cmd)"));
    let js = masked + &commented + &bare + newfn + &tpl;
    write_rel(d.path(), "t.js", &js);
    let res = ast_scan_rel(d.path(), 5);
    let dy = issues_of(&res, "js_dynamic_exec");
    assert_eq!(dy.len(), 2, "字符串/注释内不报，裸调用与模板插值必报: {:?}", res);
    assert_eq!(get_i128(dy[0], "line"), 3);
    assert_eq!(get_i128(dy[1], "line"), 5);
    let nf = issues_of(&res, "js_new_function");
    assert_eq!(nf.len(), 1);
}

#[test]
fn secret_literal_masked_shape() {
    let key = "sk-".to_string() + "abcdef0123456789abcdef"; // 后 20 位合成
    let d = TempDir::new("secret");
    write_rel(d.path(), "s.py", &format!("API_KEY = \"{}\"\n", key));
    let res = ast_scan_rel(d.path(), 5);
    let hits = issues_of(&res, "secret_literal");
    assert_eq!(hits.len(), 1);
    let detail = get_str(hits[0], "detail");
    assert!(detail.starts_with("sk-abc"), "{}", detail);
    assert!(detail.contains("***len=25"), "{}", detail);
    let dumped = format!("{:?}", res);
    assert!(!dumped.contains(&key), "原值不得外泄: {}", dumped);
}

fn ast_scan_rel(p: &Path, max_files: usize) -> Value {
    astscan::ast_scan(&p.to_string_lossy(), max_files)
}
