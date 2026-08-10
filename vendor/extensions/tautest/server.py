"""MCP server wrapping the tautest mutation-testing CLI (E:\\共享\\51\\10\\tautest).

Exposes the local, no-LLM parts of tautest (doctor / init / run / demo) as MCP
tools. Each tool runs the CLI inside a target repository directory.

Run with:  python server.py   (stdio transport)
"""

import asyncio
import json
import os
import subprocess

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

NODE = r"C:\Program Files\nodejs\node.exe"
TAUTEST_CLI = r"E:\共享\51\10\tautest\packages\cli\dist\index.js"


def _run_cli(repo_path: str, args: list[str], timeout: int) -> str:
    if not os.path.isdir(repo_path):
        return f"Error: repo path does not exist: {repo_path}"
    env = dict(os.environ)
    env["PATH"] = r"C:\Program Files\nodejs" + os.pathsep + env.get("PATH", "")
    try:
        # Windows 超时只 kill 直接子进程（node），node 派生的测试子进程会成孤儿；
        # 用 CREATE_NEW_PROCESS_GROUP + 超时后 taskkill /T /F 清理整个进程树。
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.run(
            [NODE, TAUTEST_CLI, *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        # 清理 node 派生的整棵进程树（security sa_20260809_102435：防僵尸测试进程）
        if os.name == "nt" and exc.pid is not None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(exc.pid), "/T", "/F"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
        return f"Error: tautest {args[0] if args else ''} timed out after {timeout}s"
    except (OSError, Exception) as exc:
        return f"Error: tautest {args[0] if args else ''} failed: {type(exc).__name__}"
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    output = output.strip()
    # 输出上限：只保留尾部 512KB（防超大测试日志耗尽内存）
    if len(output) > 512 * 1024:
        output = "[输出截断]" + output[-512 * 1024:]
    if proc.returncode != 0:
        return f"Error (exit {proc.returncode}):\n{output or '(no output)'}"
    return output or f"tautest {' '.join(args)} completed"


# extra_args 白名单（security sa_20260809_102435：--output 可任意写文件）
_ALLOWED_EXTRA = {"--all", "--coverage", "--diff"}


def _sanitize_extra_args(extra_args: list | None) -> list[str] | None:
    if extra_args is None:
        return None
    if not isinstance(extra_args, list) or len(extra_args) > 20:
        return None
    out = []
    i = 0
    while i < len(extra_args):
        a = extra_args[i]
        if not isinstance(a, str) or not a:
            return None
        if a == "--diff":
            # --diff 后必须跟一个合法 ref（禁止 --output 等）
            if i + 1 >= len(extra_args) or not isinstance(extra_args[i + 1], str):
                return None
            ref = extra_args[i + 1]
            if not ref or ref.startswith("-") or " " in ref or ";" in ref or "&" in ref or "|" in ref:
                return None
            out += ["--diff", ref]
            i += 2
            continue
        if a in _ALLOWED_EXTRA:
            out.append(a)
            i += 1
            continue
        return None  # 未知参数拒绝
    return out


def tautest_doctor(repo_path: str) -> str:
    """Check whether the repository is ready for Tautest mutation testing."""
    return _run_cli(repo_path, ["doctor"], 120)


def tautest_init(repo_path: str, force: bool = False) -> str:
    """Detect project settings and create a tautest.config.ts in the repo."""
    args = ["init", "--force"] if force else ["init"]
    return _run_cli(repo_path, args, 120)


def tautest_run(repo_path: str, extra_args: list[str] | None = None) -> str:
    """Run mutation testing on changed source lines (needs tautest.config.ts).

    extra_args may include: --all, --coverage, --diff <ref>.
    Runs the project's test suite, so allow generous time.
    """
    args = ["run", *(extra_args or [])]
    return _run_cli(repo_path, args, 1800)


def tautest_demo(repo_path: str) -> str:
    """Print the copy-paste demo (a passing test suite with a surviving mutant)."""
    return _run_cli(repo_path, ["demo"], 60)


def _tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name="tautest_doctor",
            description="Check whether a repository is ready for Tautest mutation testing (detects test runner, config, git state). No LLM.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to the repository to check"},
                },
                "required": ["repo_path"],
            },
        ),
        types.Tool(
            name="tautest_init",
            description="Detect project settings and create tautest.config.ts in the repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string"},
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite an existing tautest.config.ts",
                    },
                },
                "required": ["repo_path"],
            },
        ),
        types.Tool(
            name="tautest_run",
            description=(
                "Run mutation testing on changed source lines (requires tautest.config.ts). "
                "extra_args may include --all, --coverage, --diff <ref>. "
                "Executes the project's test suite; allow generous time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string"},
                    "extra_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra CLI arguments passed to `tautest run`",
                    },
                },
                "required": ["repo_path"],
            },
        ),
        types.Tool(
            name="tautest_demo",
            description="Print the copy-paste demo (passing test suite with a surviving mutant).",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Any repo directory (demo is printed, not written)"},
                },
                "required": ["repo_path"],
            },
        ),
    ]


def _call(name: str, arguments: dict | None) -> str:
    arguments = arguments or {}
    repo = str(arguments.get("repo_path", ""))
    if not repo:
        return "Error: missing required argument 'repo_path'"
    if name == "tautest_doctor":
        return tautest_doctor(repo)
    if name == "tautest_init":
        # force 严格 bool（防字符串 "false" 变 True）
        force = arguments.get("force", False)
        return tautest_init(repo, isinstance(force, bool) and force)
    if name == "tautest_run":
        extra = _sanitize_extra_args(arguments.get("extra_args"))
        if extra is None and "extra_args" in arguments:
            return "Error: extra_args 含非法参数（仅允许 --all/--coverage/--diff <ref>）"
        return tautest_run(repo, extra)
    if name == "tautest_demo":
        return tautest_demo(repo)
    raise ValueError(f"Unknown tool: {name}")


async def main() -> None:
    server = Server("tautest-runner")

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
                server_name="tautest-runner",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
