//! fs —— 文件层读面三工具的 Rust 原生实现（S79，spec/VULN-HUNTING.md 五）。
//!
//! 等价复刻 tools/fs.py 的 fs_read / fs_stat / fs_list（fs_write 写面按路线图
//! 最后迁移，仍在 Python 侧）。契约关键点：
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
