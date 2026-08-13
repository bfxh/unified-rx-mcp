"""rust_scan 精化回归测试：测试模块过滤 + as 三级分类。

覆盖 rust_scan 增强：
  1. #[cfg(test)] mod tests 块内 unwrap/panic 不报（生产报告精确）
  2. 顶层 #[test] fn 不报
  3. as 按目标类型三级分类（SCAN_QUALITY_ISSUES.md 问题 A）：
     窄化（u8/i8/u16/i16）warn；精度损失（f32）/可能窄化（u32/i32）info；
     加宽/同宽/浮点（f64/usize/i64/u64）跳过——体素坐标/尺寸/质量转换零噪音
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rust_scan import scan_rust_file


def _scan(code: str):
    p = os.path.join(tempfile.gettempdir(), "hermes_rustscan_probe.rs")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(code)
    try:
        issues, ln = scan_rust_file(p)
        return issues
    finally:
        os.remove(p)


def test_cfg_test_module_filtered():
    code = """
fn main() {
    let x = foo().unwrap();
}

#[cfg(test)]
mod tests {
    fn t1() {
        let y = foo().unwrap();
        panic!("in test");
    }
}
"""
    issues = _scan(code)
    unwraps = [i for i in issues if i["rule"] == "unwrap"]
    panics = [i for i in issues if i["rule"] == "panic"]
    assert len(unwraps) == 1, f"应只报生产 unwrap，实际 {len(unwraps)}"
    assert len(panics) == 0, "测试模块 panic 不应报"


def test_top_level_test_fn_filtered():
    code = """
#[test]
fn integration() {
    let x = foo().unwrap();
}

fn prod() {
    let y = foo().unwrap();
}
"""
    issues = _scan(code)
    unwraps = [i for i in issues if i["rule"] == "unwrap"]
    assert len(unwraps) == 1, f"应只报生产 fn 的 unwrap，实际 {len(unwraps)}"


def test_as_severity_classified():
    """as 三级分类（SCAN_QUALITY_ISSUES.md 问题 A 修复）：
    窄化（u8）warn；精度损失（f32）info；可能窄化（u32/i32）info；加宽/同宽（f64/usize/i64）跳过。
    """
    code = """
fn conv(x: f64, i: usize, v: i64) -> (u8, f32, u32, f64, usize, i64) {
    let a = i as u8;      // 窄化 → warn
    let b = x as f32;     // 精度损失 → info
    let c = i as u32;     // 可能窄化 → info
    let d = x as f64;     // 加宽 → 不报
    let e = i as usize;   // 同宽 → 不报
    let f = v as i64;     // 同宽 → 不报
    (a, b, c, d, e, f)
}
"""
    issues = _scan(code)
    as_issues = [i for i in issues if i["rule"] == "as"]
    # 只有 3 条：u8(warn) + f32(info) + u32(info)
    assert len(as_issues) == 3, f"应报 3 条 as（u8 warn/f32 info/u32 info），实际 {len(as_issues)}"
    u8s = [i for i in as_issues if "u8" in i["message"]]
    f32s = [i for i in as_issues if "f32" in i["message"]]
    u32s = [i for i in as_issues if "u32" in i["message"]]
    assert len(u8s) == 1 and u8s[0]["severity"] == "warn", "窄化必须 warn"
    assert len(f32s) == 1 and f32s[0]["severity"] == "info", "精度损失应 info（不再 warn）"
    assert len(u32s) == 1 and u32s[0]["severity"] == "info", "可能窄化应 info"


def test_as_f64_usize_i64_skipped():
    """加宽/同宽目标（f64/usize/i64）不报——体素坐标/尺寸/索引常规转换零噪音。"""
    code = """
fn conv(x: f32, i: i32, u: u32, n: usize) -> (f64, usize, i64, u64) {
    let a = x as f64;     // 加宽
    let b = n as usize;   // 同宽
    let c = i as i64;     // 加宽
    let d = u as u64;     // 加宽
    (a, b, c, d)
}
"""
    issues = _scan(code)
    as_issues = [i for i in issues if i["rule"] == "as"]
    assert len(as_issues) == 0, f"常规加宽/同宽转换不应报，实际 {len(as_issues)}"
