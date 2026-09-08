"""S95 Linux 面 smoke（WSL 面回归入口，pytest 版）。

S95 回迁后 fs_read/fs_stat/fs_list 是纯 Python（无 exe 依赖），在 Linux 上必须
与 Windows 同契约工作；fs_write 仅剩的 exe 臂缺 exe 时必须报清晰错误
（红线：exe 缺失报清晰错误、不静默降级）。

Windows 上同样可跑：额外做 fs_write 真实回环。WSL 内运行方式：
  python3 -m pytest tests/test_s95_linux_smoke.py -q
（golden 契约回放 test_s95_fs_back_contract.py 为 Windows-only；本文件是
Linux 面语义的覆盖入口，两个 docstring 的指引均指向这里。）
"""
import os

import registry
from tools import fs as fs_tools


def test_linux_face_pure_python_arm(tmp_path):
    """read/stat/list 纯 Python 臂：无 exe 环境全额工作，契约与 Windows 同。"""
    f1 = tmp_path / "a.txt"
    f1.write_text("hello 烟\nsecond line", encoding="utf-8", newline="")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"\x00\x01")

    r = registry.call("fs_stat", {"path": str(f1)})
    assert r["ok"]
    st = r["result"]
    assert st["exists"] is True and st["size"] == 21  # 6+3(烟)+1+11 字节
    assert st["is_file"] is True and st["is_dir"] is False

    r = registry.call("fs_read", {"path": str(f1)})
    assert r["ok"] and r["result"]["content"] == "hello 烟\nsecond line"

    r = registry.call("fs_list", {"path": str(tmp_path), "depth": 0})
    assert r["ok"]
    res = r["result"]
    assert res["total"] == 2
    assert [e["name"] for e in res["entries"]] == ["a.txt", "sub"]

    r = registry.call("fs_list", {"path": str(tmp_path), "depth": 1})
    assert {"name": os.path.join("sub", "b.bin"), "type": "file", "size": 2} \
        in r["result"]["entries"]

    r = registry.call("fs_stat", {"path": str(tmp_path / "ghost.txt")})
    assert r["ok"] and r["result"]["exists"] is False


def test_linux_face_write_exe_missing_clear_error(tmp_path, monkeypatch):
    """fs_write 缺 exe：报清晰错误，绝不静默降级。"""
    monkeypatch.setattr(fs_tools, "_rx_fs_exe", lambda: None)
    r = registry.call("fs_write", {"path": str(tmp_path / "w.txt"),
                                   "content": "x", "__authorized": True})
    assert r["ok"] is False
    assert "rx-fs.exe" in str(r.get("error", ""))


def test_windows_face_write_roundtrip(tmp_path):
    """exe 实际存在时（Windows 开发机）做真实写回环；Linux 上跳过。"""
    if fs_tools._rx_fs_exe() is None:
        import pytest
        pytest.skip("本机无 rx-fs.exe（Linux 面预期）")
    r = registry.call("fs_write", {"path": str(tmp_path / "w.txt"),
                                   "content": "写回环", "__authorized": True})
    assert r["ok"]
    assert (tmp_path / "w.txt").read_text(encoding="utf-8") == "写回环"
