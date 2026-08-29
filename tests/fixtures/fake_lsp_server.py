# -*- coding: utf-8 -*-
"""fake_lsp_server.py —— LSP 协议测试桩：极小 stdio JSON-RPC 语言服务器。

测试用法：UNIFIED_RX_LSP_CMD_PYTHON="<python> <本文件>" 驱动 tools/lsp.py 真客户端逻辑。
行为契约：
  initialize → capabilities
  textDocument/definition → 固定 Location
  textDocument/references → 2 Locations（含声明，看 includeDeclaration 不重要）
  textDocument/rename     → WorkspaceEdit(changes)
  textDocument/hover      → MarkupContent
  textDocument/documentSymbol → 单层符号
  其余请求 → result=None；通知全部忽略；stderr 收噪音防管道堵。
"""
import json
import sys


def read_msg():
    n = None
    head = b""
    while n is None:
        ch = sys.stdin.buffer.read(1)
        if not ch:
            return None
        head += ch
        if head.endswith(b"\r\n\r\n"):
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    n = int(line.split(b":")[1].strip())
            head = b""
    body = sys.stdin.buffer.read(n)
    return json.loads(body.decode("utf-8"))


def send(obj):
    b = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n%s" % (len(b), b))
    sys.stdout.buffer.flush()


LOC = {"uri": "file:///tmp/whatever.rs",
       "range": {"start": {"line": 7, "character": 4}, "end": {"line": 7, "character": 9}}}


def main():
    while True:
        msg = read_msg()
        if msg is None:
            break
        rid = msg.get("id")
        method = msg.get("method", "")
        if rid is None:
            continue                                  # notification：吞掉
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}}})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "textDocument/definition":
            send({"jsonrpc": "2.0", "id": rid, "result": LOC})
        elif method == "textDocument/references":
            send({"jsonrpc": "2.0", "id": rid,
                  "result": [LOC, dict(LOC,
                                       range={"start": {"line": 11, "character": 0},
                                              "end": {"line": 11, "character": 5}})]})
        elif method == "textDocument/rename":
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"changes": {LOC["uri"]: [
                      {"range": LOC["range"], "newText": "renamed_x"}]}}})
        elif method == "textDocument/hover":
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"contents": {"kind": "plaintext", "value": "fn demo(&self) -> u32"},
                             "range": LOC["range"]}})
        elif method == "textDocument/documentSymbol":
            send({"jsonrpc": "2.0", "id": rid,
                  "result": [{"name": "demo", "kind": 12,
                              "range": LOC["range"], "selectionRange": LOC["range"],
                              "children": []}]})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": None})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass                                             # 测试桩静默退出即可
