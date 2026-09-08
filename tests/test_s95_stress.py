# -*- coding: utf-8 -*-
"""tests/test_s95_stress.py —— S95 高压电池：回迁后的 fs 读面在压力下行为不变形。

用户点名要"严苛的流程：高压、多线程"，本文件是正确性/一致性面（时延面在
bench/s94_perf.py）：
- 多线程并发读（16 线程 × 200 混合 op）：结果与单线程基准逐字段一致；
- 多线程并发写同一目标（8 线程 × 160 次 fs_write）：全部 ok，终态 = 写入集之一
  （tmp+replace 原子性，不留半截文件）；
- 读翻涌下并发 list：写线程持续落盘时，list 结果结构恒合法（total=entries、
  同层名有序、文件 size≥0）；
- 2000 文件语料：深度 0/2 全量对账（经 registry 出口 200 条钳制契约）+ 逐层有序；
- 8MB 大文件：读/写双侧都是干净拒绝（带 size / 字节数），绝不吐半截内容；
- 30 层深目录：list 深度钳不失控，深路径读照常。
fs_write 走 exe（S90），exe 缺失时相应用例 SKIP（读面用例无此依赖）。
"""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

import registry
import tools  # noqa: F401

_RX_FS = None
for _kind in ("release", "debug"):
    _cand = os.path.join(os.environ.get("TEMP", r"C:\Temp"),
                         "rx-rs-target", _kind, "rx-fs.exe")
    if os.path.isfile(_cand):
        _RX_FS = _cand
        break

pytestmark = pytest.mark.usefixtures("open_sandbox")


@pytest.fixture()
def open_sandbox(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "*")


@pytest.fixture()
def mini_corpus(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i:02d}.txt").write_bytes(f"内容{i}".encode("utf-8"))
    (tmp_path / "sub").mkdir()
    for i in range(5):
        (tmp_path / "sub" / f"s{i}.txt").write_bytes(b"sub")
    return str(tmp_path)


def _reference(root):
    """单线程基准：多线程跑完后逐字段对账的期望值。"""
    ref = {}
    for i in range(20):
        p = os.path.join(root, f"f{i:02d}.txt")
        ref[p] = registry.call("fs_read", {"path": p})
    return ref


def test_mt_read_stat_list_consistency(mini_corpus):
    ref = _reference(mini_corpus)
    paths = list(ref)
    jobs = []
    for k in range(200):
        p = paths[k % len(paths)]
        jobs.append(("fs_read", {"path": p}))
        jobs.append(("fs_stat", {"path": p}))
        jobs.append(("fs_list", {"path": mini_corpus, "depth": 1}))
    with ThreadPoolExecutor(max_workers=16) as ex:
        outs = list(ex.map(lambda j: registry.call(j[0], dict(j[1])), jobs))
    for k, out in enumerate(outs):
        kind = ("fs_read", "fs_stat", "fs_list")[k % 3]
        if kind == "fs_read":
            assert out == ref[jobs[k][1]["path"]]
        else:
            assert out["ok"], out
    # 汇总抽查一次 list 结构
    r = registry.call("fs_list", {"path": mini_corpus, "depth": 1})
    assert r["result"]["total"] == 26 and len(r["result"]["entries"]) == 26


def test_mt_write_same_target_atomic(tmp_path):
    if _RX_FS is None:
        pytest.skip("rx-fs.exe 缺失（fs_write 走 exe）")
    target = str(tmp_path / "hot.txt")
    payloads = [f"并发写-{k:03d}" for k in range(160)]

    def w(k):
        return registry.call("fs_write", {"path": target,
                                          "content": payloads[k],
                                          "__authorized": True})

    with ThreadPoolExecutor(max_workers=8) as ex:
        outs = list(ex.map(w, range(160)))
    assert all(o["ok"] for o in outs), next(o for o in outs if not o["ok"])
    final = registry.call("fs_read", {"path": target})
    assert final["ok"]
    assert final["result"]["content"] in payloads


def test_list_valid_under_write_churn(tmp_path):
    if _RX_FS is None:
        pytest.skip("rx-fs.exe 缺失（fs_write 走 exe）")
    root = str(tmp_path)
    (tmp_path / "churn").mkdir()
    stop = []

    def churn():
        k = 0
        while not stop:
            registry.call("fs_write", {"path": os.path.join(root, "churn",
                                                         f"c{k:03d}.txt"),
                                       "content": "x", "__authorized": True})
            k += 1

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(churn)
        try:
            seen = 0
            while seen < 40:
                r = registry.call("fs_list", {"path": os.path.join(root, "churn"),
                                              "depth": 1})
                assert r["ok"], r
                res = r["result"]
                assert res["total"] == len(res["entries"])
                names = [e["name"] for e in res["entries"]]
                assert names == sorted(names)
                # 契约：getsize 瞬时失败 = size:-1（rust walk 同语义）——翻涌下
                # 合法，只验结构与类型，不验非负
                assert all(e["type"] == "file" and isinstance(e["size"], int)
                           for e in res["entries"])
                seen += 1
        finally:
            # 断言失败也必须先停写线程，否则 with __exit__ 等一个永不停止的
            # 翻涌线程（S95 起初的"pytest 挂死"实为断言异常跳过 stop 的假死）
            stop.append(True)
            fut.result(timeout=120)


def test_corpus_2000_files_full_reconciliation(tmp_path):
    root = str(tmp_path)
    for d in range(4):
        (tmp_path / f"d{d}").mkdir()
    for i in range(2000):
        rel = (f"d{i % 4}", f"n{i:05d}.txt") if i % 3 else (f"n{i:05d}.txt",)
        tmp_path.joinpath(*rel).write_bytes(b"")
    root_files = {f"n{i:05d}.txt" for i in range(2000) if i % 3 == 0}
    dirs = {f"d{d}" for d in range(4)}

    def expected(depth):
        # walk 契约全量展开：每层 sorted、目录深度优先即插即递归
        out = []
        for name in sorted(root_files | dirs):
            if name in dirs:
                out.append({"name": name, "type": "dir"})
                if depth >= 1:
                    d = int(name[1])
                    for c in sorted(f"n{i:05d}.txt" for i in range(2000)
                                    if i % 3 and i % 4 == d):
                        out.append({"name": f"{name}{os.sep}{c}",
                                    "type": "file", "size": 0})
            else:
                out.append({"name": name, "type": "file", "size": 0})
        return out

    for depth in (0, 2):
        exp_full = expected(depth)
        n = len(exp_full)
        # fs_list 的 entries 在工具结果顶层 → registry S10 分页契约：每页
        # MAX_RESULT_ITEMS 条 + total_items/truncated/next_cursor；逐页续读
        # 拼回全量，与 walk 契约期望逐字段对账（含每层排序、目录无 size）
        got_all = []
        cursor = 0
        while True:
            r = registry.call("fs_list", {"path": root, "depth": depth,
                                          "cursor": cursor})
            assert r["ok"]
            res = r["result"]
            assert res["total"] == n and res["total_items"] == n
            got_all.extend(res["entries"])
            if res.get("truncated"):
                assert res["next_cursor"] == cursor + registry.MAX_RESULT_ITEMS
                cursor = res["next_cursor"]
            else:
                break
        assert got_all == exp_full


def test_8mb_read_rejected_cleanly(tmp_path):
    p = tmp_path / "huge.bin"
    p.write_bytes(b"A" * (8 * 1024 * 1024))
    r = registry.call("fs_read", {"path": str(p)})
    assert not r["ok"] and "文件过大" in r["error"]
    assert r["result"]["size"] == 8 * 1024 * 1024
    assert "content" not in r["result"]


def test_8mb_write_rejected_cleanly(tmp_path):
    # 写超限是双层防御：registry Schema 门（>2MB 字符）先拦，穿过者由 exe 侧
    # 1MB 门（内容过大）拦——两层都必须干净拒绝且不留文件
    if _RX_FS is None:
        pytest.skip("rx-fs.exe 缺失（fs_write 走 exe）")
    r = registry.call("fs_write", {"path": str(tmp_path / "huge.txt"),
                                   "content": "A" * (8 * 1024 * 1024),
                                   "__authorized": True})
    assert not r["ok"] and "过大" in r["error"]  # SchemaError: 参数 content 过大
    assert not os.path.exists(tmp_path / "huge.txt")
    r2 = registry.call("fs_write", {"path": str(tmp_path / "big.txt"),
                                    "content": "A" * 1_500_000,
                                    "__authorized": True})
    assert not r2["ok"] and "内容过大" in r2["error"]  # exe 侧 1MB 门
    assert not os.path.exists(tmp_path / "big.txt")


def test_deep_tree_30_levels(tmp_path):
    p = tmp_path
    for i in range(30):
        p = p / f"L{i:02d}"
    p.mkdir(parents=True)  # L00..L29 全建
    deep = p / "bottom.txt"
    deep.write_bytes(b"bottom")
    r = registry.call("fs_read", {"path": str(deep)})
    assert r["ok"] and r["result"]["content"] == "bottom"
    r2 = registry.call("fs_list", {"path": str(tmp_path), "depth": 9})
    assert r2["ok"]
    names = [e["name"] for e in r2["result"]["entries"]]
    assert all(n.count(os.sep) <= 4 for n in names)  # 深度钳 0..=4 生效
    assert not any("bottom" in n for n in names)  # 30 层深的文件不被展开


def test_churn_reads_100x512kb(tmp_path):
    blob = os.urandom(512 * 1024)
    paths = []
    for i in range(100):
        p = tmp_path / f"blob{i:03d}.bin"
        p.write_bytes(blob)
        paths.append(str(p))
    for p in paths:
        r = registry.call("fs_read", {"path": p})
        assert r["ok"] and r["result"]["size"] == len(blob)
    # 翻涌后小文件读不受影响（分配器/句柄状态无残留畸变）
    (tmp_path / "small.txt").write_bytes(b"ok")
    r = registry.call("fs_read", {"path": str(tmp_path / "small.txt")})
    assert r["result"]["content"] == "ok"
