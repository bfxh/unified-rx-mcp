//! appclone —— appaudit 域写面原生化（S86）：app_clone / app_clean。
//!
//! 等价复刻红线：授权门留在 Python registry（requires_auth，exe 永不自行放权）；
//! 沙盒门复用 appaudit::strictly_under / sandbox_root；清单指纹 sha256::hex。
//!
//! walk 语义按 Python 3.14 实测真值（S86 oracle 探针钉死）：
//! - **junction 不再是 symlink**：islink=False、is_dir=True（悬空也算目录）——
//!   有效 junction 照进并克隆目标内容，悬空的 read_dir 失败按 os.walk onerror
//!   静默剪枝；两者都不进 skipped_links（那只数真 symlink）；
//! - 真 symlink：有效目录链 = dirnames 但不下钻（无 mkdir/无计数）；
//!   文件链与断链 = filenames 分支 → skipped_links += 1；
//! - os.path.relpath 的 abspath 走 Win32 GetFullPathName：路径成分尾部
//!   空格/点被剥掉（`name. .` → `name`）——errors 条目须同样归一。

use std::io::Write as _;
use std::path::{Path, PathBuf};

use crate::appaudit::{sandbox_root, strictly_under};
use crate::json::Value;
use crate::sandbox::lenient_realpath;
use crate::sha256;

// ---------- 入口 ----------

pub fn app_clone(src: &str, max_files: &str, max_bytes: &str) -> Value {
    app_clone_under(src, max_files, max_bytes, &sandbox_root())
}

pub fn app_clean(target: &str) -> Value {
    app_clean_under(target, &sandbox_root())
}

pub fn app_clone_under(src: &str, max_files: &str, max_bytes: &str, root: &Path) -> Value {
    let trimmed = src.trim();
    if trimmed.is_empty() {
        return err_obj("source_dir 必须是非空字符串");
    }
    let p = Path::new(trimmed);
    if !p.is_absolute() {
        return err_obj("必须绝对路径（防相对路径歧义）；例：D:\\rj\\AI\\Yan Agent");
    }
    let real = match std::fs::canonicalize(p) {
        Ok(r) => strip_unc(r),
        Err(_) => return err_obj(format!("源不存在或不可达: {trimmed}")),
    };
    if !real.is_dir() {
        return err_obj(format!("不是目录: {}", real.display()));
    }
    let max_files = match py_int(max_files) {
        Ok(v) => v as i128,
        Err(m) => return err_obj(m),
    };
    let max_bytes = match py_int(max_bytes) {
        Ok(v) => v as i128,
        Err(m) => return err_obj(m),
    };

    // 唯一落点：时间戳-sha256前12位-净化名（目录名由哈希派生，不吃原始路径注入面）
    let norm = real.to_string_lossy().to_lowercase().replace('/', "\\");
    let tag = sha256::hex(norm.as_bytes())[..12].to_string();
    let safe_name = sanitize_name(&real.file_name().map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default());
    let stem = format!("{}-{tag}", local_stamp());
    let mut dest = root.join(format!("{stem}-{safe_name}"));
    let mut k = 1;
    while dest.exists() {
        k += 1;
        dest = root.join(format!("{stem}-{k}-{safe_name}"));
    }
    if let Err(e) = std::fs::create_dir(&dest) {
        return err_obj(format!("OSError: {e}"));
    }

    let mut st = CloneState {
        copied: 0,
        copied_bytes: 0,
        skipped_links: 0,
        meta_warns: 0,
        read_fails: 0,
        stopped: false,
        truncation: None,
        errors: Vec::new(),
        manifest: Vec::new(),
        plan: 0,
        max_files,
        max_bytes,
    };
    walk_dir(&real, ".", &dest, &mut st);

    // 验证阶段：副本实盘复核（计数不一致必须显式暴露，不静默）
    let (v_files, v_bytes) = verify_dir(&dest);

    Value::Obj(vec![
        ("snapshot".into(), Value::Str(dest.to_string_lossy().into_owned())),
        ("source".into(), Value::Str(real.to_string_lossy().into_owned())),
        ("files".into(), Value::Int(st.copied)),
        ("bytes".into(), Value::Int(st.copied_bytes)),
        ("verify_files".into(), Value::Int(v_files)),
        ("verify_bytes".into(), Value::Int(v_bytes)),
        ("verified".into(), Value::Bool(v_files as usize == st.plan && v_bytes == st.copied_bytes)),
        ("inventory_digest".into(), Value::Str(sha256::hex(&st.manifest))),
        ("skipped_links".into(), Value::Int(st.skipped_links)),
        ("meta_warns".into(), Value::Int(st.meta_warns)),
        ("read_fails".into(), Value::Int(st.read_fails)),
        ("errors".into(), Value::Arr(st.errors.into_iter().map(Value::Str).collect())),
        ("truncated_by".into(), match st.truncation {
            Some(t) => Value::Str(t.into()),
            None => Value::Null,
        }),
    ])
}

pub fn app_clean_under(target: &str, root: &Path) -> Value {
    if target.trim().is_empty() {
        return err_obj("target 必须是非空字符串");
    }
    if !strictly_under(target, root) {
        return err_obj("拒绝：清理目标必须在隔离沙箱内（app_clone 的 snapshot 路径）");
    }
    let p = lenient_realpath(Path::new(target));
    match std::fs::remove_dir_all(&p) {
        Ok(()) => Value::Obj(vec![
            ("removed".into(), Value::Bool(true)),
            ("path".into(), Value::Str(p.to_string_lossy().into_owned())),
        ]),
        Err(e) => err_obj(format!(
            "清理失败: {}: {}（沙箱内可手动重试；多半是文件被占用）",
            py_err_class(&e), e)),
    }
}

// ---------- 克隆遍历（os.walk 预序：先本目录文件，后按 dirnames 序下钻） ----------

struct CloneState {
    copied: i128,
    copied_bytes: i128,
    skipped_links: i128,
    meta_warns: i128,
    read_fails: i128,
    stopped: bool,
    truncation: Option<String>,
    errors: Vec<String>,
    manifest: Vec<u8>,
    plan: usize,
    max_files: i128,
    max_bytes: i128,
}

fn walk_dir(dir: &Path, rel_dir: &str, dest: &Path, st: &mut CloneState) {
    // os.walk onerror 默认忽略：目录打不开 = 静默剪枝（悬空 junction 也走这里）
    let rd = match std::fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => return,
    };
    let mut dirs: Vec<(String, PathBuf)> = Vec::new();
    let mut files: Vec<(String, PathBuf)> = Vec::new();
    for e in rd.filter_map(|e| e.ok()) {
        let name = e.file_name().to_string_lossy().into_owned();
        let Ok(ft) = e.file_type() else { continue };
        let child_rel = if rel_dir == "." { name.clone() } else { format!("{rel_dir}\\{name}") };
        if ft.is_dir() || (ft.is_symlink() && is_junction(&e.path())) {
            // Python 3.14：junction 就是目录（islink=False、悬空也 is_dir=True）
            dirs.push((child_rel, e.path()));
        } else if ft.is_symlink() {
            // 真 symlink：有效目录链 = dirnames 但不下钻（无 mkdir/无计数）；
            // 文件链与断链 = filenames → skipped_links
            match std::fs::metadata(e.path()) {
                Ok(m) if m.is_dir() => {}
                _ => st.skipped_links += 1,
            }
        } else {
            files.push((child_rel, e.path()));
        }
    }

    // 本目录落点（rel_dir="." 是 dest 本身，已建）
    if rel_dir != "." {
        let out_dir = dest.join(rel_dir);
        if let Err(e) = std::fs::create_dir_all(&out_dir) {
            if st.errors.len() < 30 {
                st.errors.push(format!("{rel_dir}: mkdir {}", py_err_class(&e)));
            }
            return; // dirnames[:] = [] + continue：本目录文件与子树全剪
        }
    }
    for (rel, path) in &files {
        if st.stopped {
            break;
        }
        // Python rel 语义：根层文件 rel=""（清单行 "\t{size}\n"），子层为 rel_dir\fn；
        // 清单/计划用 mrel，落盘与错误显示用原 rel
        let mrel = if rel_dir == "." { String::new() } else { rel.replace('\\', "/") };
        process_file(path, rel, &mrel, dest, st);
    }
    if st.stopped {
        return;
    }
    for (rel, path) in &dirs {
        walk_dir(path, rel, dest, st);
    }
}

fn process_file(path: &Path, rel: &str, mrel: &str, dest: &Path, st: &mut CloneState) {
    // 分级：内容复制失败才算丢文件（read_fails）；元数据失败只计警告（meta_warns）。
    // 必须先开源句柄再建目标——源打不开不能留 0 字节残桩污染克隆。
    let md = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(e) => {
            st.read_fails += 1;
            if st.errors.len() < 30 {
                // relpath→abspath 走 Win32 归一：成分尾部空格/点被剥（`name. .` → `name`）
                st.errors.push(format!("{}: {}", win32_display_rel(rel), py_err_class(&e)));
            }
            return;
        }
    };
    if st.copied >= st.max_files {
        st.truncation = Some("max_files".into());
        st.stopped = true;
        return;
    }
    if st.copied_bytes + md.len() as i128 > st.max_bytes {
        st.truncation = Some("max_bytes".into());
        st.stopped = true;
        return;
    }
    let dst = dest.join(rel);
    let copy = (|| -> std::io::Result<()> {
        let mut fin = std::fs::File::open(path)?;
        let mut fout = std::fs::File::create(&dst)?;
        std::io::copy(&mut fin, &mut fout)?;
        fout.flush()?;
        Ok(())
    })();
    if let Err(e) = copy {
        let _ = std::fs::remove_file(&dst); // 残桩清理（源打开失败时不存在，忽略）
        st.read_fails += 1;
        if st.errors.len() < 30 {
            st.errors.push(format!("{}: {}", win32_display_rel(rel), py_err_class(&e)));
        }
        return;
    }
    // copystat：mtime/atime + 只读位；任一失败 meta_warns += 1（整体一次）
    if set_meta(&dst, &md).is_err() {
        st.meta_warns += 1;
    }
    st.copied += 1;
    st.copied_bytes += md.len() as i128;
    st.plan += 1;
    st.manifest.extend_from_slice(format!("{mrel}\t{}\n", md.len()).as_bytes());
}

fn set_meta(dst: &Path, md: &std::fs::Metadata) -> std::io::Result<()> {
    use std::fs::FileTimes;
    let f = std::fs::File::options().write(true).open(dst)?;
    let times = FileTimes::new().set_accessed(md.accessed()?).set_modified(md.modified()?);
    f.set_times(times)?;
    let mut perm = std::fs::metadata(dst)?.permissions();
    perm.set_readonly(md.permissions().readonly());
    std::fs::set_permissions(dst, perm)?;
    Ok(())
}

fn verify_dir(root: &Path) -> (i128, i128) {
    fn rec(dir: &Path, files: &mut i128, bytes: &mut i128) {
        let Ok(rd) = std::fs::read_dir(dir) else { return };
        for e in rd.filter_map(|e| e.ok()) {
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() {
                rec(&e.path(), files, bytes);
            } else if let Ok(m) = e.metadata() {
                *files += 1;
                *bytes += m.len() as i128;
            }
        }
    }
    let (mut f, mut b) = (0, 0);
    rec(root, &mut f, &mut b);
    (f, b)
}

// ---------- 小件 ----------

/// Python int() 的防御性解析（registry schema 已挡非整数，这里兜底 argv 直呼）。
pub fn py_int(s: &str) -> Result<i64, String> {
    let bad = || format!("invalid literal for int() with base 10: '{s}'");
    let t = s.trim();
    let (sign, rest) = match t.strip_prefix('-') {
        Some(r) => (-1i64, r),
        None => (1i64, t.strip_prefix('+').unwrap_or(t)),
    };
    if rest.is_empty() {
        return Err(bad());
    }
    let mut val: i128 = 0;
    let mut last_digit = false;
    for ch in rest.chars() {
        if ch == '_' {
            if !last_digit {
                return Err(bad());
            }
            last_digit = false;
            continue;
        }
        let Some(d) = ch.to_digit(10) else { return Err(bad()) };
        val = val * 10 + d as i128;
        last_digit = true;
    }
    if !last_digit {
        return Err(bad());
    }
    // 饱和到 i64：JSON 整数任意精度，位截断 as 会变号破坏预算语义
    //（Python 无限精度：巨数=形同无上限，巨负数=立即按 max_files 截断）
    let signed = sign as i128 * val;
    Ok(signed.clamp(i64::MIN as i128, i64::MAX as i128) as i64)
}

/// re.sub(r"[^A-Za-z0-9_\-]+", "_", name)[:40] or "app"——连续坏字符合并为单个 `_`。
fn sanitize_name(name: &str) -> String {
    let mut out = String::new();
    let mut in_run = false;
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' {
            out.push(ch);
            in_run = false;
        } else if !in_run {
            out.push('_');
            in_run = true;
        }
    }
    let cut: String = out.chars().take(40).collect();
    if cut.is_empty() { "app".into() } else { cut }
}

/// Win32 GetFullPathName 归一（os.path.relpath→abspath 链路）：成分尾部空格/点剥除。
fn win32_display_rel(rel: &str) -> String {
    rel.split('\\')
        .map(|c| c.trim_end_matches(['.', ' ']))
        .collect::<Vec<_>>()
        .join("\\")
}

/// junction 判别：本工具链 Rust 把 junction 报成 symlink（is_dir()=false）且
/// read_link 已剥 `\??\` 前缀——与真 symlink 文本不可分，必须看 reparse tag。
/// Windows 走 GetFileInformationByHandleEx(FileAttributeTagInfo) 手写 FFI
/// （IO_REPARSE_TAG_MOUNT_POINT），非 Windows 恒 false（无 junction 概念）。
#[cfg(windows)]
fn is_junction(p: &Path) -> bool {
    use std::ffi::c_void;
    use std::os::windows::ffi::OsStrExt as _;

    #[repr(C)]
    struct FileAttributeTagInfo {
        file_attributes: u32,
        reparse_tag: u32,
    }
    unsafe extern "system" {
        fn CreateFileW(
            lpfilename: *const u16,
            dwdesiredaccess: u32,
            dwsharemode: u32,
            lpsecurityattributes: *const c_void,
            dwcreationdisposition: u32,
            dwflagsandattributes: u32,
            htemplatefile: *mut c_void,
        ) -> *mut c_void;
        fn GetFileInformationByHandleEx(
            hfile: *mut c_void,
            fileinformationclass: u32,
            lpfileinformation: *mut c_void,
            dwbufferbytes: u32,
        ) -> i32;
        fn CloseHandle(hobject: *mut c_void) -> i32;
    }
    const FILE_SHARE_READ_WRITE_DELETE: u32 = 1 | 2 | 4;
    const OPEN_EXISTING: u32 = 3;
    const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    const FILE_ATTRIBUTE_TAG_INFO: u32 = 9;
    const IO_REPARSE_TAG_MOUNT_POINT: u32 = 0xA000_0003;

    // \\?\ 前缀：std 内部 fs 调用自带，裸 FFI 需自备（UNC 用 \\?\UNC\ 形态）
    let mut s = p.to_string_lossy().into_owned();
    if !s.starts_with(r"\\?\") {
        if let Some(rest) = s.strip_prefix(r"\\") {
            s = format!(r"\\?\UNC\{rest}");
        } else {
            s = format!(r"\\?\{s}");
        }
    }
    let wide: Vec<u16> = std::ffi::OsStr::new(&s).encode_wide().chain(Some(0)).collect();
    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            0,
            FILE_SHARE_READ_WRITE_DELETE,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            std::ptr::null_mut(),
        )
    };
    if handle.is_null() || handle as usize == usize::MAX {
        return false;
    }
    let mut info = FileAttributeTagInfo { file_attributes: 0, reparse_tag: 0 };
    let ok = unsafe {
        GetFileInformationByHandleEx(
            handle,
            FILE_ATTRIBUTE_TAG_INFO,
            &mut info as *mut _ as *mut c_void,
            std::mem::size_of::<FileAttributeTagInfo>() as u32,
        )
    };
    unsafe { CloseHandle(handle) };
    ok != 0 && info.reparse_tag == IO_REPARSE_TAG_MOUNT_POINT
}

#[cfg(not(windows))]
fn is_junction(_p: &Path) -> bool {
    false
}

fn py_err_class(e: &std::io::Error) -> &'static str {
    match e.kind() {
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::NotADirectory => "NotADirectoryError",
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        _ => match e.raw_os_error() {
            Some(32) | Some(5) => "PermissionError", // 共享冲突/拒绝访问 → CPython 同映射
            _ => "OSError",
        },
    }
}

fn err_obj(m: impl Into<String>) -> Value {
    Value::Obj(vec![("error".into(), Value::Str(m.into()))])
}

/// time.strftime("%Y%m%d-%H%M%S") 等价（本地时区）。Windows 走 GetLocalTime
/// 手写 FFI（零第三方 crate 红线），其余平台用 UTC 兜底（本仓库 exe 只发 Windows）。
fn local_stamp() -> String {
    #[cfg(windows)]
    {
        #[repr(C)]
        #[derive(Default)]
        struct SystemTime {
            year: u16, month: u16, day_of_week: u16, day: u16,
            hour: u16, minute: u16, second: u16, millis: u16,
        }
        unsafe extern "system" { fn GetLocalTime(lp: *mut SystemTime); }
        let mut st = SystemTime::default();
        unsafe { GetLocalTime(&mut st) };
        format!("{:04}{:02}{:02}-{:02}{:02}{:02}",
                st.year, st.month, st.day, st.hour, st.minute, st.second)
    }
    #[cfg(not(windows))]
    {
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64).unwrap_or(0);
        let days = secs.div_euclid(86400);
        let sod = secs.rem_euclid(86400);
        let z = days + 719468;
        let era = z.div_euclid(146097);
        let doe = z.rem_euclid(146097);
        let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
        let mut y = yoe + era * 400;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let d = doy - (153 * mp + 2) / 5 + 1;
        let m = if mp < 10 { mp + 3 } else { mp - 9 };
        if m <= 2 { y += 1; }
        format!("{:04}{:02}{:02}-{:02}{:02}{:02}",
                y, m, d, sod / 3600, (sod % 3600) / 60, sod % 60)
    }
}

fn strip_unc(p: PathBuf) -> PathBuf {
    let s = p.to_string_lossy().to_string();
    match s.strip_prefix(r"\\?\") {
        Some(rest) => PathBuf::from(rest),
        None => p,
    }
}

// ---------- 测试 ----------

#[cfg(test)]
mod tests {
    use super::*;

    fn num(r: &Value, k: &str) -> i128 {
        match r.get(k) { Some(Value::Int(i)) => *i, _ => panic!("{k} 非整型: {}", r.to_json()) }
    }
    fn bl(r: &Value, k: &str) -> bool {
        match r.get(k) { Some(Value::Bool(b)) => *b, _ => panic!("{k} 非布尔: {}", r.to_json()) }
    }
    fn st<'a>(r: &'a Value, k: &str) -> &'a str {
        match r.get(k) { Some(Value::Str(s)) => s, _ => panic!("{k} 非字符串: {}", r.to_json()) }
    }

    #[test]
    fn py_int_python_semantics() {
        assert_eq!(py_int("12").unwrap(), 12);
        assert_eq!(py_int(" 12 ").unwrap(), 12);
        assert_eq!(py_int("+7").unwrap(), 7);
        assert_eq!(py_int("-3").unwrap(), -3);
        assert_eq!(py_int("1_0").unwrap(), 10);
        assert_eq!(py_int("abc").unwrap_err(),
                   "invalid literal for int() with base 10: 'abc'");
        assert!(py_int("1_").is_err());
        assert!(py_int("_1").is_err());
        assert!(py_int("").is_err());
        assert!(py_int("1__0").is_err());
        assert!(py_int("--1").is_err());
    }

    #[test]
    fn sanitize_runs_and_cap() {
        assert_eq!(sanitize_name("Yan Agent"), "Yan_Agent");
        assert_eq!(sanitize_name("中文app"), "_app");
        assert_eq!(sanitize_name("a!!b"), "a_b");
        assert_eq!(sanitize_name(""), "app");
        let long = "x".repeat(50);
        assert_eq!(sanitize_name(&long).chars().count(), 40);
    }

    #[test]
    fn win32_display_strips_trailing_dots_spaces() {
        assert_eq!(win32_display_rel("name. ."), "name");
        assert_eq!(win32_display_rel("sub\\name. ."), "sub\\name");
        assert_eq!(win32_display_rel("UPPER.JS"), "UPPER.JS");
        assert_eq!(win32_display_rel("a\\b. \\c."), "a\\b\\c");
    }

    #[test]
    fn stamp_shape() {
        let s = local_stamp();
        assert_eq!(s.len(), 15);
        assert_eq!(s.as_bytes()[8], b'-');
        assert!(s[..8].chars().all(|c| c.is_ascii_digit()));
        assert!(s[9..].chars().all(|c| c.is_ascii_digit()));
    }

    #[cfg(windows)]
    #[test]
    fn clone_junction_semantics_py314() {
        // Python 3.14 真值：junction = 目录（进、克隆目标内容、不计数）；
        // 悬空 junction = 静默剪枝；真 symlink 目录链 = 不下钻不计数。
        let base = std::env::temp_dir().join(format!("rx-appclone-test-{}",
                                                     std::process::id()));
        let src = base.join("src");
        let dst_root = base.join("sandbox");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(src.join("targetdir")).unwrap();
        std::fs::write(src.join("targetdir").join("t.txt"), b"hi").unwrap();
        std::fs::write(src.join("main.js"), b"const a=1;\n").unwrap();
        let ok_junc = src.join("loop.junc");
        let bad_junc = src.join("broken.junc");
        let status = |j: &Path, t: &Path| std::process::Command::new("cmd")
            .args(["/c", "mklink", "/J"])
            .arg(j).arg(t).output().unwrap();
        assert!(status(&ok_junc, &src.join("targetdir")).status.success());
        assert!(status(&bad_junc, Path::new(r"C:\nonexistent_rx_test_target")).status.success());
        std::fs::create_dir_all(&dst_root).unwrap();

        let r = app_clone_under(&src.to_string_lossy(), "100", "1073741824", &dst_root);
        assert!(r.get("error").is_none(), "{}", r.to_json());
        // main.js + targetdir/t.txt + loop.junc/t.txt（junction 目标内容被克隆）= 3
        assert_eq!(num(&r, "files"), 3, "{}", r.to_json());
        assert_eq!(num(&r, "skipped_links"), 0);
        assert_eq!(num(&r, "read_fails"), 0);
        assert_eq!(bl(&r, "verified"), true);
        let snap = PathBuf::from(st(&r, "snapshot"));
        assert!(snap.join("loop.junc").join("t.txt").is_file());
        assert!(!snap.join("broken.junc").exists(), "悬空 junction 必须静默剪枝");
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn clean_gate_and_class_names() {
        let base = std::env::temp_dir().join(format!("rx-appclean-test-{}",
                                                     std::process::id()));
        let root = base.join("sandbox");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(root.join("in")).unwrap();
        std::fs::create_dir_all(&base).unwrap();

        // 沙盒根本身拒绝；越界拒绝；空拒绝
        let r = app_clean_under(&root.to_string_lossy(), &root);
        assert!(st(&r, "error").contains("拒绝"));
        let r = app_clean_under(&base.parent().unwrap().to_string_lossy(), &root);
        assert!(st(&r, "error").contains("拒绝"));
        let r = app_clean_under("   ", &root);
        assert!(st(&r, "error").contains("必须是非空字符串"));
        // 沙盒内不存在的路径：过门但清理失败，类名 FileNotFoundError
        let r = app_clean_under(&root.join("missing").to_string_lossy(), &root);
        assert!(st(&r, "error").starts_with("清理失败: FileNotFoundError: "));
        // 文件目标：NotADirectoryError
        let f = root.join("note.txt");
        std::fs::write(&f, b"x").unwrap();
        let r = app_clean_under(&f.to_string_lossy(), &root);
        assert!(st(&r, "error").starts_with("清理失败: NotADirectoryError: "));
        // 正常移除
        let dir = root.join("in");
        let r = app_clean_under(&dir.to_string_lossy(), &root);
        assert_eq!(bl(&r, "removed"), true);
        assert!(!dir.exists());
        std::fs::remove_dir_all(&base).unwrap();
    }
}
