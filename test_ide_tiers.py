"""test_ide_tiers.py — IDE R3 热温冷测试（IDE_ENHANCE_PLAN R3）。

覆盖：
  1. enable_persistence 建表 + 温层落盘
  2. 进程内 store → 新实例 enable_persistence → 恢复（模拟重启）
  3. 版本不匹配不恢复（文件变了 → 缓存失效）
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ide_cache  # noqa: E402


def test_persistence_roundtrip(tmp_path):
    """store → 落盘 → 清内存 → enable_persistence 恢复。"""
    db = str(tmp_path / "warm.db")
    f = tmp_path / "src.py"
    f.write_text("x = 1")
    p = str(f)

    ide_cache.enable_persistence(db)
    ide_cache.store(p, "hover:1:0", {"ok": True, "result": {"contents": "x"}})

    # 模拟重启：清内存
    ide_cache._CACHE.clear()

    # 新实例恢复
    ide_cache.enable_persistence(db)
    got = ide_cache.cached(p, "hover:1:0")
    assert got is not None, "温层应恢复缓存"
    assert got["ok"] is True


def test_persistence_version_mismatch(tmp_path):
    """文件变了 → 恢复的缓存失效。"""
    db = str(tmp_path / "warm2.db")
    f = tmp_path / "b.py"
    f.write_text("y = 1")
    p = str(f)

    ide_cache.enable_persistence(db)
    ide_cache.store(p, "hover:1:0", {"ok": True})
    time.sleep(0.01)
    f.write_text("y = 2")  # 文件变化

    ide_cache._CACHE.clear()
    ide_cache.enable_persistence(db)
    got = ide_cache.cached(p, "hover:1:0")
    assert got is None, "版本不匹配不应返回缓存"


def test_persistence_db_created(tmp_path):
    db = str(tmp_path / "warm3.db")
    ide_cache.enable_persistence(db)
    assert os.path.exists(db)
    import sqlite3
    with sqlite3.connect(db) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "ide_cache" in tables
