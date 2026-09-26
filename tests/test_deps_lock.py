"""依赖/供应链红线（scripts/deps_lock.py）契约测试。

三条判据（spec 见脚本 docstring）：
- R1 Cargo 依赖段恒空（含 dev/build/target cfg 段）；
- R2 CI 依赖：包名在 LIBRARY-POLICY §六登记 + 默认 `==` 钉版（浮动须同行留理由）；
- R3 workflow `uses:` 钉 40 位 sha，或 zizmor 例外带非空理由。
另锁：真仓当前 `collect()` 必须为空（门不能被存量顶红而不自知），
以及三条判据各自能转红（门是真门，不是摆设）。
"""
import sys

sys.path.insert(0, "scripts")

import deps_lock as dl

_CARGO_CLEAN = '[package]\nname = "x"\n\n[dependencies]\n\n[dev-dependencies]\n\n'
_CARGO_BAD = '[package]\nname = "x"\n\n[dependencies]\nserde = "1"\n\n'


def test_r1_clean_and_bad():
    assert dl.offenders(_CARGO_CLEAN) == []
    bad = dl.offenders(_CARGO_BAD)
    assert len(bad) == 1 and "serde" in bad[0]
    assert dl.offenders("[dev-dependencies]\nfoo = {}\n") != []
    assert dl.offenders("[build-dependencies]\nbar = {}\n") != []
    assert dl.offenders("[target.'cfg(unix)'.dependencies]\nbaz = {}\n") != []


def test_r2_requires_registration():
    out = dl.req_offenders("LIBRARY-POLICY 里有 ruff", ["ruff==0.16.8"])
    assert out == []
    out = dl.req_offenders("LIBRARY-POLICY 里没有它", ["brandnew==1.0.0"])
    assert len(out) == 1 and "登记" in out[0]


def test_r2_pinned_by_default():
    out = dl.req_offenders("有 ruff", ["ruff"])
    assert any("未钉版" in x for x in out)
    out = dl.req_offenders("有 jedi", ["jedi  " + dl.FLOAT_MARK + " §六①"])
    assert out == []


def test_r3_sha_or_justified_exception():
    sha = "a" * 40
    assert dl.workflow_offenders(f"      - uses: actions/checkout@{sha} # v7") == []
    ok = ("      - uses: dtolnay/rust-toolchain@stable "
          "# zizmor: ignore[unpinned-uses] ref 即工具链名")
    assert dl.workflow_offenders(ok) == []
    assert dl.workflow_offenders("      - uses: foo/bar@v1") != []
    assert dl.workflow_offenders(
        "      - uses: foo/bar@v1 # zizmor: ignore[unpinned-uses]") != []


def test_real_repo_is_clean():
    """真仓必须 0 违规——否则是存量没补齐或门写松了。"""
    assert dl.collect() == []
