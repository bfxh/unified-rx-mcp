"""cargo_machete（Rust 未使用依赖检测薄壳）契约测试。

口径（spec 见模块 docstring）：
- 能力探测：cargo-machete 缺失如实 available=False + 安装 hint；
- 沙盒：路径过 _fs_resolve，越界报错；目录缺 Cargo.toml 显式报错；
- 解析纯函数 _parse_machete_text：文本输出（crate -- manifest: + TAB 依赖名）逐条抽取，
  防御式跳过不认识的行；
- 授权：requires_auth（执行外部子进程）。
真机验证（本机 cargo-machete 0.9.2）：scratch 项目故意留未使用依赖 → 逐条命中；
零依赖 crate（unified-rx-mcp rust/）→ clean（exit 0）。
"""
from tools.cargomachete import _parse_machete_text

_SAMPLE = (
    "Analyzing dependencies of crates in this directory..." + chr(10)
    + "cargo-machete found the following unused dependencies in this directory:" + chr(10)
    + "demo -- D:/proj/Cargo.toml:" + chr(10)
    + chr(9) + "serde" + chr(10)
    + "sub -- D:/proj/crates/sub/Cargo.toml:" + chr(10)
    + chr(9) + "anyhow" + chr(10)
)


def test_parse_extracts_packages():
    out = _parse_machete_text(_SAMPLE)
    assert [o["package"] for o in out] == ["serde", "anyhow"]
    assert out[0]["manifest"] == "D:/proj/Cargo.toml"
    assert out[0]["workspace_dir"] == "D:/proj"
    assert out[1]["workspace_dir"] == "D:/proj/crates/sub"


def test_parse_clean_output_is_empty():
    clean = ("Analyzing dependencies of crates in this directory..." + chr(10)
             + "cargo-machete didn't find any unused dependencies in this directory. Good job!")
    assert _parse_machete_text(clean) == []


def test_parse_defensive_on_garbage():
    assert _parse_machete_text("") == []
    assert _parse_machete_text("完全未知的输出格式") == []


def test_unavailable_when_binary_missing(tmp_path, monkeypatch):
    import tools.cargomachete as m
    monkeypatch.setattr(m.shutil, "which", lambda name: None)
    out = m.cargo_machete(str(tmp_path))
    assert out["available"] is False
    assert "cargo install cargo-machete" in out["hint"]


def test_sandbox_rejects_outside(tmp_path, monkeypatch):
    import tools.cargomachete as m
    monkeypatch.setattr(m.shutil, "which", lambda name: "cargo-machete")
    out = m.cargo_machete("Z:/definitely/outside")
    assert "error" in out and "沙盒" in out["error"]


def test_missing_cargo_toml_is_explicit(tmp_path, monkeypatch):
    import tools.cargomachete as m
    monkeypatch.setattr(m.shutil, "which", lambda name: "cargo-machete")
    out = m.cargo_machete(str(tmp_path))
    assert "Cargo.toml" in out["error"]


def test_requires_auth_declared():
    from registry import _TOOLS
    assert _TOOLS["cargo_machete"].get("requires_auth") is True
