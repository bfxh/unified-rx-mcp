"""MCP server exposing pure functions from E:\\共享\\51\\10\\CI-Optimization\\src.

Dynamically discovers every top-level function in the src/*.py modules and
exposes it as an MCP tool named `ciopt_<module>_<func>`.

Run with:  python server.py   (stdio transport)

Excluded by design:
  - functions that simulate long-running jobs (multi-minute sleep loops)
  - write_file (arbitrary filesystem write side effect)
"""

import importlib.util
import os
import inspect
import sys
from datetime import date, datetime
from typing import Any, get_args, get_origin

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

_THIS = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.environ.get("CIOPT_SRC") or os.path.join(_THIS, "src") or r"E:\共享)\CI-Optimization\src"
TOOL_PREFIX = "ciopt"

# Functions that simulate minutes-long jobs would block MCP calls for minutes.
_EXCLUDED_FUNCS = frozenset(
    {
        "process_large_dataset",
        "perform_database_backup",
        "simulate_file_transfer",
        "generate_detailed_report",
        "render_high_quality_video",
        "write_file",  # arbitrary filesystem write
        "read_file",   # arbitrary filesystem read (security sa_20260809_102048)
    }
)


def _type_to_js(ann: Any) -> dict:
    if ann is inspect.Parameter.empty or ann is Any:
        return {}  # unannotated params accept any JSON value
    if ann is int:
        return {"type": "integer"}
    if ann is float:
        return {"type": "number"}
    if ann is str:
        return {"type": "string"}
    if ann is bool:
        return {"type": "boolean"}
    if ann in (list, tuple):
        return {"type": "array", "items": {}}
    if ann is dict:
        return {"type": "object"}
    if ann in (datetime, date):
        return {"type": "string", "description": "ISO-8601 string, e.g. 2024-01-01T12:00:00"}
    origin = get_origin(ann)
    if origin in (list, tuple):
        args = get_args(ann)
        return {"type": "array", "items": _type_to_js(args[0]) if args else {}}
    if origin is dict:
        return {"type": "object"}
    return {"type": "string"}


def _build_schema(fn) -> dict:
    sig = inspect.signature(fn)
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, p in sig.parameters.items():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        prop = _type_to_js(p.annotation)
        if p.default is not inspect.Parameter.empty:
            prop["default"] = p.default
        else:
            required.append(name)
        props[name] = prop
    return {"type": "object", "properties": props, "required": required}


def _coerce(name: str, ann: Any, value: Any) -> Any:
    if ann in (datetime, date) and isinstance(value, str):
        try:
            return (datetime if ann is datetime else date).fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"'{name}' must be an ISO-8601 date string, got {value!r}"
            ) from exc
    return value


def _discover():
    """Return dict: tool_name -> (module_name, func_name, fn, doc)."""
    tools: dict[str, tuple[str, str, Any, str]] = {}
    mods = sorted(p for p in __import__("glob").glob(SRC_DIR + "/*.py") if not p.endswith("__init__.py"))
    for path in mods:
        mod_name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1][:-3]
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            continue  # modules with unmet imports are skipped
        for fn_name, fn in inspect.getmembers(module, inspect.isfunction):
            if fn.__module__ != mod_name:
                continue  # imported functions are not part of this toolset
            if fn_name.startswith("_") or fn_name in _EXCLUDED_FUNCS:
                continue
            tool_name = f"{TOOL_PREFIX}_{mod_name}_{fn_name}"
            doc = inspect.getdoc(fn) or f"{mod_name}.{fn_name}"
            tools[tool_name] = (mod_name, fn_name, fn, doc)
    return tools


TOOLS = _discover()


def _call(tool_name: str, arguments: dict | None) -> str:
    entry = TOOLS.get(tool_name)
    if entry is None:
        raise ValueError(f"Unknown tool: {tool_name}")
    mod_name, fn_name, fn, _ = entry
    arguments = arguments or {}
    # 危险计算上限（security sa_20260809_102048：无界 DoS；review sa_20260809_103911 收紧复杂度不匹配项）
    _LIMITS = {
        "factorial": {"n": 100_000},
        "fibonacci": {"n": 1_000},          # 返回完整序列，O(n²) 位数，1k 即够
        "power": {"base": 10_000, "b": 10_000, "exponent": 10_000},
        "generate_primes": {"limit": 100_000},  # 逐数 O(√n) 试除，1e5 约 7e7 模运算
        "is_prime": {"n": 10_000_000},
    }
    if fn_name in _LIMITS:
        for pname, cap in _LIMITS[fn_name].items():
            if pname in arguments and isinstance(arguments[pname], (int, float)) and arguments[pname] > cap:
                raise ValueError(f"参数 {pname}={arguments[pname]} 超过上限 {cap}（防 DoS）")
    # 矩阵/数组规模上限（O(n³) 乘法 / O(n²) 排序）
    if fn_name in ("matrix_multiplication", "matrix_addition"):
        for pname in ("a", "b"):
            val = arguments.get(pname)
            if isinstance(val, list) and (len(val) > 200 or any(isinstance(r, list) and len(r) > 200 for r in val)):
                raise ValueError(f"矩阵 {pname} 超过 200×200 上限（防 DoS）")
    if fn_name in ("bubble_sort", "quick_sort") and isinstance(arguments.get("arr"), list) and len(arguments["arr"]) > 10_000:
        raise ValueError("数组超过 10k 元素上限（防 DoS）")
    sig = inspect.signature(fn)
    kwargs: dict[str, Any] = {}
    for name, p in sig.parameters.items():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if name in arguments:
            kwargs[name] = _coerce(name, p.annotation, arguments[name])
        elif p.default is not inspect.Parameter.empty:
            kwargs[name] = p.default
        else:
            raise ValueError(f"Missing required argument: '{name}'")
    try:
        result = fn(**kwargs)
    except Exception as exc:  # surface the tool's own error to the client
        return f"Error in {mod_name}.{fn_name}: {type(exc).__name__}: {exc}"
    if isinstance(result, (str, int, float, bool)) or result is None:
        return str(result)
    try:
        return __import__("json").dumps(result, ensure_ascii=False, default=str)
    except TypeError:
        return str(result)


def _tool_definitions() -> list[types.Tool]:
    tools = []
    for name, (mod_name, fn_name, fn, doc) in TOOLS.items():
        tools.append(
            types.Tool(
                name=name,
                description=f"({mod_name}.{fn_name}) {doc}",
                inputSchema=_build_schema(fn),
            )
        )
    return tools


async def main() -> None:
    server = Server("ci-optimization-tools")

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
                server_name="ci-optimization-tools",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
