"""cargo_semver_checks（Rust API 兼容性检查薄壳）契约测试。

口径（spec 见模块 docstring）：
- 解析纯函数 _parse_semver_text：failure 行 + Summary 行抽取，防御式跳过；
- 能力探测：cargo-semver-checks 缺失如实 available=False + 安装 hint；
- baseline_rev 安全字符集校验（防注入进子进程 argv——虽然 list 形式已无 shell，
  仍拒绝怪字符，防御纵深）；
- 授权：requires_auth（执行外部子进程 + 生成 rustdoc）。
真机验证（本机 cargo-semver-checks 0.50.0）：scratch git 仓 v0.1.0 tag 后给
pub fn 加参数（破坏性）→ 逐条 failure + required_bump=major。
"""
from tools.cargosemver import _parse_semver_text

_SAMPLE = (
    "--- failure function_parameter_count_changed: pub fn parameter count changed ---"
    + chr(10) + chr(10)
    + "Description:" + chr(10)
    + "A publicly-visible function now takes a different number of parameters." + chr(10)
    + "Failed in:" + chr(10)
    + "  demo::compute now takes 2 parameters instead of 1, in lib.rs:1" + chr(10)
    + chr(10)
    + "    Summary semver requires new major version: 1 major and 0 minor checks failed"
    + chr(10)
)


def test_parse_extracts_failures():
    failures, bump, major, minor = _parse_semver_text(_SAMPLE)
    assert len(failures) == 1
    assert failures[0]["lint"] == "function_parameter_count_changed"
    assert "parameter count changed" in failures[0]["title"]
    assert bump == "major"
    assert (major, minor) == (1, 0)


def test_parse_clean_output():
    clean = ("No API breaking changes detected." + chr(10)
             + "    Finished [   3.2s] demo" + chr(10))
    failures, bump, major, minor = _parse_semver_text(clean)
    assert failures == [] and bump is None and major is None and minor is None


def test_parse_minor_bump():
    sample = ("--- failure doc_added: doc was added ---" + chr(10)
              + "    Summary semver requires new minor version: 0 major and 2 minor"
              + " checks failed" + chr(10))
    failures, bump, major, minor = _parse_semver_text(sample)
    assert len(failures) == 1 and failures[0]["lint"] == "doc_added"
    assert bump == "minor" and (major, minor) == (0, 2)


def test_parse_defensive_on_garbage():
    failures, bump, _, _ = _parse_semver_text("")
    assert failures == [] and bump is None
    failures, bump, _, _ = _parse_semver_text("完全未知的输出格式")
    assert failures == [] and bump is None


def test_baseline_rev_charset_rejected(tmp_path, monkeypatch):
    import tools.cargosemver as m
    monkeypatch.setattr(m.shutil, "which", lambda name: "cargo-semver-checks")
    out = m.cargo_semver_checks(str(tmp_path), "main; rm -rf /")
    assert "非法字符" in out["error"]


def test_unavailable_when_binary_missing(tmp_path, monkeypatch):
    import tools.cargosemver as m
    monkeypatch.setattr(m.shutil, "which", lambda name: None)
    out = m.cargo_semver_checks(str(tmp_path), "v0.1.0")
    assert out["available"] is False
    assert "cargo install cargo-semver-checks" in out["hint"]


def test_requires_auth_declared():
    from registry import _TOOLS
    assert _TOOLS["cargo_semver_checks"].get("requires_auth") is True
