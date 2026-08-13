#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""quality_engine.py — P2a 质量引擎多后端（抄 ruff/semgrep/gitleaks/pyright）。

每个后端：可用就用（子进程 JSON），缺失自动降级（返回 unavailable，不炸）。
后端清单：
  - ruff     Python lint（AST 规则，Rust 实现，★49k）
  - semgrep  模式即规则跨语言静态分析（★16k）
  - gitleaks 密钥泄露检测（★28.6k）
  - pyright  静态类型检查（★15.6k）

用法：
  qe = QualityEngine()
  qe.scan(path)            # 跑全部可用后端
  qe.ruff_check(path)      # 单后端
"""
import json
import os
import shutil
import subprocess


class QualityEngine:
    """质量检查多后端（自动探测可用性，缺失降级）。"""

    def __init__(self):
        self._cache: dict[str, str | None] = {}

    # ── 后端探测 ──────────────────────────────────────────
    def _find(self, name: str) -> str | None:
        """找可执行文件（缓存结果）。"""
        if name not in self._cache:
            self._cache[name] = shutil.which(name)
        return self._cache[name]

    def available(self) -> dict[str, bool]:
        return {
            "ruff": self._find("ruff") is not None,
            "semgrep": self._find("semgrep") is not None,
            "gitleaks": self._find("gitleaks") is not None,
            "pyright": self._find("pyright") is not None,
            "codeql": self._find("codeql") is not None,
            "angr": self._find("python") is not None and self._angr_importable(),
        }

    def _angr_importable(self) -> bool:
        try:
            import angr  # noqa: F401
            return True
        except ImportError:
            return False

    # ── 单后端 ────────────────────────────────────────────
    def ruff_check(self, path: str, select: str = "E,F", timeout: int = 60) -> dict:
        """ruff lint（Python AST 规则）。返回 {ok, findings, summary}。"""
        exe = self._find("ruff")
        if exe is None:
            return {"backend": "ruff", "available": False,
                    "error": "ruff 未安装（pip install ruff）"}
        try:
            r = subprocess.run(
                [exe, "check", "--select", select, "--output-format", "json", path],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"backend": "ruff", "available": True, "error": f"{type(exc).__name__}: {exc}"}
        try:
            findings = json.loads(r.stdout) if r.stdout.strip() else []
        except json.JSONDecodeError:
            findings = []
        # 按规则聚合
        by_rule: dict[str, int] = {}
        for f in findings:
            code = f.get("code", "?")
            by_rule[code] = by_rule.get(code, 0) + 1
        return {"backend": "ruff", "available": True, "ok": r.returncode == 0,
                "count": len(findings),
                "by_rule": by_rule,
                "top": [{"code": f.get("code"), "file": f.get("filename", ""),
                         "line": (f.get("location") or {}).get("row"),
                         "message": f.get("message", "")[:120]}
                        for f in findings[:20]]}

    def semgrep_check(self, path: str, timeout: int = 120) -> dict:
        """semgrep 扫描（模式规则，跨语言）。"""
        exe = self._find("semgrep")
        if exe is None:
            return {"backend": "semgrep", "available": False,
                    "error": "semgrep 未安装"}
        try:
            r = subprocess.run(
                [exe, "scan", "--json", "--quiet", path],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"backend": "semgrep", "available": True, "error": f"{type(exc).__name__}: {exc}"}
        try:
            data = json.loads(r.stdout) if r.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        results = data.get("results", [])
        by_rule: dict[str, int] = {}
        for res in results:
            rid = (res.get("check_id") or "?").split(".")[-1]
            by_rule[rid] = by_rule.get(rid, 0) + 1
        return {"backend": "semgrep", "available": True,
                "count": len(results), "by_rule": by_rule,
                "top": [{"rule": (res.get("check_id") or "?"),
                         "file": (res.get("path") or ""),
                         "line": (res.get("start") or {}).get("line"),
                         "message": (res.get("extra") or {}).get("message", "")[:120]}
                        for res in results[:20]]}

    def gitleaks_check(self, path: str, timeout: int = 120) -> dict:
        """gitleaks 密钥扫描。"""
        exe = self._find("gitleaks")
        if exe is None:
            return {"backend": "gitleaks", "available": False,
                    "error": "gitleaks 未安装"}
        try:
            r = subprocess.run(
                [exe, "detect", "--source", path, "--no-banner", "--report-format", "json",
                 "--report-path", os.devnull],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"backend": "gitleaks", "available": True, "error": f"{type(exc).__name__}: {exc}"}
        # gitleaks stdout 是报告行；stderr 有统计
        leaks = []
        for line in (r.stdout or "").splitlines():
            if not line.strip():
                continue
            try:
                leaks.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return {"backend": "gitleaks", "available": True,
                "count": len(leaks),
                "top": [{"rule": l.get("RuleID", ""), "file": l.get("File", ""),
                         "line": l.get("StartLine"), "secret": str(l.get("Secret", ""))[:8] + "..."}
                        for l in leaks[:20]]}

    def pyright_check(self, path: str, timeout: int = 180) -> dict:
        """pyright 类型检查（需 node 环境）。"""
        exe = self._find("pyright")
        if exe is None:
            return {"backend": "pyright", "available": False,
                    "error": "pyright 未安装"}
        try:
            r = subprocess.run(
                [exe, "--outputjson", path],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"backend": "pyright", "available": True, "error": f"{type(exc).__name__}: {exc}"}
        try:
            data = json.loads(r.stdout) if r.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        diags = data.get("generalDiagnostics", [])
        by_sev: dict[str, int] = {}
        for d in diags:
            sev = d.get("severity", "?")
            by_sev[sev] = by_sev.get(sev, 0) + 1
        return {"backend": "pyright", "available": True,
                "count": len(diags), "by_severity": by_sev,
                "top": [{"severity": d.get("severity"), "file": d.get("file", ""),
                         "line": d.get("range", {}).get("start", {}).get("line"),
                         "message": d.get("message", "")[:120]}
                        for d in diags[:20]]}

    # ── 顶级后端（2026-08-12：codeql/angr——探测降级，装了才跑）──
    def codeql_check(self, path: str, timeout: int = 300) -> dict:
        """CodeQL 数据流/污点分析（GitHub 级，需 codeql CLI + 数据库）。"""
        exe = self._find("codeql")
        if exe is None:
            return {"backend": "codeql", "available": False,
                    "error": "codeql 未安装（https://github.com/github/codeql）"}
        try:
            # 数据库不存在时提示建库（完整跑需要 codeql database create）
            db_path = os.path.join(path, "codeql-db")
            r = subprocess.run(
                [exe, "database", "create", db_path, "--language", "python",
                 "--source-root", path, "--overwrite"],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"backend": "codeql", "available": True,
                    "error": f"{type(exc).__name__}: {exc}"}
        return {"backend": "codeql", "available": True,
                "ok": r.returncode == 0,
                "note": "数据库已建（可跑 codeql database analyze 查数据流）"}

    def angr_check(self, path: str, timeout: int = 120) -> dict:
        """angr 符号执行（深度路径 bug：除零/越界；需 pip install angr）。"""
        if not self._angr_importable():
            return {"backend": "angr", "available": False,
                    "error": "angr 未安装（pip install angr）"}
        try:
            import angr
            proj = angr.Project(path, auto_load_libs=False)
            state = proj.factory.entry_state()
            # 简化：跑 5 秒符号执行，收集发现
            sm = proj.factory.simulation_manager(state)
            sm.explore(find=lambda s: s.history.block_count > 100,
                       num_find=3, avoid_unsat=True)
            return {"backend": "angr", "available": True,
                    "explored": len(sm.active) + len(sm.deadended),
                    "found": len(sm.found),
                    "note": "符号执行探测（路径探索；完整分析需定制约束）"}
        except Exception as exc:
            return {"backend": "angr", "available": True,
                    "error": f"angr 执行失败: {type(exc).__name__}: {exc}"}

    # ── 聚合扫描 ──────────────────────────────────────────
    def scan(self, path: str, backends: list[str] | None = None) -> dict:
        """跑全部（或指定）可用后端，聚合结果。"""
        if not os.path.exists(path):
            return {"ok": False, "error": f"路径不存在: {path}"}
        wanted = backends or ["ruff", "semgrep", "gitleaks", "pyright"]
        results = {}
        for b in wanted:
            if b == "ruff":
                results[b] = self.ruff_check(path)
            elif b == "semgrep":
                results[b] = self.semgrep_check(path)
            elif b == "gitleaks":
                results[b] = self.gitleaks_check(path)
            elif b == "pyright":
                results[b] = self.pyright_check(path)
            elif b == "codeql":
                results[b] = self.codeql_check(path)
            elif b == "angr":
                results[b] = self.angr_check(path)
        return {"ok": True, "path": path, "backends": results,
                "available": self.available()}
