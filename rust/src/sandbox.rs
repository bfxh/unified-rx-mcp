//! sandbox —— Rust 侧沙盒钳制，等价复刻 tools/fs.py::_resolve 语义（迁移红线）。
//!
//! 语义（与 Python 侧一致）：
//! - `UNIFIED_RX_SANDBOX` 未设置/为空 → fail-closed，一律拒绝；
//! - `"*"` → 不限（可信宿主显式全开）；
//! - 其余按 `;` 分隔的根目录白名单，路径必须规范化后落在某个根内。
//!
//! Windows 细节：`fs::canonicalize` 产出 `\\?\C:\...` 前缀，比较前剥掉；
//! 路径比较不区分大小写（与 os.path.normcase 行为对齐）。

use std::path::{Path, PathBuf};

/// 返回 Ok(None) 表示 "*" 全开；Ok(Some(roots)) 为白名单根。
pub fn roots() -> Result<Option<Vec<PathBuf>>, String> {
    let raw = std::env::var("UNIFIED_RX_SANDBOX").unwrap_or_default();
    let raw = raw.trim().to_string();
    if raw.is_empty() {
        return Err("沙盒未配置：UNIFIED_RX_SANDBOX 未设置，fail-closed 拒绝".to_string());
    }
    if raw == "*" {
        return Ok(None);
    }
    let mut out = Vec::new();
    for part in raw.split(';') {
        let p = part.trim();
        if p.is_empty() {
            continue;
        }
        let pb = PathBuf::from(p);
        let can = std::fs::canonicalize(&pb)
            .map_err(|e| format!("沙盒根不可解析 {:?}: {}", pb, e))?;
        out.push(strip_unc(can));
    }
    if out.is_empty() {
        return Err("沙盒未配置：白名单为空".to_string());
    }
    Ok(Some(out))
}

fn strip_unc(p: PathBuf) -> PathBuf {
    let s = p.to_string_lossy().to_string();
    match s.strip_prefix(r"\\?\") {
        Some(rest) => PathBuf::from(rest),
        None => p,
    }
}

/// 规范化并校验 path 落在沙盒内；成功返回规范化后的绝对路径。
pub fn resolve(path: &Path) -> Result<PathBuf, String> {
    let roots = roots()?;
    let can = std::fs::canonicalize(path)
        .map_err(|_| format!("路径不可解析: {}", path.display()))?;
    let can = strip_unc(can);
    let roots = match roots {
        None => return Ok(can), // "*"
        Some(r) => r,
    };
    let cp = can.to_string_lossy().to_lowercase();
    for r in roots {
        let rp = r.to_string_lossy().to_lowercase();
        let rp = rp.trim_end_matches(['\\', '/']);
        if cp == rp || cp.starts_with(&format!("{}\\", rp)) || cp.starts_with(&format!("{}/", rp)) {
            return Ok(can);
        }
    }
    Err(format!("路径越界（沙盒外）: {}", path.display()))
}
