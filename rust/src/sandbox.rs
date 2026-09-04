//! sandbox —— Rust 侧沙盒钳制，等价复刻 tools/fs.py::_resolve 语义（迁移红线）。
//!
//! 语义（与 Python 侧一致）：
//! - `UNIFIED_RX_SANDBOX` 未设置/空白/全垃圾 → 一律拒绝（Python 侧空白名单同路径，
//!   错误消息同为「路径越界（沙盒外）」——不另立词表）；
//! - `"*"` → 不限（可信宿主显式全开）；
//! - 其余按 `;` 分隔的根目录白名单，路径规范化后必须落在某个根内。
//!
//! 与 S78 版的差异（S79 fs 迁移时发现并修正）：
//! - **宽限 realpath**：`fs::canonicalize` 对不存在的路径直接失败，而 Python
//!   `realpath(strict=False)` 容忍——fs_stat 的 `{exists: false}`、fs_write 建目录
//!   都依赖这点。改为「最深存在祖先 canonicalize + 余尾拼接」；
//! - 沙盒根不再要求可解析（Python `abspath` 恒成功，垃圾根只是永远匹配不上）；
//! - Windows：canonicalize 产出 `\\?\` 前缀，比较前剥掉；比较不区分大小写
//!   （与 normcase 语义对齐）。

use std::path::{Path, PathBuf};

/// 沙盒配置：从环境变量解析一次，之后可复用（可测，不依赖进程全局 env）。
pub enum SandboxCfg {
    /// 显式全开（"*"）
    Open,
    /// 白名单根（已规范化）；空 = fail-closed
    Roots(Vec<PathBuf>),
}

impl SandboxCfg {
    pub fn from_env() -> SandboxCfg {
        let raw = std::env::var("UNIFIED_RX_SANDBOX").unwrap_or_default();
        Self::parse(&raw)
    }

    /// Python `_sandbox_roots` 同语义：空白串=未配置；"*"=全开；";" 分隔收非空条目。
    pub fn parse(raw: &str) -> SandboxCfg {
        let raw = raw.trim();
        if raw.is_empty() {
            return SandboxCfg::Roots(Vec::new());
        }
        if raw == "*" {
            return SandboxCfg::Open;
        }
        SandboxCfg::Roots(raw.split(';').filter_map(|p| {
            let p = p.trim();
            if p.is_empty() {
                None
            } else {
                Some(lenient_realpath(Path::new(p)))
            }
        }).collect())
    }

    /// 校验 path 落在沙盒内，返回规范化绝对路径；越界报错文本与 Python 侧逐字一致。
    pub fn resolve(&self, path: &Path) -> Result<PathBuf, String> {
        let can = lenient_realpath(path);
        match self {
            SandboxCfg::Open => Ok(can),
            SandboxCfg::Roots(roots) => {
                let cp = can.to_string_lossy().to_lowercase();
                for r in roots {
                    let rp = r.to_string_lossy().to_lowercase();
                    let rp = rp.trim_end_matches(['\\', '/']);
                    if cp == rp
                        || cp.starts_with(&format!("{}\\", rp))
                        || cp.starts_with(&format!("{}/", rp))
                    {
                        return Ok(can);
                    }
                }
                Err(format!("路径越界（沙盒外）: {}", path.display()))
            }
        }
    }
}

/// 便捷入口：直接读进程 env（rx-taint 等单发进程用）。
pub fn resolve(path: &Path) -> Result<PathBuf, String> {
    SandboxCfg::from_env().resolve(path)
}

/// 宽限 realpath：等价 Python `realpath(strict=False)`。
/// 存在 → canonicalize（解析 symlink/junction）；不存在 → 最深存在祖先
/// canonicalize 后拼回余尾；全链不可解析 → 返回 cwd 锚定的规范化路径。
pub fn lenient_realpath(p: &Path) -> PathBuf {
    let p = if p.is_relative() {
        match std::env::current_dir() {
            Ok(cwd) => cwd.join(p),
            Err(_) => p.to_path_buf(),
        }
    } else {
        p.to_path_buf()
    };
    if let Ok(c) = std::fs::canonicalize(&p) {
        return strip_unc(c);
    }
    let mut tails: Vec<std::ffi::OsString> = Vec::new();
    let mut anc = p.clone();
    while let Some(parent) = anc.parent() {
        if let Some(f) = anc.file_name() {
            tails.push(f.to_owned());
        }
        anc = parent.to_path_buf();
        if anc.as_os_str().is_empty() {
            break;
        }
        if let Ok(c) = std::fs::canonicalize(&anc) {
            let mut out = strip_unc(c);
            for t in tails.iter().rev() {
                out.push(t);
            }
            return out;
        }
    }
    p
}

fn strip_unc(p: PathBuf) -> PathBuf {
    let s = p.to_string_lossy().to_string();
    match s.strip_prefix(r"\\?\") {
        Some(rest) => PathBuf::from(rest),
        None => p,
    }
}
