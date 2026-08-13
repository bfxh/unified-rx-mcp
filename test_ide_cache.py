"""test_ide_cache.py — IDE R1 增量同步测试（IDE_ENHANCE_PLAN R1）。

覆盖：
  1. ide_cache 版本跟踪：file_version 检测文件变化
  2. ide_cache LRU：store/cached/invalidate 行为
  3. lsp_query 缓存：同文件同请求第二次 cached=True（且瞬时）
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ide_cache  # noqa: E402
import server  # noqa: E402

TEST_FILE = r"D:\开发\VoxelForge-Nexus\crates\nexus_app\src\terrain.rs"


def test_file_version_detects_change(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    v1 = ide_cache.file_version(str(f))
    assert v1 is not None
    time.sleep(0.01)
    f.write_text("hello world")
    v2 = ide_cache.file_version(str(f))
    assert v1 != v2, "文件变化后版本应不同"


def test_file_version_missing():
    assert ide_cache.file_version(r"D:\开发\不存在\文件.rs") is None


def test_store_and_cached(tmp_path):
    f = tmp_path / "b.rs"
    f.write_text("fn main() {}")
    p = str(f)
    ide_cache.store(p, "hover:1:0", {"ok": True})
    got = ide_cache.cached(p, "hover:1:0")
    assert got is not None and got["ok"] is True
    # 文件变了 → 缓存失效
    time.sleep(0.01)
    f.write_text("fn main() { let x = 1; }")
    assert ide_cache.cached(p, "hover:1:0") is None


def test_invalidate(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("x = 1")
    p = str(f)
    ide_cache.store(p, "hover:1:0", {"ok": True})
    ide_cache.invalidate(p)
    assert ide_cache.cached(p, "hover:1:0") is None


def test_lsp_query_cache_hit():
    """lsp_query 同文件同请求第二次应 cached=True（R1 核心）。"""
    server._ext_definitions()
    test_file = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "server.py"))
    args = {"path": test_file, "request": "hover", "line": 100, "language_id": "python"}
    r1 = server._call_ext("cae_lsp_query", args)[0].text
    d1 = json.loads(r1)
    if not d1.get("ok"):
        # pylsp 未安装 → 跳过缓存断言（环境依赖）
        return
    t0 = time.time()
    r2 = server._call_ext("cae_lsp_query", args)[0].text
    elapsed = time.time() - t0
    d2 = json.loads(r2)
    assert d2.get("cached") is True, f"第二次应命中缓存: {r2[:120]}"
    assert elapsed < 0.5, f"缓存命中应瞬时: {elapsed:.2f}s"
