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
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent).unwrap();
    }
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

// ================= S93：locate_edit / code_context / ide_rename =================

use rxrs::ide::{code_context, ide_rename};

fn hits_of(v: &Value) -> Vec<(String, i128, String)> {
    match v.get("hits") {
        Some(Value::Arr(a)) => a
            .iter()
            .map(|h| {
                (
                    get_str(h, "file").to_string(),
                    get_int(h, "line"),
                    get_str(h, "snippet").to_string(),
                )
            })
            .collect(),
        other => panic!("hits 应为数组，实得 {:?}", other),
    }
}

#[test]
fn s93_locate_hits_snippet_and_ci() {
    let td = TempDir::new("s93loc");
    write_rel(td.path(), "aa.py", "hello_world = 1\nHELLO_WORLD twice\nfind hello_world here\nhello_world again\nlast line\n");
    write_rel(td.path(), "ab.txt", "hello_world in txt\n"); // 非代码：不计
    let cfg = SandboxCfg::parse("*");
    let v =
        ide::locate_edit(&cfg, td.path().to_str().unwrap(), "hello_world", 100, 10).unwrap();
    assert_eq!(get_int(&v, "total"), 4);
    // 影响面计数区分大小写：HELLO_WORLD 不入 refs
    assert_eq!(get_int(&v, "references_in_scan"), 3);
    assert_eq!(get_str(&v, "query"), "hello_world");
    let hits = hits_of(&v);
    assert_eq!(hits[0].1, 1);
    // snippet 窗口：1 前导 + 当前行 + 2 后随（尾行命中窗口收口）
    assert_eq!(
        hits[0].2,
        "hello_world = 1\nHELLO_WORLD twice\nfind hello_world here\nhello_world again"
    );
    // 忽略大小写命中（精确未中、小写比较命中）
    assert_eq!(hits[1].1, 2);
}

#[test]
fn s93_locate_limit_stop_and_refs() {
    let td = TempDir::new("s93limit");
    for i in 1..=8 {
        write_rel(td.path(), &format!("f{}.py", i), "zzq\nzzq\nzzq\n");
    }
    let cfg = SandboxCfg::parse("*");
    // limit=1：3 处即停（limit*3），只读过 f1 → refs=3
    let v = ide::locate_edit(&cfg, td.path().to_str().unwrap(), "zzq", 100, 1).unwrap();
    assert_eq!(get_int(&v, "total"), 3);
    assert_eq!(get_int(&v, "references_in_scan"), 3);
    assert_eq!(hits_of(&v).len(), 1);
    // limit=4：12 处停在第 4 个文件 → refs=12（含触发停机的文件）
    let v4 = ide::locate_edit(&cfg, td.path().to_str().unwrap(), "zzq", 100, 4).unwrap();
    assert_eq!(get_int(&v4, "total"), 12);
    assert_eq!(get_int(&v4, "references_in_scan"), 12);
    assert_eq!(hits_of(&v4).len(), 4);
}

#[test]
fn s93_locate_max_files_skip_dirs_and_errors() {
    let td = TempDir::new("s93cap");
    for i in 1..=6 {
        write_rel(td.path(), &format!("m{}.py", i), "cap\n");
    }
    write_rel(td.path(), "n.md", "cap\n");
    write_rel(td.path(), ".git/h.py", "cap\n");
    write_rel(td.path(), "node_modules/h.py", "cap\n");
    let cfg = SandboxCfg::parse("*");
    let root = td.path().to_str().unwrap().to_string();
    let v = ide::locate_edit(&cfg, &root, "cap", 4, 10).unwrap();
    assert_eq!(get_int(&v, "total"), 4); // 非代码不占额度，跳过目录不进
    // 空查询（strip 后）→ 工具级错误
    let e1 = ide::locate_edit(&cfg, &root, "   ", 100, 10).unwrap();
    assert_eq!(get_str(&e1, "error"), "query 为空——请提供符号或关键词");
    // 非目录（指向文件）→ 工具级错误，含解析后路径
    let file_path = td.path().join("m1.py");
    let e2 = ide::locate_edit(&cfg, file_path.to_str().unwrap(), "x", 100, 10).unwrap();
    assert_eq!(
        get_str(&e2, "error"),
        format!("不是目录: {}", file_path.display())
    );
    // path 必填
    let e3 = ide::locate_edit(&cfg, "", "x", 100, 10).unwrap();
    assert_eq!(get_str(&e3, "error"), "path 必填");
    // 沙盒拒绝 → 工具级错误（locate 捕获 resolve 失败，不走 exit-2）
    let deny = SandboxCfg::parse("Z:\\no-such-root-xyz");
    let e4 = ide::locate_edit(&deny, &root, "x", 100, 10).unwrap();
    assert!(get_str(&e4, "error").contains("路径越界"), "{}", get_str(&e4, "error"));
}

#[test]
fn s93_context_window_radius_and_raw_split() {
    let td = TempDir::new("s93ctx");
    let twenty: String = (1..=20).map(|i| format!("l{}\n", i)).collect();
    let p = write_rel(td.path(), "long20.py", &twenty);
    let cfg = SandboxCfg::parse("*");
    let pp = p.to_str().unwrap();
    // radius 下限 5：radius=1/3 同窗（start=5 行, end=14 行，1-based 报告）
    let v = code_context(&cfg, pp, 10, 3).unwrap();
    assert_eq!(get_int(&v, "start"), 5);
    assert_eq!(get_int(&v, "end"), 14);
    assert_eq!(get_int(&v, "total_lines"), 21); // 尾幻影行
    assert_eq!(get_str(&v, "lang"), "python");
    let v1 = code_context(&cfg, pp, 10, 1).unwrap();
    assert_eq!(get_int(&v1, "start"), get_int(&v, "start"));
    // radius=0 视作缺省 30
    let v0 = code_context(&cfg, pp, 10, 0).unwrap();
    assert_eq!(get_int(&v0, "start"), 1);
    assert_eq!(get_int(&v0, "end"), 21);
    // radius 上限 200
    let big = write_rel(td.path(), "big15.py", &(1..=15).map(|i| format!("b{}\n", i)).collect::<String>());
    let vb = code_context(&cfg, big.to_str().unwrap(), 8, 500).unwrap();
    assert_eq!(get_int(&vb, "start"), 1);
    assert_eq!(get_int(&vb, "end"), 16);
    // cursor=0 → 头窗 min(80, 行数)
    let vh = code_context(&cfg, pp, 0, 5).unwrap();
    assert_eq!(get_int(&vh, "start"), 1);
    assert_eq!(get_int(&vh, "end"), 21);
    // RAW split：\r 留在行内，CRLF 文件带尾幻影行
    let crlf = write_rel(td.path(), "crlf.py", "l1\r\nl2\r\nl3\r\nl5\r\n");
    let vc = code_context(&cfg, crlf.to_str().unwrap(), 2, 5).unwrap();
    assert_eq!(get_int(&vc, "total_lines"), 5);
    assert_eq!(get_str(&vc, "content"), "l1\r\nl2\r\nl3\r\nl5\r\n");
    // 负 cursor → end 负值 + Python 负切片（end=-6 → 前 13-6=7 行）
    let twelve: String = (1..=12).map(|i| format!("t{}\n", i)).collect();
    let tw = write_rel(td.path(), "tail12.py", &twelve);
    let vn = code_context(&cfg, tw.to_str().unwrap(), -10, 5).unwrap();
    assert_eq!(get_int(&vn, "start"), 1);
    assert_eq!(get_int(&vn, "end"), -6);
    assert!(get_str(&vn, "content").starts_with("t1\nt2\nt3\nt4\nt5\nt6\nt7"));
    // file/lang 字段回原样参数（不重写为解析路径）
    assert_eq!(get_str(&vc, "file"), crlf.to_str().unwrap());
}

#[test]
fn s93_context_gates() {
    let td = TempDir::new("s93gate");
    let cfg = SandboxCfg::parse("*");
    // >10MB：getsize 门（裸路径，先于沙盒/读取）
    let big = vec![b'x'; 10 * 1024 * 1024 + 1];
    let bp = td.path().join("over10mb.py");
    fs::write(&bp, &big).unwrap();
    let v = code_context(&cfg, bp.to_str().unwrap(), 0, 0).unwrap();
    assert_eq!(get_str(&v, "error"), "文件超过 10MB——拒绝读取");
    // 缺失 / 目录 / 空路径 → "文件不可读"
    let miss = code_context(&cfg, td.path().join("no_such.py").to_str().unwrap(), 0, 0).unwrap();
    assert_eq!(get_str(&miss, "error"), format!("文件不可读: {}", td.path().join("no_such.py").display()));
    let dir = code_context(&cfg, td.path().to_str().unwrap(), 0, 0).unwrap();
    assert!(get_str(&dir, "error").starts_with("文件不可读: "));
    let empty = code_context(&cfg, "", 0, 0).unwrap();
    assert_eq!(get_str(&empty, "error"), "文件不可读: ");
    // 沙盒拒绝同样落"文件不可读"（getsize 成功后 _read 失败的漏斗）
    let deny = SandboxCfg::parse("Z:\\no-such-root-xyz");
    let ok_file = write_rel(td.path(), "ok.py", "x = 1\n");
    let vd = code_context(&deny, ok_file.to_str().unwrap(), 0, 0).unwrap();
    assert!(get_str(&vd, "error").starts_with("文件不可读: "));
}

#[test]
fn s93_rename_plan_cap_and_quirks() {
    let td = TempDir::new("s93ren");
    write_rel(td.path(), "one.py", "foo = 1\nfoo(2)\n");
    write_rel(td.path(), "sub/two.py", "bar\nfoo\n");
    write_rel(td.path(), "three.txt", "foo\n"); // 非代码：排除
    write_rel(td.path(), ".git/g.py", "foo\n"); // 跳过目录
    let cfg = SandboxCfg::parse("*");
    let root = td.path().to_str().unwrap().to_string();
    // 缺省：plan=null
    let v = ide_rename(&cfg, &root, "foo", "bar", false).unwrap();
    assert_eq!(get_int(&v, "files_affected"), 2);
    assert_eq!(get_int(&v, "total_occurrences"), 3);
    assert!(matches!(v.get("plan"), Some(Value::Null)));
    assert_eq!(get_str(&v, "note"), "L3 只建议不落盘；确认后可用 fs_write 应用");
    // include_plan：文件先于子目录（os.walk 两段式）
    let v2 = ide_rename(&cfg, &root, "foo", "bar", true).unwrap();
    match v2.get("plan") {
        Some(Value::Arr(a)) => {
            assert_eq!(a.len(), 2);
            assert!(get_str(&a[0], "file").ends_with("one.py"));
            assert_eq!(get_int(&a[0], "occurrences"), 2);
            assert!(get_str(&a[1], "file").ends_with("two.py"));
        }
        other => panic!("plan 应为数组，实得 {:?}", other),
    }
    // 大小写敏感：FOO 无命中
    let v3 = ide_rename(&cfg, &root, "FOO", "x", false).unwrap();
    assert_eq!(get_int(&v3, "files_affected"), 0);
    // 空符号怪癖：匹配一切可读代码文件，count("")=len+1
    let v4 = ide_rename(&cfg, &root, "", "x", false).unwrap();
    assert_eq!(get_int(&v4, "files_affected"), 2);
    assert_eq!(get_int(&v4, "total_occurrences"), 16 + 9);
    // path 必填 / 非目录
    let e1 = ide_rename(&cfg, "", "x", "y", false).unwrap();
    assert_eq!(get_str(&e1, "error"), "path 必填");
    let fp = td.path().join("one.py");
    let e2 = ide_rename(&cfg, fp.to_str().unwrap(), "x", "y", false).unwrap();
    assert_eq!(get_str(&e2, "error"), format!("不是目录: {}", fp.display()));
}

#[test]
fn s93_rename_cap_200() {
    let td = TempDir::new("s93cap200");
    for i in 1..=205 {
        write_rel(td.path(), &format!("c{:03}.py", i), "sym\n");
    }
    let cfg = SandboxCfg::parse("*");
    let v = ide_rename(&cfg, td.path().to_str().unwrap(), "sym", "n", true).unwrap();
    assert_eq!(get_int(&v, "files_affected"), 200);
    assert_eq!(get_int(&v, "total_occurrences"), 200);
    match v.get("plan") {
        Some(Value::Arr(a)) => {
            assert_eq!(a.len(), 200);
            assert!(get_str(&a[0], "file").ends_with("c001.py"));
            assert!(get_str(&a[199], "file").ends_with("c200.py"));
        }
        other => panic!("plan 应为数组，实得 {:?}", other),
    }
}

