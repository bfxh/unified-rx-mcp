//! rxrs —— unified-rx 的 Rust 化（S78 地基，S79 起逐域原生实现；
//! 路线图见 spec/VULN-HUNTING.md 五·Rust 迁移路线图）。
//!
//! 纪律：零第三方 crate（Cargo [dependencies] 恒空），与 Python 侧"纯 stdlib"同等级。
//! 模块：
//! - `json` — 手写 JSON 解析/序列化（限深 512 防栈溢出，fuzz 电池深嵌套用例的靶）
//! - `sandbox` — 沙盒钳制（等价复刻 tools/fs.py::_resolve 语义：fail-closed / "*" / ; 分隔；
//!   宽限 realpath 容忍不存在路径——S79 修正）
//! - `fs` — 文件层四工具原生实现（fs_read/fs_stat/fs_list S79 + fs_write S90
//!   写面收官——stdin 字节通道 + tmp+replace 原子写）
//! - `search` — code_search 原生实现（S80：BM25 + 手写分词器 + 行重排）
//! - `sem` — code_semantic 原生实现（S81：符号定义 tf-idf 余弦 + 手写定义匹配器）
//! - `scan` — scan 域轻正则三工具（S82：std_check/ui_check/bug_locate + 遍历契约）
//! - `pyast` — 手写 Python 迷你解析器（S83：ASDL 字段序 children + Ctx 标记，
//!   3.14 SyntaxError 消息对齐；ast_scan 的 S84 地基）
//! - `bug` — bug_scan 原生实现（S83：Python 迷你 AST 规则 + Rust 生产规则 +
//!   通用正则 + 聚合，与 tools/scan.py 逐条对齐）
//! - `astscan` — ast_scan 原生实现（S84：Python 真 AST 规则 / JS 词法掩码+调用面 /
//!   Rust 结构化信号 + S16 可达性，与 tools/astscan.py 逐条对齐）
//! - `appaudit` — app_audit 原生实现（S85：审计沙盒门 + 行级规则/秘密掩码/URL 盘点
//!   asar SHA256 自标定提取复扫，与 tools/appaudit.py 逐条对齐；
//!   `sha256` 为手写零依赖实现。注：续行不以 `+` 起头——rustdoc 会当成列表符）
//! - `appclone` — app_clone/app_clean 原生实现（S86：appaudit 域写面收官——授权门
//!   仍留 Python registry；Python 3.14 junction 真值 + Win32 relpath 归一
//!   + GetLocalTime FFI 时间戳，与 tools/appaudit.py 逐条对齐）
//! - `taint` — 污点引擎（Python 子集词法 + 缩进作用域 + 来源→汇点浅数据流）
//! - `sketch` — n-gram bottom-k MinHash 指纹（S119：near_dupes 的 sketch 原生化，
//!   stdin 帧流 + std::thread 分块并行，与 Python/GPU 口径逐位一致）
//! - `xorscan` — 单字节异或密钥枚举（S120：file_scan 的 xor_crib 原生化，
//!   密钥分片并行，饱和计数 256，16MB 实测 365ms vs GPU 1279ms）
//! - `server` — MCP stdio 协议层（rx-mcp：独立协议实现——解析/分发/tools+ping 直答、
//!   通知静默；S78 落地形态，转发代理 S79 评估后维持缓议）

pub mod appaudit;
pub mod astgrep;
pub mod appclone;
pub mod astscan;
pub mod bug;
pub mod fs;
pub mod ide;
pub mod json;
pub mod nameres;
pub mod pyast;
pub mod repomap;
pub mod scan;
pub mod sandbox;
pub mod search;
pub mod sem;
pub mod server;
pub mod sha256;
pub mod sketch;
pub mod xorscan;
pub mod taint;
