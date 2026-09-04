//! fs 读面三工具的沙盒与行为契约测试（S79）。
//! 直接打 rxrs::fs 库函数 + 手工构造 SandboxCfg——不依赖进程级 env（cargo test
//! 并行线程共享 env 会互踩）。

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rxrs::json::Value;
use rxrs::sandbox::SandboxCfg;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-fs-test-{}-{}", tag, n));
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
    fs::create_dir_all(p.parent().unwrap()).unwrap();
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
        Some(Value::Int(i)) => *i,
        other => panic!("{} 应为整数，实得 {:?}", k, other),
    }
}

fn is_err_obj(v: &Value) -> &str {
    get_str(v, "error")
}

// ---------- 沙盒语义 ----------

#[test]
fn fail_closed_when_sandbox_blank() {
    let t = TempDir::new("closed");
    let f = write_rel(t.path(), "x.txt", "hi");
    for raw in ["", "  ", "  ;  "] {
        let cfg = SandboxCfg::parse(raw);
        let err = rxrs::fs::op_read(&cfg, &f.to_string_lossy()).unwrap_err();
        assert!(err.contains("路径越界（沙盒外）"), "{}", err);
        // stat / list 同样拒绝，且消息与 Python 侧逐字一致
        let err2 = rxrs::fs::op_stat(&cfg, &f.to_string_lossy()).unwrap_err();
        assert!(err2.contains("路径越界（沙盒外）"), "{}", err2);
    }
}

#[test]
fn star_is_explicit_open() {
    let t = TempDir::new("star");
    let f = write_rel(t.path(), "x.txt", "hi");
    let cfg = SandboxCfg::parse("*");
    let r = rxrs::fs::op_stat(&cfg, &f.to_string_lossy()).unwrap();
    assert_eq!(r.get("exists"), Some(&Value::Bool(true)));
}

#[test]
fn whitelist_allows_inside_denies_outside() {
    let t = TempDir::new("wl");
    let f = write_rel(t.path(), "in.txt", "hi");
    let root = t.path().to_string_lossy().into_owned();
    let cfg = SandboxCfg::parse(&root);
    let r = rxrs::fs::op_read(&cfg, &f.to_string_lossy()).unwrap();
    assert_eq!(get_str(&r, "content"), "hi");
    let err = rxrs::fs::op_read(&cfg, r"C:\Windows\win.ini").unwrap_err();
    assert!(err.contains("路径越界（沙盒外）"), "{}", err);
}

#[test]
fn traversal_escape_denied() {
    let t = TempDir::new("trav");
    let outside = TempDir::new("trav-out");
    let f = write_rel(outside.path(), "secret.txt", "s");
    let root = t.path().to_string_lossy().into_owned();
    let cfg = SandboxCfg::parse(&root);
    let sneak = format!("{}\\..\\..\\{}", root, f.file_name().unwrap().to_string_lossy());
    let err = rxrs::fs::op_read(&cfg, &sneak).unwrap_err();
    assert!(err.contains("路径越界（沙盒外）"), "{}", err);
}

#[test]
fn garbage_roots_tolerated_like_python_abspath() {
    // Python 侧 abspath 恒成功：垃圾根只是永远匹配不上，不得让整个解析报错
    let t = TempDir::new("garbage");
    let f = write_rel(t.path(), "x.txt", "hi");
    let raw = format!("zzz-no-such-root;;;{};", t.path().to_string_lossy());
    let cfg = SandboxCfg::parse(&raw);
    let r = rxrs::fs::op_read(&cfg, &f.to_string_lossy()).unwrap();
    assert_eq!(get_str(&r, "content"), "hi");
}

#[test]
fn case_insensitive_root_match() {
    let t = TempDir::new("case");
    let f = write_rel(t.path(), "x.txt", "hi");
    let root = t.path().to_string_lossy().into_owned();
    let lowered = if let Some(rest) = root.strip_prefix("C:") {
        format!("c:{}", rest)
    } else {
        root.to_lowercase()
    };
    let cfg = SandboxCfg::parse(&lowered);
    let r = rxrs::fs::op_read(&cfg, &f.to_string_lossy()).unwrap();
    assert_eq!(get_str(&r, "content"), "hi");
}

// ---------- 宽限 realpath（S79 修正的核心） ----------

#[test]
fn nonexistent_inside_sandbox_stats_as_missing() {
    let t = TempDir::new("missing");
    let root = t.path().to_string_lossy().into_owned();
    let cfg = SandboxCfg::parse(&root);
    let ghost = format!("{}\\no\\such\\file.txt", root);
    let r = rxrs::fs::op_stat(&cfg, &ghost).unwrap();
    assert_eq!(r.get("exists"), Some(&Value::Bool(false)));
    let p = get_str(&r, "path");
    assert!(p.starts_with(root.trim_end_matches(['\\', '/'])), "{}", p);
}

#[test]
fn relative_path_resolves_against_cwd() {
    let cfg = SandboxCfg::parse("*");
    let r = rxrs::fs::op_stat(&cfg, "Cargo.toml").unwrap();
    assert_eq!(r.get("exists"), Some(&Value::Bool(true)));
}

// ---------- fs_read 行为 ----------

#[test]
fn read_normalizes_crlf_and_cr() {
    let t = TempDir::new("crlf");
    let f = write_rel(t.path(), "w.txt", "a\r\nb\rc\n");
    let cfg = SandboxCfg::parse("*");
    let r = rxrs::fs::op_read(&cfg, &f.to_string_lossy()).unwrap();
    assert_eq!(get_str(&r, "content"), "a\nb\nc\n");
    assert_eq!(get_int(&r, "size"), 7); // 字节数（a\r\nb\rc\n，替换前的大小）
}

#[test]
fn read_oversize_rejected_with_size() {
    let t = TempDir::new("big");
    let f = t.path().join("big.bin");
    fs::write(&f, vec![b'A'; 1_000_001]).unwrap();
    let cfg = SandboxCfg::parse("*");
    let r = rxrs::fs::op_read(&cfg, &f.to_string_lossy()).unwrap();
    assert!(is_err_obj(&r).contains("文件过大"), "{}", is_err_obj(&r));
    assert_eq!(get_int(&r, "size"), 1_000_001);
}

#[test]
fn read_dir_and_missing_give_not_a_file() {
    let t = TempDir::new("notfile");
    let cfg = SandboxCfg::parse("*");
    let r = rxrs::fs::op_read(&cfg, &t.path().to_string_lossy()).unwrap();
    assert!(is_err_obj(&r).starts_with("不是文件或不存在"), "{}", is_err_obj(&r));
    let ghost = t.path().join("ghost.txt");
    let r2 = rxrs::fs::op_read(&cfg, &ghost.to_string_lossy()).unwrap();
    assert!(is_err_obj(&r2).contains("ghost.txt"), "{}", is_err_obj(&r2));
}

// ---------- fs_list 行为 ----------

#[test]
fn list_sorted_with_depth_clamp() {
    let t = TempDir::new("list");
    write_rel(t.path(), "b\\sub\\3.txt", "3");
    write_rel(t.path(), "a\\1.txt", "1");
    write_rel(t.path(), "2.txt", "2");
    let cfg = SandboxCfg::parse("*");
    let root = t.path().to_string_lossy().into_owned();

    // 深度语义与 Python 实测对齐（双实现对照实验，S79）：depth=N 列 N+1 层。
    // 唯一归正：Python 的 `depth or 1` 把字面 0 强制成 1，Rust 侧 0 = 仅根层。
    // 本树根层：2.txt / a / b（1.txt 在 a 下）。
    let r0 = rxrs::fs::op_list(&cfg, &root, 0).unwrap();
    assert_eq!(get_int(&r0, "total"), 3, "depth=0 仅根层（S79 归正）");
    let r = rxrs::fs::op_list(&cfg, &root, 1).unwrap();
    assert_eq!(get_int(&r, "total"), 5);
    let names: Vec<&str> = match r.get("entries") {
        Some(Value::Arr(xs)) => xs.iter().map(|e| get_str(e, "name")).collect(),
        other => panic!("entries 应为数组，实得 {:?}", other),
    };
    // DFS 序：根层按名排序，目录项后立即跟其子层；rel 名含子目录前缀
    assert_eq!(names, vec!["2.txt", "a", "a\\1.txt", "b", "b\\sub"]);

    // depth=2：+b\sub\3.txt = 6 项
    let r2 = rxrs::fs::op_list(&cfg, &root, 2).unwrap();
    assert_eq!(get_int(&r2, "total"), 6);

    // 深度钳制 0..=4：99 与 4 等价（本树 3 层全出）
    let r3 = rxrs::fs::op_list(&cfg, &root, 99).unwrap();
    assert_eq!(get_int(&r3, "total"), 6);

    // 目录项无 size 字段，文件项有
    match r.get("entries") {
        Some(Value::Arr(xs)) => {
            let dir = xs.iter().find(|e| get_str(e, "name") == "a").unwrap();
            assert!(dir.get("size").is_none(), "dir 不应带 size");
            let file = xs.iter().find(|e| get_str(e, "name") == "a\\1.txt").unwrap();
            assert_eq!(get_int(file, "size"), 1);
        }
        _ => panic!("entries 缺失"),
    }
}

#[test]
fn list_not_a_dir() {
    let t = TempDir::new("listdir");
    let f = write_rel(t.path(), "f.txt", "x");
    let cfg = SandboxCfg::parse("*");
    let r = rxrs::fs::op_list(&cfg, &f.to_string_lossy(), 1).unwrap();
    assert!(is_err_obj(&r).starts_with("不是目录"), "{}", is_err_obj(&r));
}
