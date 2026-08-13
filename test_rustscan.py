"""rust_scan 精化回归测试：测试模块过滤 + as 危险规则。

覆盖本轮 rust_scan 的 3 项增强：
  1. #[cfg(test)] mod tests 块内 unwrap/panic 不报（生产报告精确）
  2. 顶层 #[test] fn 不报
  3. as 只报危险目标类型（u8/i8/u16/i16/u32/i32/f32/u64），跳过安全（as f64/as usize）
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


def test_as_dangerous_only():
    code = """
fn conv(x: f64, i: usize) -> (u8, f64, usize) {
    let a = i as u8;      // 危险：窄化
    let b = x as f32;     // 危险：精度截断
    let c = x as f64;     // 安全：不报
    let d = i as usize;   // 安全：不报
    (a, c, d)
}
"""
    issues = _scan(code)
    as_issues = [i for i in issues if i["rule"] == "as"]
    assert len(as_issues) == 2, f"应只报 2 个危险 as（u8/f32），实际 {len(as_issues)}"
    assert any("u8" in i["message"] for i in as_issues)
    assert any("f32" in i["message"] for i in as_issues)
