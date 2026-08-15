#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""sage_scan —— 语义回归优先级（阶段3，SAGE 式）。

输入仓库 + 提交范围（默认最近 1 个提交）：
  ① git diff --name-only 提取变更文件（只读）
  ② commit 消息语义分析 → 标签（bugfix/feature/perf/ui/physics/network/
     test/security/refactor——中英文关键词表）
  ③ 测试影响映射：复用 pr_oracle 的 TestMapper（经 server._call_ext 调
     pr_oracle_map_local）→ 候选测试；扩展不可用则降级为变更文件→
     测试文件路径启发式匹配
  ④ 输出"优先测试清单"（与本次更新最相关）——海量内容锁定风险区

全部只读。安全：git 命令只读（log/diff/show），无写操作。
"""
from __future__ import annotations

import json
import os
import re
import subprocess

# 语义标签关键词表（中文+英文）
_TAG_KEYWORDS = [
    ("bugfix", ["fix", "bug", "修复", "修", "回归", "崩溃", "panic", "挂"]),
    ("feature", ["feat", "feature", "新增", "功能", "add", "支持"]),
    ("perf", ["perf", "性能", "优化", "卡顿", "慢", "提速", "fast"]),
    ("ui", ["ui", "界面", "菜单", "hud", "布局", "样式", "screen"]),
    ("physics", ["phys", "物理", "碰撞", "重力", "刚体", "collision", "gravity"]),
    ("network", ["net", "网络", "联机", "同步", "sync", "延迟", "弱网"]),
    ("test", ["test", "测试", "spec", "pytest", "覆盖率"]),
    ("security", ["sec", "security", "安全", "漏洞", "注入", "越界", "权限"]),
    ("refactor", ["refactor", "重构", "清理", "rename", "移动", "拆"]),
]


def _git(root: str, args: list[str]) -> str:
    r = subprocess.run(["git", "-C", root, *args], capture_output=True,
                       text=True, timeout=20, encoding="utf-8",
                       errors="replace")
    return r.stdout if r.returncode == 0 else ""


def _semantic_tags(messages: list[str]) -> list[dict]:
    """commit 消息 → 语义标签（含命中关键词证据）。"""
    blob = " ".join(messages).lower()
    out = []
    for tag, kws in _TAG_KEYWORDS:
        hit = [k for k in kws if k.lower() in blob]
        if hit:
            out.append({"tag": tag, "matched": hit[:4]})
    return out


def _test_heuristic(changed: list[str], repo: str) -> list[dict]:
    """降级映射：变更文件 → 同目录/同名 test 文件（关键词匹配）。"""
    out = []
    for cf in changed:
        base = os.path.basename(cf)
        stem = os.path.splitext(base)[0]
        # 同名 test_<stem>.py / <stem>_test.py / test/ 目录下同名
        cands = [
            f"test_{stem}.py", f"{stem}_test.py",
            f"test_{stem}.rs", f"{stem}_test.rs",
        ]
        for c in cands:
            # 在变更文件同目录或 test 目录查找
            d = os.path.dirname(cf)
            for sub in (d, os.path.join("tests", d), os.path.join(d, "tests")):
                p = os.path.join(repo, sub, c) if sub else os.path.join(repo, c)
                if os.path.exists(p):
                    out.append({"test": os.path.relpath(p, repo),
                                "reason": f"变更 {cf} 的对应测试"})
    # 去重
    seen = set()
    dedup = []
    for t in out:
        if t["test"] not in seen:
            seen.add(t["test"])
            dedup.append(t)
    return dedup


def sage_scan(root: str, commits: int = 1, since: str = "") -> dict:
    """语义回归优先级扫描主入口。"""
    if not os.path.isdir(root):
        return {"ok": False, "error": f"路径不存在: {root}"}
    # ① 提交与变更文件
    if since:
        log = _git(root, ["log", "--since", since,
                          "--format=%h|%s", "-50"])
        diff = _git(root, ["diff", f"$(git -C {root} log --since={since} "
                                   f"--format=%H | tail -1)..HEAD",
                           "--name-only"])
    else:
        log = _git(root, ["log", f"-{max(1, commits)}", "--format=%h|%s"])
        diff = _git(root, ["diff", f"HEAD~{max(1, commits)}..HEAD",
                           "--name-only"])
    commits_list = []
    for line in log.strip().splitlines():
        if "|" in line:
            h, s = line.split("|", 1)
            commits_list.append({"hash": h, "message": s[:100]})
    changed = [l.strip() for l in diff.strip().splitlines()
               if l.strip() and not l.startswith("diff ")]
    if not commits_list and not changed:
        return {"ok": False, "error": "无提交或变更（空仓库/无历史）"}

    # ② 语义标签
    tags = _semantic_tags([c["message"] for c in commits_list])

    # ③ 测试影响：优先 pr_oracle TestMapper，降级启发式
    tests: list[dict] = []
    mapper_used = False
    try:
        import server
        r = server._call_ext("pr_oracle_map_local",
                             {"repo_path": root, "changed_files": changed})
        text = r[0].text if r else ""
        data = json.loads(text) if text and not text.startswith("Error") else {}
        mappings = data.get("mappings") or []
        for m in mappings:
            for t in (m.get("candidate_tests") or []):
                reason = m.get("mapping_reason") or f"变更 {m.get('source_file', '')}"
                tests.append({"test": str(t),
                              "reason": str(reason)[:80]})
            if m.get("candidate_tests"):
                mapper_used = True
    except Exception:  # noqa: BLE001 —— 扩展不可用降级
        pass
    if not tests:
        tests = _test_heuristic(changed, root)

    # ④ 优先测试清单（按标签相关度加权排序）
    for t in tests:
        t["priority"] = 1
        if any(tg["tag"] in ("bugfix", "security", "perf") for tg in tags):
            t["priority"] = 0  # 高风险变更 → 最高优先
    tests.sort(key=lambda t: t["priority"])
    test_paths = [t["test"] for t in tests]

    return {
        "ok": True,
        "root": root,
        "commits": commits_list[:10],
        "changed_files": changed[:50],
        "semantic_tags": tags,
        "mapper": "pr_oracle" if mapper_used else "heuristic(降级)",
        "prioritized_tests": tests[:30],
        "test_paths": test_paths[:30],
        "hint": ("prioritized_tests 是与本次更新最相关的测试——SAGE 语义回归："
                 "先跑高风险（bugfix/security/perf）对应的测试，再跑其余"),
    }


if __name__ == "__main__":  # CLI 调试入口
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    print(json.dumps(sage_scan(root, n), ensure_ascii=False, indent=1))
