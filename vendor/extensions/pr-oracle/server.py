"""MCP server exposing pr-test-oracle's STATIC test-impact analysis (no LLM).

Uses only the pure-static parts of E:\\共享\\51\\10\\pr-test-oracle:
  - pr_test_oracle.test_mapper.TestMapper  (changed-file -> candidate tests)

Tools:
  - pr_oracle_map_local(repo_path, changed_files)   analyze a local repo checkout
  - pr_oracle_discover_tests(repo_path, patterns)   list test files in a repo
  - pr_oracle_map_pr(pr_url, github_token)          analyze a public GitHub PR

Run with:  python server.py   (stdio transport)
"""

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

PR_ORACLE_SRC = r"E:\共享\51\10\pr-test-oracle\src"
sys.path.insert(0, PR_ORACLE_SRC)

from pr_test_oracle.models import TestMapping  # noqa: E402
from pr_test_oracle.test_mapper import TestMapper  # noqa: E402

_DEFAULT_PATTERNS = ["tests/**/*.py", "test_*.py", "**/*.test.js", "**/*.test.ts"]


def _mapper(repo_path: str, patterns: list[str] | None) -> TestMapper:
    return TestMapper(repo_path, patterns or _DEFAULT_PATTERNS)


def _mapping_to_dict(m: TestMapping) -> dict:
    return {
        "source_file": m.source_file,
        "candidate_tests": m.candidate_tests,
        "mapping_reason": m.mapping_reason,
    }


def map_local(repo_path: str, changed_files: list[str], test_patterns: list[str] | None = None) -> str:
    """Map changed files in a local repository to candidate test files."""
    repo = Path(repo_path)
    if not repo.is_dir():
        return f"Error: repo path does not exist: {repo_path}"
    mapper = _mapper(str(repo), test_patterns)
    tests = mapper.discover_test_files()
    mappings = mapper.map_changed_files(changed_files)
    summary = {
        "repo": str(repo),
        "test_files_discovered": len(tests),
        "mappings": [_mapping_to_dict(m) for m in mappings],
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


def discover_tests(repo_path: str, test_patterns: list[str] | None = None) -> str:
    """List test files in a local repository."""
    repo = Path(repo_path)
    if not repo.is_dir():
        return f"Error: repo path does not exist: {repo_path}"
    mapper = _mapper(str(repo), test_patterns)
    tests = mapper.discover_test_files()
    return json.dumps({"repo": str(repo), "test_files": tests}, ensure_ascii=False, indent=2)


_PR_URL_RE = re.compile(r"^https://github\.com/([\w.\-]+)/([\w.\-]+)/pull/(\d+)$")


def _is_safe_clone_url(url: str) -> bool:
    """clone_url 必须 https://github.com/ 开头（security sa_20260809_101856）。"""
    return isinstance(url, str) and url.startswith("https://github.com/")


def _validate_head_ref(ref: str) -> bool:
    """head_ref 白名单校验（security sa_20260809_101856：防 git 参数注入）。"""
    return isinstance(ref, str) and bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}", ref)
    )


def _check_repo_limits(repo_path: str, max_files: int = 50_000, max_bytes: int = 100 * 1024 * 1024) -> str | None:
    """体积/文件数上限检查（security sa_20260809_105116：防 fork 塞数 GB 的 DoS）。

    返回错误消息（超限）或 None（正常）。不跟随符号链接；.git 排除。
    """
    total_size = 0
    file_count = 0
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            file_count += 1
            if file_count > max_files:
                return f"Error: 仓库文件数超过 {max_files} 上限（防 DoS）"
            try:
                total_size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            if total_size > max_bytes:
                return f"Error: 仓库体积超过 {max_bytes // (1024 * 1024)}MB 上限（防 DoS）"
    return None


def _github_json(client: httpx.Client, url: str, token: str | None) -> dict | list:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def map_pr(pr_url: str, github_token: str | None = None, test_patterns: list[str] | None = None) -> str:
    """Fetch a GitHub PR, clone its head branch, and map changed files to tests."""
    m = _PR_URL_RE.match(pr_url)
    if not m:
        return (
            "Error: invalid PR URL. Expected https://github.com/owner/repo/pull/N "
            "(only public GitHub PRs work without a token)"
        )
    owner, repo, pr_number = m.group(1), m.group(2), int(m.group(3))

    try:
        # TLS 校验保持开启（security sa_20260809_101856：verify=False 会被 MITM 窃取 token）。
        # 优先 truststore（Windows 系统 CA 存储），缺失时回退 ssl.create_default_context()（fail-closed）。
        import ssl as _ssl

        try:
            import truststore

            _ctx = truststore.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            _ctx = _ssl.create_default_context()
        with httpx.Client(verify=_ctx) as client:
            pr = _github_json(client, f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}", github_token)
            head_ref = pr.get("head", {}).get("ref")
            if not head_ref:
                return f"Error: cannot resolve head branch for PR {pr_number}"
            # head_ref 白名单校验（security sa_20260809_101856：防 git 参数注入）
            if not _validate_head_ref(head_ref):
                return f"Error: invalid head branch name: {head_ref!r}"
            # PR head may live in a fork: clone the head repo, not the base repo
            head_repo = (pr.get("head") or {}).get("repo") or {}
            clone_url = head_repo.get("clone_url") or f"https://github.com/{owner}/{repo}.git"
            # clone_url 必须 https://github.com/（防任意 URL/git 协议注入）
            if not _is_safe_clone_url(clone_url):
                return f"Error: invalid clone URL: {clone_url!r}"
            changed = _github_json(
                client,
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100",
                github_token,
            )
            changed_files = [f["filename"] for f in changed]

        tmp = tempfile.mkdtemp(prefix="pr-oracle-")
        try:
            proc = subprocess.run(
                # --filter=blob:none + --depth 1：clone 阶段只下载 commit/tree，
                # checkout 时仍按需拉 blob——真正的磁盘/带宽防护靠下方体积上限检查。
                ["git", "clone", "--depth", "1", "--filter=blob:none", "--branch", head_ref, clone_url, tmp],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if proc.returncode != 0:
                return f"Error cloning {clone_url} (branch {head_ref}): {proc.stderr.strip()[:500]}"
            # 体积/文件数上限（security sa_20260809_105116：checkout 后分析前的硬防护，
            # 防攻击者 fork 塞数 GB/数十万文件的磁盘+CPU DoS）
            limit_err = _check_repo_limits(tmp)
            if limit_err:
                return limit_err

            mapper = _mapper(tmp, test_patterns)
            tests = mapper.discover_test_files()
            mappings = mapper.map_changed_files(changed_files)
            summary = {
                "pr": pr_url,
                "head_branch": head_ref,
                "changed_files": changed_files,
                "test_files_discovered": len(tests),
                "mappings": [_mapping_to_dict(m) for m in mappings],
            }
            return json.dumps(summary, ensure_ascii=False, indent=2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as exc:
        return f"Error analyzing PR: {type(exc).__name__}: {exc}"


def _tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name="pr_oracle_map_local",
            description=(
                "Static test-impact analysis on a LOCAL repository checkout: given changed "
                "source files, returns candidate test files per changed file (naming/directory "
                "conventions, config-file broad impact). No LLM, no network."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to local repository root"},
                    "changed_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Changed file paths relative to repo root",
                    },
                    "test_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional glob patterns for test discovery",
                    },
                },
                "required": ["repo_path", "changed_files"],
            },
        ),
        types.Tool(
            name="pr_oracle_discover_tests",
            description="List test files in a local repository (static discovery, no LLM).",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to local repository root"},
                    "test_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional glob patterns for test discovery",
                    },
                },
                "required": ["repo_path"],
            },
        ),
        types.Tool(
            name="pr_oracle_map_pr",
            description=(
                "Static test-impact analysis of a public GitHub PR: fetches changed files via "
                "the GitHub API, shallow-clones the PR head branch, and maps changed files to "
                "candidate tests. No LLM. Works without a token for public repos."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pr_url": {
                        "type": "string",
                        "description": "GitHub PR URL, e.g. https://github.com/owner/repo/pull/123",
                    },
                    "github_token": {
                        "type": "string",
                        "description": "Optional GitHub token for private repos / higher rate limits",
                    },
                    "test_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional glob patterns for test discovery",
                    },
                },
                "required": ["pr_url"],
            },
        ),
    ]


def _call(name: str, arguments: dict | None) -> str:
    arguments = arguments or {}
    # 资源上限（security sa_20260809_101856：无界数组 DoS）
    changed = arguments.get("changed_files", [])
    if isinstance(changed, list) and len(changed) > 200:
        return "Error: changed_files 超过 200 上限（防 DoS）"
    patterns = arguments.get("test_patterns")
    if isinstance(patterns, list) and len(patterns) > 20:
        return "Error: test_patterns 超过 20 上限（防 DoS）"
    # 拒绝含 .. 的 glob 模式（路径遍历）
    if isinstance(patterns, list):
        for p in patterns:
            if isinstance(p, str) and (".." in p or os.path.isabs(p) or (len(p) > 1 and p[1] == ":" and p[0] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")):
                return f"Error: test_patterns 含非法路径: {p!r}"
    if name == "pr_oracle_map_local":
        return map_local(
            str(arguments["repo_path"]),
            [str(f) for f in changed],
            patterns,
        )
    if name == "pr_oracle_discover_tests":
        return discover_tests(str(arguments["repo_path"]), patterns)
    if name == "pr_oracle_map_pr":
        return map_pr(
            str(arguments["pr_url"]),
            arguments.get("github_token"),
            arguments.get("test_patterns"),
        )
    raise ValueError(f"Unknown tool: {name}")


async def main() -> None:
    server = Server("pr-oracle-static")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return _tool_definitions()

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        try:
            text = _call(name, arguments)
        except ValueError as exc:
            return [types.TextContent(type="text", text=f"Error: {exc}")]
        return [types.TextContent(type="text", text=text)]

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="pr-oracle-static",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
