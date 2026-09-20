# -*- coding: utf-8 -*-
"""协议面门（S160）：对**真实 stdio 服务器**握手并校验对外契约。

为什么单独立门：S143 曾发生"registry 发了 annotations、协议层只转发三字段 →
注解根本没上线路"（每个单元测试都绿，因为没人测协议真路径）。本门把这一层
契约全部固化，逐条对着**真握手**验：

1. initialize：返回协议版本 ∈ 支持集、serverInfo.name/version 在册、
   capabilities.tools.listChanged == True；
2. 握手留痕（S145/S146）：UNIFIED_RX_CLIENTS_LOG 指向的文件必须多出一行，
   且含 requested/negotiated/params_keys/pid/ppid/server 字段；
3. tools/list：条数 == registry、名字唯一、每条有 name/description/inputSchema/
   annotations{title, readOnlyHint 布尔}、**顶层 title**（S146）、写类工具 schema
   含 `__authorized`（properties 且 required）；
4. tools/call：内容类工具（fs_read）回包带 `[untrusted-content` 前缀（S144）；
   普通工具（sys_topology）回包 text 可解析为 JSON；
5. 未知方法 → 错误对象（带 code），不是静默或崩。

用法：python -X utf8 scripts/mcp_surface_gate.py
退出码 0 = 全过；1 = 任一条契约违约（逐条打印）。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
W = Path(tempfile.mkdtemp(prefix="urx-surface-")).resolve()
os.environ["UNIFIED_RX_SANDBOX"] = str(W)
os.environ["UNIFIED_RX_SVC"] = "off"          # 走 spawn 路径（不依赖常驻服务开）
CLIENTS = W / "clients.jsonl"
os.environ["UNIFIED_RX_CLIENTS_LOG"] = str(CLIENTS)

import registry  # noqa: E402
import tools  # noqa: E402,F401


class Server:
    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(ROOT / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            shell=False, cwd=str(ROOT))

    def call(self, obj):
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:                                        # noqa: BLE001
            self.p.kill()


def main():
    failures = []
    def ok(cond, msg):
        if not cond:
            failures.append(msg)
        print(f"  {'OK  ' if cond else 'FAIL'} {msg}")

    fixture = W / "fixture.py"
    fixture.write_text("def f(p):\n    return open(p).read()\n", encoding="utf-8")

    s = Server()
    try:
        # 1) initialize
        r = s.call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18",
                               "clientInfo": {"name": "surface-gate", "version": "1"}}})
        res = r.get("result") or {}
        ok(res.get("protocolVersion", "").startswith("20"),
           f"initialize 返回协议版本 {res.get('protocolVersion')!r}")
        ok((res.get("serverInfo") or {}).get("name") == "unified-rx-v2",
           "serverInfo.name = unified-rx-v2")
        ok((res.get("serverInfo") or {}).get("version"),
           "serverInfo.version 存在")
        ok(((res.get("capabilities") or {}).get("tools") or {}).get("listChanged") is True,
           "capabilities.tools.listChanged == True")

        # 2) 握手留痕
        lines = []
        if CLIENTS.is_file():
            lines = [json.loads(x) for x in CLIENTS.read_text(encoding="utf-8").splitlines()]
        ok(lines and lines[0].get("requested") == "2025-06-18",
           "握手留痕含 requested=2025-06-18")
        ok(all(k in (lines[0] if lines else {}) for k in
               ("negotiated", "params_keys", "pid", "ppid", "server")),
           "握手留痕含 negotiated/params_keys/pid/ppid/server")

        # 3) tools/list 形态
        r = s.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tl = (r.get("result") or {}).get("tools") or []
        ok(len(tl) == len(registry.list_tools()),
           f"tools/list 条数 {len(tl)} == registry {len(registry.list_tools())}")
        names = [t.get("name") for t in tl]
        ok(len(set(names)) == len(names), "工具名唯一")
        ok(all(t.get("description") and isinstance(t.get("inputSchema"), dict)
               for t in tl), "每条含 description/inputSchema")
        ok(all(isinstance((t.get("annotations") or {}).get("readOnlyHint"), bool)
               and (t.get("annotations") or {}).get("title") for t in tl),
           "annotations.title/readOnlyHint 全部在（S143 契约）")
        ok(all(t.get("title") == t["annotations"]["title"] for t in tl),
           "顶层 title 与 annotations.title 同值（S146 契约）")
        auth_bad = [t["name"] for t in tl
                    if registry._TOOLS.get(t["name"], {}).get("requires_auth")
                    and ("__authorized" not in ((t.get("inputSchema") or {})
                                                .get("properties") or {})
                         or "__authorized" not in ((t.get("inputSchema") or {})
                                                   .get("required") or []))]
        ok(not auth_bad, f"写类工具 schema 含 __authorized（违约: {auth_bad[:5]}）")

        # 4) tools/call：不可信前缀 + JSON 回包
        r = s.call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "fs_read", "arguments": {"path": str(fixture)}}})
        txt = ((r.get("result") or {}).get("content") or [{}])[0].get("text", "")
        ok(txt.startswith("[untrusted-content"), "fs_read 回包带不可信前缀（S144 契约）")
        r = s.call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "sys_topology", "arguments": {}}})
        txt = ((r.get("result") or {}).get("content") or [{}])[0].get("text", "")
        try:
            parsed = json.loads(txt)
            ok(isinstance(parsed, dict) and parsed.get("vendor"),
               "sys_topology 回包为可解析 JSON（含 vendor）")
        except ValueError:
            ok(False, "sys_topology 回包非 JSON")

        # 5) 未知方法 → 错误对象
        r = s.call({"jsonrpc": "2.0", "id": 5, "method": "no/such/method"})
        ok(isinstance(r.get("error"), dict) and "code" in r["error"],
           "未知方法返回错误对象（带 code）")
    finally:
        s.close()

    if failures:
        print(f"MCP-SURFACE-GATE FAIL: {len(failures)} 条契约违约")
        sys.exit(1)
    print("MCP-SURFACE-GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
