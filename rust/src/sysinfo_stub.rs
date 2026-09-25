//! sysinfo 的非 Windows 占位实现（S173，跨平台腿实锤：原实现的 advapi32/kernel32/user32
//! FFI 在 linux 链接期直接失败）。
//!
//! 口径 = 本仓"能力探测失败如实报"哲学：P/E 核拓扑、QoS 引导、进程快照是 **Windows
//! 混合架构调度**的能力——非 Windows 平台这些 FFI 不存在，如实返回 unsupported，
//! 绝不假装能做（宿主侧 tools/sysinfo 的能力探测会拿到 error 并如实降级）。
//! 真实现（cfg(windows)）在 sysinfo.rs，两模块公开签名同形。

use crate::json::Value;

fn unsupported(op: &str) -> Value {
    Value::Obj(vec![(
        "error".into(),
        Value::Str(format!(
            "unsupported: sysinfo 域（{op}）仅 Windows（P/E 核拓扑 / QoS 引导 / 进程快照 FFI）"
        )),
    )])
}

pub fn topology() -> Value {
    unsupported("topology")
}

pub fn devices() -> Value {
    unsupported("devices")
}

pub fn procs(_filter: &str) -> Value {
    unsupported("procs")
}

pub fn threads(_pid: u32) -> Value {
    unsupported("threads")
}

pub fn process_affinity(_pid: u32) -> Value {
    unsupported("affinity")
}

pub fn enable_debug_privilege() -> (bool, String) {
    (
        false,
        "unsupported: SeDebugPrivilege 仅 Windows（advapi32 FFI）".into(),
    )
}

pub struct SteerOpts<'a> {
    pub preset: Option<&'a str>,
    pub class_want: Option<&'a str>,
    pub priority: Option<&'a str>,
    pub eco: Option<bool>,
    pub hard: bool,
    pub tids: &'a [u32],
}

pub fn steer(_target: &str, _opts: &SteerOpts) -> Value {
    unsupported("steer")
}
