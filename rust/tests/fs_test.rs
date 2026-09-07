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

// ---------- fs_write（S90，fs 域收官） ----------

#[test]
fn write_ok_bytes_verbatim_and_char_size() {
    let t = TempDir::new("wr-ok");
    let cfg = SandboxCfg::parse("*");
    let p = t.path().join("nested").join("w.txt");
    let content = "你好\nworld\r\nend";   // 13 字符 / 17 字节（\r\n 不得被翻译）
    let r = rxrs::fs::op_write(&cfg, &p.to_string_lossy(), content.as_bytes()).unwrap();
    assert_eq!(get_int(&r, "size"), 13);
    assert_eq!(r.get("ok"), Some(&Value::Bool(true)));
    assert_eq!(fs::read(&p).unwrap(), content.as_bytes());
    // CJK 字符数 vs 字节数：size 报 len(str) 语义
    let r2 = rxrs::fs::op_write(&cfg, &p.to_string_lossy(), "你好".as_bytes()).unwrap();
    assert_eq!(get_int(&r2, "size"), 2);
    assert_eq!(fs::read(&p).unwrap(), "你好".as_bytes());
}

#[test]
fn write_overwrite_and_empty() {
    let t = TempDir::new("wr-ow");
    let cfg = SandboxCfg::parse("*");
    let p = t.path().join("w.txt");
    let _ = rxrs::fs::op_write(&cfg, &p.to_string_lossy(), b"first").unwrap();
    let r = rxrs::fs::op_write(&cfg, &p.to_string_lossy(), b"second").unwrap();
    assert_eq!(get_int(&r, "size"), 6);
    assert_eq!(fs::read(&p).unwrap(), b"second");
    let r2 = rxrs::fs::op_write(&cfg, &p.to_string_lossy(), b"").unwrap();
    assert_eq!(get_int(&r2, "size"), 0);
    assert_eq!(fs::read(&p).unwrap(), b"");
}

#[test]
fn write_size_cap_exact_and_over() {
    let t = TempDir::new("wr-cap");
    let cfg = SandboxCfg::parse("*");
    let p = t.path().join("cap.txt");
    // 恰好等于上限：放行
    let ok = rxrs::fs::op_write(&cfg, &p.to_string_lossy(), &vec![b'a'; 1_000_000]).unwrap();
    assert_eq!(ok.get("ok"), Some(&Value::Bool(true)));
    // 超一字节：工具级错误，消息逐字对齐旧 Python
    let over = rxrs::fs::op_write(&cfg, &p.to_string_lossy(), &vec![b'a'; 1_000_001]).unwrap();
    assert_eq!(is_err_obj(&over), "内容过大（1000001 > 1000000 字节）");
    // 顺序钉死：超大 + 越界同中时"内容过大"先报（旧实现先查大小后 resolve）
    let both = rxrs::fs::op_write(&cfg, r"C:\Windows\s90-rs-probe.txt", &vec![b'a'; 1_000_001]).unwrap();
    assert_eq!(is_err_obj(&both), "内容过大（1000001 > 1000000 字节）");
}

#[test]
fn write_outside_sandbox_refused() {
    let t = TempDir::new("wr-box");
    let cfg = SandboxCfg::parse(t.path().to_string_lossy().as_ref());
    let err = rxrs::fs::op_write(&cfg, r"C:\Windows\win.ini", b"x").unwrap_err();
    assert!(err.contains("路径越界（沙盒外）"), "{}", err);
}

#[test]
fn write_fail_envelopes_and_no_residue() {
    let t = TempDir::new("wr-fail");
    let cfg = SandboxCfg::parse("*");
    // 目标是目录 → 写入失败，且无 .urxtmp 残留
    let dir = t.path().join("adir");
    fs::create_dir_all(&dir).unwrap();
    let r1 = rxrs::fs::op_write(&cfg, &dir.to_string_lossy(), b"x").unwrap();
    assert!(is_err_obj(&r1).starts_with("写入失败"), "{}", is_err_obj(&r1));
    // 父路径是文件 → 创建目录失败
    let f = write_rel(t.path(), "seed.txt", "s");
    let r2 = rxrs::fs::op_write(&cfg, &f.join("child.txt").to_string_lossy(), b"x").unwrap();
    assert!(is_err_obj(&r2).starts_with("创建目录失败"), "{}", is_err_obj(&r2));
    // 两处失败路径都不得留下半截 tmp 文件
    let residue: Vec<_> = fs::read_dir(t.path()).unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.contains(".urxtmp"))
        .collect();
    assert!(residue.is_empty(), "残留 tmp: {:?}", residue);
}
