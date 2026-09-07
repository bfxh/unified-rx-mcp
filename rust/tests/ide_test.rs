//! ide_read 双件（outline / read_symbol）的行为契约测试（S92）。
//! 直接打 rxrs::ide 库函数 + 手工构造 SandboxCfg——不依赖进程级 env（cargo test
//! 并行线程共享 env 会互踩）。语义基准 = tools/scan.py::_symbol_spans（S70）
//! + tools/ide_read.py（S66），与 Python 侧 tests/test_s92_ide_read_rust.py 同表。

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rxrs::ide;
use rxrs::json::Value;
use rxrs::sandbox::SandboxCfg;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-ide-test-{}-{}", tag, n));
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

fn write_rel(root: &Path, rel: &str, content: &str) -> PathBuf {
    let p = root.join(rel);
    fs::write(&p, content).unwrap();
    p
}

fn get_str<'a>(v: &'a Value, k: &str) -> &'a str {
    match v.get(k) {
        Some(Value::Str(s)) => s,
        other => panic!("{} 应为字符串，实得 {:?}", k, other),
    }
}

fn get_int(v: &Value, k: &str) -> i128 {
    match v.get(k) {
        Some(Value::Int(n)) => *n,
        other => panic!("{} 应为整数，实得 {:?}", k, other),
    }
}

/// outline 结果的符号速览：(name, kind, line, end_line, params)
fn syms(v: &Value) -> Vec<(String, String, i128, i128, i128)> {
    let arr = match v.get("symbols") {
        Some(Value::Arr(a)) => a.clone(),
        other => panic!("symbols 应为数组，实得 {:?}", other),
    };
    arr.iter()
        .map(|s| {
            (
                get_str(s, "name").to_string(),
                get_str(s, "kind").to_string(),
                get_int(s, "line"),
                get_int(s, "end_line"),
                get_int(s, "params"),
            )
        })
        .collect()
}

const PY_SRC: &str = "def top(a, b):\n    return a\n\n\nclass Outer:\n    def handle(self, x):\n        return x\n\n    def handle(self, y, z):\n        return y + z\n";

#[test]
fn outline_python_symbols() {
    let td = TempDir::new("py");
    let p = write_rel(td.path(), "s.py", PY_SRC);
    let cfg = SandboxCfg::parse("*");
    let v = ide::outline(&cfg, p.to_str().unwrap()).unwrap();
    assert_eq!(get_str(&v, "lang"), "python");
    assert_eq!(get_int(&v, "total"), 4);
    let s = syms(&v);
    assert_eq!(s[0], ("top".into(), "fn".into(), 1, 2, 2));
    assert_eq!(s[1], ("Outer".into(), "type".into(), 5, 10, 0));
    assert_eq!(s[2], ("handle".into(), "fn".into(), 6, 7, 2));
    assert_eq!(s[3], ("handle".into(), "fn".into(), 9, 10, 3));
}

#[test]
fn outline_rust_quirks() {
    let td = TempDir::new("rs");
    let p = write_rel(
        td.path(),
        "q.rs",
        "struct Point(i32, i32);\n\nimpl fmt::Display for Point {\n    fn fmt(&self) {}\n}\n\nfn q() { struct S; }\n",
    );
    let cfg = SandboxCfg::parse("*");
    let v = ide::outline(&cfg, p.to_str().unwrap()).unwrap();
    let s = syms(&v);
    let names: Vec<&str> = s.iter().map(|x| x.0.as_str()).collect();
    assert_eq!(names, vec!["Point", "fmt", "q"]);
    assert_eq!((s[1].1.as_str(), s[1].2), ("type", 3)); // impl 头捕获名 fmt
    assert_eq!((s[2].1.as_str(), s[2].2, s[2].4), ("type", 7, 0)); // 一行 fn 含 struct → type、params 0
    assert_eq!((s[0].1.as_str(), s[0].4), ("type", 0)); // 元组 struct header 有括号但 outline 只认 fn
}

#[test]
fn outline_go_receiver_and_alias() {
    let td = TempDir::new("go");
    let p = write_rel(
        td.path(),
        "m.go",
        "func main() {\n}\n\ntype Point struct {\n\tX int\n}\n\nfunc (p *Point) Sum() int {\n\treturn 1\n}\n\ntype Alias = int\n",
    );
    let cfg = SandboxCfg::parse("*");
    let v = ide::outline(&cfg, p.to_str().unwrap()).unwrap();
    let s = syms(&v);
    let names: Vec<&str> = s.iter().map(|x| x.0.as_str()).collect();
    assert_eq!(names, vec!["main", "Point", "Sum", "Alias"]);
    assert_eq!(s[0].1, "fn");
    // S70 怪癖：kind 判定要求 keyword 后跟 \s+\w，"type Point struct {" 行尾是
    // "{" 不是词 → go 的 type 声明落 fn（旧 Python 实现同判，oracle 43/43 对拍）
    assert_eq!(s[1].1, "fn");
    assert_eq!(s[2].1, "fn"); // 带接收器的 func 捕获方法名 Sum
    assert_eq!(s[3].1, "fn");
}

#[test]
fn outline_js_class_is_fn_and_indented_function() {
    let td = TempDir::new("js");
    let p = write_rel(
        td.path(),
        "w.js",
        "class Widget {\n  method(x) {\n    return x;\n  }\n}\n\n  function tail(x) {\n    return x;\n  }\n",
    );
    let cfg = SandboxCfg::parse("*");
    let v = ide::outline(&cfg, p.to_str().unwrap()).unwrap();
    let s = syms(&v);
    let names: Vec<&str> = s.iter().map(|x| x.0.as_str()).collect();
    assert_eq!(names, vec!["Widget", "tail"]); // 简写 method 无 function 关键字不捕获
    assert_eq!(s[0].1, "fn"); // js class 不含 struct/... 关键字 → fn（怪癖）
}

#[test]
fn outline_typescript_empty_but_ok() {
    let td = TempDir::new("ts");
    let p = write_rel(td.path(), "t.ts", "const x: number = 1;\n");
    let cfg = SandboxCfg::parse("*");
    let v = ide::outline(&cfg, p.to_str().unwrap()).unwrap();
    assert_eq!(get_str(&v, "lang"), "typescript");
    assert_eq!(get_int(&v, "total"), 0);
}

#[test]
fn outline_unicode_identifier() {
    let td = TempDir::new("uni");
    let p = write_rel(td.path(), "u.py", "def 你好(x):\n    return x\n");
    let cfg = SandboxCfg::parse("*");
    let v = ide::outline(&cfg, p.to_str().unwrap()).unwrap();
    assert_eq!(syms(&v)[0].0, "你好");
}

#[test]
fn read_symbol_occurrence_and_params_rules() {
    let td = TempDir::new("occ");
    let p = write_rel(td.path(), "s.py", PY_SRC);
    let cfg = SandboxCfg::parse("*");
    let v = ide::read_symbol(&cfg, p.to_str().unwrap(), "handle", 2).unwrap();
    assert_eq!(get_int(&v, "start"), 9);
    assert_eq!(get_int(&v, "lines"), 2);
    assert_eq!(get_int(&v, "params"), 3);
    assert_eq!(
        get_str(&v, "content"),
        "    def handle(self, y, z):\n        return y + z"
    );
    // read_symbol 的 params 只看括号不看 kind：rust 元组 struct → 2
    let td2 = TempDir::new("occ2");
    let p2 = write_rel(td2.path(), "q.rs", "struct Point(i32, i32);\n");
    let v2 = ide::read_symbol(&cfg, p2.to_str().unwrap(), "Point", 1).unwrap();
    assert_eq!(get_int(&v2, "params"), 2);
}

#[test]
fn read_symbol_crlf_content_has_no_cr() {
    let td = TempDir::new("crlf");
    let p = td.path().join("c.py");
    fs::write(&p, b"def one(x):\r\n    return x\r\n").unwrap();
    let cfg = SandboxCfg::parse("*");
    let v = ide::read_symbol(&cfg, p.to_str().unwrap(), "one", 1).unwrap();
    assert_eq!(get_str(&v, "content"), "def one(x):\n    return x");
}

#[test]
fn read_symbol_trailing_phantom_line() {
    // 末位缩进 function（无 brace 回扫）的跨度含尾幻影行 → content 以 \n 收尾
    let td = TempDir::new("phantom");
    let p = write_rel(
        td.path(),
        "w.js",
        "class Widget {\n}\n\n  function tail(x) {\n    return x;\n  }\n",
    );
    let cfg = SandboxCfg::parse("*");
    let v = ide::read_symbol(&cfg, p.to_str().unwrap(), "tail", 1).unwrap();
    assert!(get_str(&v, "content").ends_with('\n'));
}

#[test]
fn tool_level_errors() {
    let td = TempDir::new("err");
    write_rel(td.path(), "s.py", PY_SRC);
    write_rel(td.path(), "n.txt", "not code");
    let cfg = SandboxCfg::parse("*");
    let err = ide::outline(&cfg, td.path().join("no.py").to_str().unwrap()).unwrap();
    assert!(get_str(&err, "error").contains("文件不存在"));
    let err2 = ide::outline(&cfg, td.path().join("n.txt").to_str().unwrap()).unwrap();
    assert!(get_str(&err2, "error").contains("文件不可读或非代码文件"));
    let err3 =
        ide::read_symbol(&cfg, td.path().join("s.py").to_str().unwrap(), "ghost", 1).unwrap();
    assert!(get_str(&err3, "error").contains("符号 ghost 不存在"));
    let err4 =
        ide::read_symbol(&cfg, td.path().join("s.py").to_str().unwrap(), "handle", 0).unwrap();
    assert!(get_str(&err4, "error").contains("occurrence=0 越界（handle 共 2 处）"));
    let err5 =
        ide::read_symbol(&cfg, td.path().join("s.py").to_str().unwrap(), "handle", -1).unwrap();
    assert!(get_str(&err5, "error").contains("occurrence=-1 越界"));
}

#[test]
fn outline_cap_300() {
    let td = TempDir::new("cap");
    let mut src = String::new();
    for i in 0..320 {
        src.push_str(&format!("def f{}():\n    pass\n\n", i));
    }
    let p = write_rel(td.path(), "many.py", &src);
    let cfg = SandboxCfg::parse("*");
    let v = ide::outline(&cfg, p.to_str().unwrap()).unwrap();
    assert_eq!(get_int(&v, "total"), 300);
}

#[test]
fn sandbox_deny_and_empty_path() {
    let cfg = SandboxCfg::parse("Z:\\no-such-root-xyz");
    let err = ide::outline(&cfg, "C:\\Windows\\win.ini").unwrap_err();
    assert!(err.contains("路径越界"), "{}", err);
    let cfg2 = SandboxCfg::parse("*");
    let err2 = ide::outline(&cfg2, "").unwrap_err();
    assert_eq!(err2, "path 必填");
}
