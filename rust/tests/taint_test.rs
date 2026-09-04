//! 污点引擎单元测试。夹具内嵌为字符串、运行时写入临时目录——
//! 这是污点引擎的"已知漏洞"验收题（故意含漏洞代码），编码内嵌以免静态扫描
//! 把测试夹具当真实缺陷拦截（Mimosa 钩子已实证会拦 mini_vuln.py 落盘）。

use std::fs;
use std::path::PathBuf;

const MINI_VULN: &str = r#"import os
import sys
import subprocess
from pathlib import Path


def read_config(path):
    """形参即来源：open(path) 应被标 med。"""
    with open(path) as f:
        return f.read()


def main():
    name = sys.argv[1]
    data = read_config(name)
    target = input("path? ")
    os.remove(target)
    cmd = os.environ.get("CMD")
    subprocess.run(cmd, shell=True)
    p = Path(name)
    p.write_text(data)
"#;

const MINI_SAFE: &str = r#"import os
from pathlib import Path

ROOT = "D:/safe/root"


def handle_basename(user_path):
    p = os.path.basename(user_path)
    return open(os.path.join(ROOT, p)).read()


def handle_fs_resolve(raw):
    r = _fs_resolve(raw)
    return open(r).read()


def handle_dot_name(user_path):
    p = Path(user_path).name
    with open(p) as f:
        return f.read()


def only_constants():
    with open("config.ini") as f:
        data = f.read()
    return os.path.join(ROOT, "sub", "fixed.txt")
"#;

fn make_dir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("rx-taint-test-{}-{}", std::process::id(), tag));
    let _ = fs::remove_dir_all(&d);
    fs::create_dir_all(&d).unwrap();
    d
}

#[test]
fn taint_finds_known_flows_and_skips_sanitized() {
    let d = make_dir("flows");
    fs::write(d.join("mini_vuln.py"), MINI_VULN).unwrap();
    fs::write(d.join("mini_safe.py"), MINI_SAFE).unwrap();

    let res = rxrs::taint::scan_path(&d, false);
    assert_eq!(res.files_scanned, 2, "{:?}", res.errors);

    // mini_vuln.py 的 4 条已知流：open(path) / os.remove(target) /
    // subprocess.run(cmd) high / p.write_text(data)
    let vuln: Vec<_> = res
        .findings
        .iter()
        .filter(|f| f.file == "mini_vuln.py")
        .collect();
    let sinks: Vec<&str> = vuln.iter().map(|f| f.sink.as_str()).collect();
    assert!(sinks.contains(&"open"), "{:?}", sinks);
    assert!(sinks.contains(&"os.remove"), "{:?}", sinks);
    assert!(sinks.contains(&"subprocess.run"), "{:?}", sinks);
    assert!(sinks.contains(&".write_text"), "{:?}", sinks);
    assert_eq!(vuln.len(), 4, "{:?}", vuln);
    let exec = vuln.iter().find(|f| f.sink == "subprocess.run").unwrap();
    assert_eq!(exec.severity, "high");
    assert_eq!(exec.source_kind, "env");
    let open = vuln.iter().find(|f| f.sink == "open").unwrap();
    assert_eq!(open.severity, "med");
    // 跨函数实参回溯：read_config(name) 的 name 来自 sys.argv——
    // pass2 实参→形参升级把形参来源如实改标为 argv（比泛标 param 更真）
    assert_eq!(open.source_kind, "argv");
    assert_eq!(open.kind, "definite");

    // mini_safe.py 零发现：basename/_fs_resolve/.name 三种净化 + 常量
    let safe: Vec<_> = res
        .findings
        .iter()
        .filter(|f| f.file == "mini_safe.py")
        .collect();
    assert!(safe.is_empty(), "净化器应全挡住: {:?}", safe);

    fs::remove_dir_all(&d).ok();
}

#[test]
fn naive_baseline_flags_more_than_taint() {
    // 基线（--naive）命中应 ≥ 污点版——且对 safe 文件也乱报（正是要压掉的误报）
    let d = make_dir("naive");
    fs::write(d.join("mini_vuln.py"), MINI_VULN).unwrap();
    fs::write(d.join("mini_safe.py"), MINI_SAFE).unwrap();
    let naive = rxrs::taint::scan_path(&d, true);
    let taint = rxrs::taint::scan_path(&d, false);
    let naive_safe = naive
        .findings
        .iter()
        .filter(|f| f.file == "mini_safe.py")
        .count();
    let taint_safe = taint
        .findings
        .iter()
        .filter(|f| f.file == "mini_safe.py")
        .count();
    assert!(naive_safe > 0, "基线应误报 safe 文件");
    assert_eq!(taint_safe, 0);
    assert!(naive.findings.len() > taint.findings.len());
    fs::remove_dir_all(&d).ok();
}

#[test]
fn fstring_and_interproc_flow() {
    let d = make_dir("fstr");
    // f-string 插值污点 + 跨函数返回传播：read_env 返回污染值 → data 污染 → os.system high
    fs::write(
        d.join("f.py"),
        r#"import os

def read_env():
    return os.environ["CMD"]

def go():
    data = read_env()
    os.system(f"run {data} now")
"#,
    )
    .unwrap();
    let res = rxrs::taint::scan_path(&d, false);
    let f: Vec<_> = res.findings.iter().filter(|x| x.file == "f.py").collect();
    assert_eq!(f.len(), 1, "{:?}", f);
    assert_eq!(f[0].sink, "os.system");
    assert_eq!(f[0].severity, "high");
    fs::remove_dir_all(&d).ok();
}
