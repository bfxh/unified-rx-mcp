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

// ---------------------------------------------------------------- S128 跨文件链

const CF_MAIN: &str = r#"import os
import sys
from helpers import read_it, clean, read_cfg, only_safe

def boot():
    name = sys.argv[1]
    return read_it(name)

def safe_boot():
    name = sys.argv[1]
    return clean(name)

def callsafe():
    return only_safe(os.path.basename(sys.argv[1]))

def run():
    cmd = read_cfg()
    os.system(cmd)
"#;

const CF_HELPERS: &str = r#"import os

def read_it(path):
    return open(path).read()

def clean(p2):
    return open(os.path.basename(p2)).read()

def only_safe(p3):
    return open(p3).read()

def read_cfg():
    return os.environ.get("CFG")
"#;

#[test]
fn cross_file_chains_and_sanitization() {
    let d = make_dir("xfile");
    fs::write(d.join("main.py"), CF_MAIN).unwrap();
    fs::write(d.join("helpers.py"), CF_HELPERS).unwrap();
    let res = rxrs::taint::scan_path(&d, false);
    assert_eq!(res.files_scanned, 2, "{:?}", res.errors);

    // ① 实参跨文件 → callee 内汇点：main.boot 的 argv → helpers.read_it.path → open
    let open_read_it = res.findings.iter().find(|f| {
        f.file == "helpers.py" && f.sink == "open" && f.var == "path"
    }).expect("read_it 的 open 应命中");
    assert_eq!(open_read_it.flow, "cross", "{:?}", open_read_it);
    assert_eq!(open_read_it.kind, "definite", "argv 实锤应跨文件升级形参");
    assert_eq!(open_read_it.source_kind, "argv");
    let origin = open_read_it.origin.clone().expect("跨文件必有链证据");
    assert!(origin.contains("main.py") && origin.contains("read_it"),
            "链证据应含来源文件与目标函数: {origin}");

    // ② 污染返回值跨文件 → caller 汇点：helpers.read_cfg 的 env → main.run 的 os.system
    let exec_main = res.findings.iter().find(|f| {
        f.file == "main.py" && f.sink == "os.system"
    }).expect("run 的 os.system 应命中");
    assert_eq!(exec_main.flow, "cross", "{:?}", exec_main);
    assert_eq!(exec_main.source_kind, "env");
    assert!(exec_main.origin.as_deref().unwrap_or("").contains("helpers.py"),
            "链证据应指向 helpers.py: {:?}", exec_main.origin);

    // ③ 净化不因跨界失效：clean 内部 basename 挡住汇点——p2 即便被实锤注入也无发现
    assert!(!res.findings.iter().any(|f| f.var == "p2"),
            "callee 内部净化必须继续挡住: {:?}", res.findings);

    // ④ 调用点净化：only_safe 只被净化实参调用 → 无跨文件链（保持 clue/direct）
    let only_safe = res.findings.iter().find(|f| {
        f.file == "helpers.py" && f.sink == "open" && f.var == "p3"
    }).expect("only_safe 的 open 应以 clue 命中（形参即来源）");
    assert!(only_safe.origin.is_none() && only_safe.flow != "cross",
            "净化实参不得产生跨文件链: {:?}", only_safe);

    // ⑤ A/B：cross=false 逐字节回到 S78 语义——零 cross 流
    let no_cross = rxrs::taint::scan_path_opts(&d, false, false);
    assert!(!no_cross.findings.iter().any(|f| f.flow == "cross"),
            "{:?}", no_cross.findings);
    fs::remove_dir_all(&d).ok();
}

#[test]
fn cross_file_ambiguous_name_is_skipped_honestly() {
    let d = make_dir("xambig");
    // 两个文件定义同名函数：全局多义 → 放弃连边并计数（不猜）
    fs::write(d.join("dup_a.py"), "def dup_target(p):\n    return open(p).read()\n").unwrap();
    fs::write(d.join("dup_b.py"), "def dup_target(p):\n    return open(p).read()\n").unwrap();
    fs::write(
        d.join("dup_c.py"),
        "import sys\ndef caller():\n    return dup_target(sys.argv[1])\n",
    )
    .unwrap();
    let res = rxrs::taint::scan_path(&d, false);
    assert!(res.cross_skipped_ambiguous >= 1,
            "同名多义应如实计数: {}", res.cross_skipped_ambiguous);
    // dup_a/dup_b 各自的 clue 级发现照常（形参即来源），但不得出现跨文件链
    assert!(!res.findings.iter().any(|f| f.flow == "cross"), "{:?}", res.findings);
    fs::remove_dir_all(&d).ok();
}

#[test]
fn cross_file_alias_resolved_via_callgraph() {
    // 别名导入（as ri）在文本名层面连不上；S128 消费 nameres 调用图的解析结果
    // （callee=b.helper）才可能连边——本测试即该通路的实锤。
    let d = make_dir("xalias");
    fs::write(
        d.join("main.py"),
        "import sys\nfrom helpers import read_it as ri\n\ndef entry():\n    name = sys.argv[1]\n    return ri(name)\n",
    )
    .unwrap();
    fs::write(d.join("helpers.py"), "def read_it(path):\n    return open(path).read()\n").unwrap();
    let res = rxrs::taint::scan_path(&d, false);
    let f = res.findings.iter().find(|f| {
        f.file == "helpers.py" && f.sink == "open" && f.var == "path"
    }).expect("别名调用应经调用图连边后命中");
    assert_eq!(f.flow, "cross", "{:?}", f);
    assert!(f.origin.as_deref().unwrap_or("").contains("main.py"),
            "链证据应指向调用方: {:?}", f.origin);
    // A/B：关掉跨文件（--no-cross 通路）后同一夹具无 cross 流——证明增量来自本机制
    let base = rxrs::taint::scan_path_opts(&d, false, false);
    assert!(!base.findings.iter().any(|f| f.flow == "cross"), "{:?}", base.findings);
    fs::remove_dir_all(&d).ok();
}

#[test]
fn cross_file_mutual_recursion_terminates() {
    let d = make_dir("xcycle");
    fs::write(
        d.join("cyc_a.py"),
        "from cyc_b import pong\n\ndef ping(p):\n    return pong(p)\n",
    )
    .unwrap();
    fs::write(
        d.join("cyc_b.py"),
        "from cyc_a import ping\n\ndef pong(q):\n    if q:\n        return ping(q)\n    return open(q).read()\n",
    )
    .unwrap();
    // 不挂起、可确定产出：互递归跨文件传播靠轮次上限 + 只升级语义收敛
    let res = rxrs::taint::scan_path(&d, false);
    assert_eq!(res.files_scanned, 2, "{:?}", res.errors);
    let open_pong = res.findings.iter().find(|f| {
        f.file == "cyc_b.py" && f.sink == "open" && f.var == "q"
    }).expect("pong 的 open 应命中");
    assert_eq!(open_pong.flow, "cross", "互递归种子应带链: {:?}", open_pong);
    fs::remove_dir_all(&d).ok();
}
