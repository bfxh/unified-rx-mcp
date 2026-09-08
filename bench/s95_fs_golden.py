# -*- coding: utf-8 -*-
"""s95_fs_golden —— S95 fs 读面回迁的 golden master 捕获（节奏第 2 步：对照实验）。

回迁前用现行 exe 薄壳（registry.call → rx-fs.exe）把场景矩阵输出捕获到
tests/fixtures/s95_fs_golden.json；tools/fs.py 回迁纯 Python 后由
tests/test_s95_fs_back_contract.py 重放同一场景矩阵、断言逐字段全等——
这就是"exe 现行为 vs 回迁实现"的受控实验，且沉淀为永久回归测试。

场景对齐 rust/src/fs.rs 语义：universal newlines（CRLF/CR/混合/无尾换行）、
utf-8/replace（二进制/截断多字节/连续字节/超长编码）、1MB 门（恰好/超 1 字节/
空文件/目录当路径）、幽灵路径、stat 幽灵/目录/根、list 深度钳（0/默认/2/4/5/
-1/9）、空目录、排序混合（数字/大写/下划线/小写/中文）、六层树、非目录、
幽灵目录、沙盒拒绝/未配置 fail-closed/多根命中/空白根。

脱敏纪律（跨进程波动项，非语义项）：语料根绝对路径 → "@T"（含 realpath 变体）；
mtime → "@int"；超 4096 字符 content → sha256 摘要（1MB 边界文件不膨胀 fixture）。

Windows-only 捕获（宿主平台）；Linux 面语义由 tests/test_s95_linux_smoke.py 单独覆盖。
沙盒穿越类不入矩阵：新旧两侧共享 tools.fs._resolve（按构造同语义），越界拒绝由
tests/test_security_fuzz.py 全权锁定。
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ["PYTHONUTF8"] = "1"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import registry  # noqa: E402
import tools  # noqa: E402,F401  注册 57 工具（回迁前 fs 薄壳仍走 exe）

FIXTURE = os.path.join(_ROOT, "tests", "fixtures", "s95_fs_golden.json")

# 语料清单：相对 corpus 根的字面量片段元组 → 字节内容（唯一事实源，测试同用）
CORPUS_DIRS = (
    ("a",), ("a", "b"), ("a", "b", "c"), ("a", "b", "c", "d"),
    ("a", "b", "c", "d", "e"), ("a", "b", "c", "d", "e", "f"),
    ("sortdir",), ("emptydir",),
)
CORPUS_FILES = (
    (("plain.txt",), b"hello"),
    (("crlf.txt",), b"a\r\nb\r\n"),
    (("cr.txt",), b"a\rb"),
    (("notail.txt",), b"no trailing newline"),
    (("mixed.txt",), b"a\r\nb\rc\nd"),
    (("uni.txt",), "你好世界".encode("utf-8")),
    (("bin.bin",), b"\xff\xfe\x00abc\x80\x80"),
    (("trunc.txt",), b"\xe4\xbd"),
    (("cont.bin",), b"\x80\x80\x81"),
    (("overlong.bin",), b"\xc0\xaf\xc1\xbf"),
    (("empty.txt",), b""),
    (("exact1mb.bin",), b"a" * 1_000_000),
    (("over1mb.bin",), b"a" * 1_000_001),
    (("带 空格.txt",), "内容".encode("utf-8")),
    (("a", "lv0.txt"), b"0"),
    (("a", "b", "lv1.txt"), b"1"),
    (("a", "b", "c", "lv2.txt"), b"2"),
    (("a", "b", "c", "d", "lv3.txt"), b"3"),
    (("a", "b", "c", "d", "e", "lv4.txt"), b"4"),
    (("a", "b", "c", "d", "e", "f", "lv5.txt"), b"5"),
    (("sortdir", "B.txt"), b"x"),
    (("sortdir", "a.txt"), b"x"),
    (("sortdir", "_x"), b"x"),
    (("sortdir", "1.log"), b"x"),
    (("sortdir", "中文.txt"), b"x"),
)


def build_corpus(root):
    """语料：内容全部字面量（CORPUS_FILES），路径全部字面量片段拼接。"""
    base = Path(root)
    for d in CORPUS_DIRS:
        base.joinpath(*d).mkdir(parents=True, exist_ok=True)
    for name, data in CORPUS_FILES:
        base.joinpath(*name).write_bytes(data)


def mask(obj, root):
    """脱敏：root（含 realpath 变体）→ @T；mtime → @int；超长 content → sha256。"""
    roots = [root, os.path.realpath(root)]
    if isinstance(obj, str):
        for r in roots:
            obj = obj.replace(r, "@T")
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "mtime" and isinstance(v, int):
                out[k] = "@int"
            elif k == "error_detail":
                out[k] = "@tb"  # S72 调试堆栈尾：实现细节非契约（两臂必然不同）
            elif k == "content" and isinstance(v, str) and len(v) > 4096:
                digest = hashlib.sha256(v.encode("utf-8", "surrogatepass")).hexdigest()
                out[k] = f"@sha256:{digest}"
            else:
                out[k] = mask(v, root)
        return out
    if isinstance(obj, list):
        return [mask(x, root) for x in obj]
    return obj


def J(root, *parts):
    return os.path.join(root, *parts)


# (label, tool, args_fn(root), sandbox) —— sandbox: str / None(删 env=fail-closed) / fn(root)
SCENARIOS = [
    ("read_plain", "fs_read", lambda R: {"path": J(R, "plain.txt")}, "*"),
    ("read_crlf", "fs_read", lambda R: {"path": J(R, "crlf.txt")}, "*"),
    ("read_cr", "fs_read", lambda R: {"path": J(R, "cr.txt")}, "*"),
    ("read_notail", "fs_read", lambda R: {"path": J(R, "notail.txt")}, "*"),
    ("read_mixed", "fs_read", lambda R: {"path": J(R, "mixed.txt")}, "*"),
    ("read_uni", "fs_read", lambda R: {"path": J(R, "uni.txt")}, "*"),
    ("read_bin", "fs_read", lambda R: {"path": J(R, "bin.bin")}, "*"),
    ("read_trunc_mb", "fs_read", lambda R: {"path": J(R, "trunc.txt")}, "*"),
    ("read_cont", "fs_read", lambda R: {"path": J(R, "cont.bin")}, "*"),
    ("read_overlong", "fs_read", lambda R: {"path": J(R, "overlong.bin")}, "*"),
    ("read_empty", "fs_read", lambda R: {"path": J(R, "empty.txt")}, "*"),
    ("read_exact1mb", "fs_read", lambda R: {"path": J(R, "exact1mb.bin")}, "*"),
    ("read_over1mb", "fs_read", lambda R: {"path": J(R, "over1mb.bin")}, "*"),
    ("read_dir_path", "fs_read", lambda R: {"path": J(R, "sortdir")}, "*"),
    ("read_ghost_deep", "fs_read", lambda R: {"path": J(R, "ghost_sub", "ghost.txt")}, "*"),
    ("read_space_uni_name", "fs_read", lambda R: {"path": J(R, "带 空格.txt")}, "*"),
    ("read_relative_missing", "fs_read", lambda R: {"path": "plain.txt"}, "*"),
    ("stat_file", "fs_stat", lambda R: {"path": J(R, "plain.txt")}, "*"),
    ("stat_dir", "fs_stat", lambda R: {"path": J(R, "sortdir")}, "*"),
    ("stat_ghost", "fs_stat", lambda R: {"path": J(R, "ghost.txt")}, "*"),
    ("stat_root", "fs_stat", lambda R: {"path": R}, "*"),
    ("list_depth0", "fs_list", lambda R: {"path": R, "depth": 0}, "*"),
    ("list_default", "fs_list", lambda R: {"path": R}, "*"),
    ("list_depth2", "fs_list", lambda R: {"path": R, "depth": 2}, "*"),
    ("list_depth4", "fs_list", lambda R: {"path": R, "depth": 4}, "*"),
    ("list_depth5_clamp", "fs_list", lambda R: {"path": R, "depth": 5}, "*"),
    ("list_neg_clamp", "fs_list", lambda R: {"path": R, "depth": -1}, "*"),
    ("list_emptydir", "fs_list", lambda R: {"path": J(R, "emptydir")}, "*"),
    ("list_sortdir", "fs_list", lambda R: {"path": J(R, "sortdir"), "depth": 1}, "*"),
    ("list_notadir", "fs_list", lambda R: {"path": J(R, "plain.txt")}, "*"),
    ("list_ghostdir", "fs_list", lambda R: {"path": J(R, "nodir")}, "*"),
    ("list_deep_tree_clamp", "fs_list", lambda R: {"path": J(R, "a"), "depth": 9}, "*"),
    ("sbx_deny_read", "fs_read", lambda R: {"path": J(R, "plain.txt")}, "Z:\\no-such-root-xyz"),
    ("sbx_deny_list", "fs_list", lambda R: {"path": R}, "Z:\\no-such-root-xyz"),
    ("sbx_unset_failclosed", "fs_stat", lambda R: {"path": J(R, "plain.txt")}, None),
    ("sbx_multiroot_hit", "fs_read", lambda R: {"path": J(R, "a", "lv0.txt")},
     lambda R: "Z:\\x;" + J(R, "a")),
    ("sbx_whitespace_roots", "fs_stat", lambda R: {"path": J(R, "plain.txt")}, "  ;  "),
    ("path_required_read", "fs_read", lambda R: {"path": ""}, "*"),
    ("path_required_list", "fs_list", lambda R: {"path": ""}, "*"),
    ("path_required_stat", "fs_stat", lambda R: {"path": ""}, "*"),
]


def capture():
    """跑一遍场景矩阵，返回脱敏后的 rows（供捕获脚本与测试共用）。"""
    root = tempfile.mkdtemp(prefix="s95_golden_")
    build_corpus(root)
    rows = []
    for label, toolname, args_fn, sbx in SCENARIOS:
        s = sbx(root) if callable(sbx) else sbx
        if s is None:
            os.environ.pop("UNIFIED_RX_SANDBOX", None)
        else:
            os.environ["UNIFIED_RX_SANDBOX"] = s
        out = registry.call(toolname, args_fn(root))
        rows.append({"label": label, "out": mask(out, root)})
    os.environ.pop("UNIFIED_RX_SANDBOX", None)
    return rows


def main():
    os.chdir(_ROOT)  # read_relative_missing 以仓库根为 cwd，保证相对路径场景确定
    rows = capture()
    fx = Path(FIXTURE)
    fx.parent.mkdir(parents=True, exist_ok=True)
    fx.write_text(json.dumps(rows, ensure_ascii=False, indent=1, sort_keys=True),
                  encoding="utf-8")
    print(f"GOLDEN written: {len(rows)} scenarios -> {os.path.relpath(FIXTURE, _ROOT)}")


if __name__ == "__main__":
    main()
