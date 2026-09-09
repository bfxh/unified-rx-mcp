//! fs —— 文件层四工具的 Rust 原生实现（S79 读面 / S90 写面收官，spec/VULN-HUNTING.md 五）。
//!
//! 等价复刻 tools/fs.py 的 fs_read / fs_stat / fs_list / fs_write。契约关键点：
//! - 沙盒拒绝（resolve 层）→ `Err`：exe 以退出码 2 退出，Python 壳 raise
//!   ValueError → registry 包成 `ok:false`（与旧实现抛 ValueError 同包络）；
//! - 工具级结果（不是文件/过大/不是目录）→ `Ok(Obj)`：正常返回，error 走
//!   result 字段（与旧实现返回 dict 同包络）；
//! - 错误消息文本逐字对齐 Python 侧（pytest 契约测试有断言）。

use std::path::Path;

use crate::json::Value;
use crate::sandbox::SandboxCfg;

pub const MAX_BYTES: i128 = 1_000_000;

/// fs_read：安全读取文件（≤1MB，沙盒校验，universal newlines 归一）。
pub fn op_read(cfg: &SandboxCfg, orig: &str) -> Result<Value, String> {
    let p = cfg.resolve(Path::new(orig))?;
    let md = match std::fs::metadata(&p) {
        Ok(m) => m,
        Err(_) => {
            return Ok(err_obj(&format!("不是文件或不存在: {}", orig)));
        }
    };
    if !md.is_file() {
        return Ok(err_obj(&format!("不是文件或不存在: {}", orig)));
    }
    let size = md.len() as i128;
    if size > MAX_BYTES {
        return Ok(Value::Obj(vec![
            ("error".into(), Value::Str(format!("文件过大（{} > {}），拒绝读取", size, MAX_BYTES))),
            ("size".into(), Value::Int(size)),
        ]));
    }
    let bytes = std::fs::read(&p).map_err(|e| format!("读取失败: {}", e))?;
    // Python 侧 open(text) 默认 universal newlines：\r\n 与 \r 都归一为 \n
    let content = universal_newlines(&String::from_utf8_lossy(&bytes));
    Ok(Value::Obj(vec![
        ("path".into(), Value::Str(p.to_string_lossy().into_owned())),
        ("size".into(), Value::Int(size)),
        ("content".into(), Value::Str(content)),
    ]))
}

/// fs_write：安全写入文件（≤1MB，S90 原生化——fs 域 4/4 收官）。
/// 授权门不在此处：requires_auth 由 Python registry 统一强制（S86 决策：exe 永不
/// 自行放权）。内容经 stdin 字节通道到达（argv 不传内容，绕开 Windows 命令行
/// 32767 码元上限）；字节原样落盘——等价 Python `open(..., newline="\n")` 无换行
/// 翻译（S90 探针实锤 text 模式 stdin 会做 \n→os.linesep 翻译，壳侧同走二进制）；
/// size 按 Unicode 标量字符数计（等价 Python len(str)，非字节数）。
/// 顺序对齐旧实现：先大小上限后沙盒 resolve（超大+越界同中时"内容过大"先报）。
pub fn op_write(cfg: &SandboxCfg, orig: &str, content: &[u8]) -> Result<Value, String> {
    if content.len() > MAX_BYTES as usize {
        return Ok(err_obj(&format!("内容过大（{} > {} 字节）", content.len(), MAX_BYTES)));
    }
    let p = cfg.resolve(Path::new(orig))?;
    let text = match std::str::from_utf8(content) {
        Ok(t) => t,
        Err(_) => return Ok(err_obj("content 非 UTF-8（宿主通道损坏）")),
    };
    if let Some(d) = p.parent()
        && !d.as_os_str().is_empty() && !d.is_dir() {
            // S95 高压电池（8 线程同靶写）验收：并发建目录允许瞬时竞争，
            // 失败后复查一次（对手可能已建好）；真实失败（父路径是文件等）
            // 立即报错，不做长退避——resolve 层已修掉 $Deleted 幽灵路径，
            // 这里只需 os.makedirs(exist_ok=True) 级的容忍度。
            let mut made = false;
            let mut last_err: Option<std::io::Error> = None;
            for _ in 0..3 {
                match std::fs::create_dir_all(d) {
                    Ok(()) => {
                        made = true;
                        break;
                    }
                    Err(e) => {
                        last_err = Some(e);
                        if d.is_dir() {
                            made = true;
                            break;
                        }
                        std::thread::sleep(std::time::Duration::from_micros(200));
                    }
                }
            }
            if !made {
                return Ok(err_obj(&format!(
                    "创建目录失败: {}",
                    last_err.map(|e| e.to_string()).unwrap_or_default()
                )));
            }
        }
    // S62 原子写同款：tmp+replace，崩进程不留半截文件。tmp 名带进程级序列号：
    // 同进程并发写同靶时 pid 撞车会互踩 tmp（exe 单 op 一进程不触发，库函数
    // 直接并发调用会触发——S95 并发契约测试 pin 死）。
    static WRITE_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let tmp = p.with_file_name(format!(
        "{}.urxtmp{}-{}",
        p.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default(),
        std::process::id(),
        WRITE_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
    ));
    let outcome = std::fs::write(&tmp, content).and_then(|()| std::fs::rename(&tmp, &p));
    if let Err(e) = outcome {
        let _ = std::fs::remove_file(&tmp);   // 与旧实现 except 分支同款尽力清理
        return Ok(err_obj(&format!("写入失败: {}", e)));
    }
    Ok(Value::Obj(vec![
        ("path".into(), Value::Str(p.to_string_lossy().into_owned())),
        ("size".into(), Value::Int(text.chars().count() as i128)),
        ("ok".into(), Value::Bool(true)),
    ]))
}

/// fs_stat：文件元信息（存在/大小/mtime）。不存在的路径返回 exists:false 而非报错。
pub fn op_stat(cfg: &SandboxCfg, orig: &str) -> Result<Value, String> {
    let p = cfg.resolve(Path::new(orig))?;
    let md = match std::fs::metadata(&p) {
        Ok(m) => m,
        Err(_) => {
            return Ok(Value::Obj(vec![
                ("exists".into(), Value::Bool(false)),
                ("path".into(), Value::Str(p.to_string_lossy().into_owned())),
            ]));
        }
    };
    Ok(Value::Obj(vec![
        ("exists".into(), Value::Bool(true)),
        ("path".into(), Value::Str(p.to_string_lossy().into_owned())),
        ("is_file".into(), Value::Bool(md.is_file())),
        ("is_dir".into(), Value::Bool(md.is_dir())),
        ("size".into(), Value::Int(md.len() as i128)),
        ("mtime".into(), Value::Int(mtime_secs(&md))),
    ]))
}

/// fs_list：列目录（深度 0..=4，默认 1；每层按名排序）。
pub fn op_list(cfg: &SandboxCfg, orig: &str, depth: i64) -> Result<Value, String> {
    let p = cfg.resolve(Path::new(orig))?;
    if !std::fs::metadata(&p).map(|m| m.is_dir()).unwrap_or(false) {
        return Ok(err_obj(&format!("不是目录: {}", orig)));
    }
    let depth = depth.clamp(0, 4);
    let root = p.clone();
    let mut entries: Vec<Value> = Vec::new();
    walk(&p, 0, depth, &root, &mut entries);
    let total = entries.len() as i128;
    Ok(Value::Obj(vec![
        ("path".into(), Value::Str(p.to_string_lossy().into_owned())),
        ("total".into(), Value::Int(total)),
        ("entries".into(), Value::Arr(entries)),
    ]))
}

fn walk(d: &Path, cur: i64, depth: i64, root: &Path, out: &mut Vec<Value>) {
    if cur > depth {
        return;
    }
    let rd = match std::fs::read_dir(d) {
        Ok(r) => r,
        Err(_) => return, // 与 Python except OSError: return 同语义：该层静默缺席
    };
    let mut items: Vec<std::path::PathBuf> = rd.filter_map(|e| e.ok()).map(|e| e.path()).collect();
    items.sort_by(|a, b| {
        a.file_name().map(|f| f.to_string_lossy().into_owned())
            .unwrap_or_default()
            .cmp(&b.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default())
    });
    for full in items {
        let rel = full.strip_prefix(root).unwrap_or(&full).to_string_lossy().into_owned();
        let is_dir = std::fs::metadata(&full).map(|m| m.is_dir()).unwrap_or(false);
        if is_dir {
            out.push(Value::Obj(vec![
                ("name".into(), Value::Str(rel)),
                ("type".into(), Value::Str("dir".into())),
            ]));
            walk(&full, cur + 1, depth, root, out);
        } else {
            let sz = std::fs::metadata(&full).map(|m| m.len() as i128).unwrap_or(-1);
            out.push(Value::Obj(vec![
                ("name".into(), Value::Str(rel)),
                ("type".into(), Value::Str("file".into())),
                ("size".into(), Value::Int(sz)),
            ]));
        }
    }
}

fn err_obj(msg: &str) -> Value {
    Value::Obj(vec![("error".into(), Value::Str(msg.into()))])
}

fn universal_newlines(s: &str) -> String {
    if !s.contains('\r') {
        return s.to_string();
    }
    s.replace("\r\n", "\n").replace('\r', "\n")
}

fn mtime_secs(md: &std::fs::Metadata) -> i128 {
    match md.modified() {
        Ok(t) => match t.duration_since(std::time::UNIX_EPOCH) {
            Ok(d) => d.as_secs() as i128,
            Err(e) => -(e.duration().as_secs() as i128),
        },
        Err(_) => 0,
    }
}
