"""S144 契约：间接注入立场（EXTERNAL-ALIGNMENT B3）+ annotations 上线修复。

四锁：
1. 不可信输出声明面：非空、全部在册、代表性成员锁定（防静默缩小）；
2. 协议回包：内容类工具带 `[untrusted-content` 前缀；非声明工具不带；错误包
   永不带（前缀语义=数据边界，不是错误标识）；
3. registry.call 嵌入式结果形状零变化（前缀只在协议回包层）；
4. S143 补遗：`tools/list` 协议层必须转发 `annotations`（此前 registry 发了、
   server 只转发三字段——注解根本没上线路，实锤"改了一半"）。
"""
import registry
import server
import toolmeta


def test_untrusted_declaration_covers_content_tools():
    names = toolmeta.UNTRUSTED_OUTPUT_TOOLS
    assert names, "声明面不许为空"
    stale = names - set(registry._TOOLS)
    assert not stale, f"声明了不在册的工具: {stale}"
    for must in ("fs_read", "code_context", "ide_read_symbol", "bug_scan",
                 "secrets_hunt", "ide_diagnostics"):
        assert must in names, f"代表性内容工具 {must} 掉出声明面"


def test_protocol_reply_prefixes_untrusted_only():
    ok = {"ok": True, "result": {"x": 1}}
    assert server.tool_reply(1, "fs_read", ok)["result"]["content"][0]["text"].startswith(
        "[untrusted-content")
    assert not server.tool_reply(2, "fs_stat", ok)["result"]["content"][0]["text"].startswith(
        "[untrusted-content")
    # 错误回包永不带前缀（是错误标识的领域，不掺数据边界语义）
    err = {"ok": False, "error": "boom"}
    assert not server.tool_reply(3, "fs_read", err)["result"]["content"][0]["text"].startswith(
        "[untrusted-content")


def test_protocol_call_path_prefixes_real_result(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello\n", encoding="utf-8")
    r = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "fs_read", "arguments": {"path": str(p)}}})
    assert r["result"]["isError"] is False
    assert r["result"]["content"][0]["text"].startswith("[untrusted-content")
    r2 = server._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "fs_stat", "arguments": {"path": str(p)}}})
    assert not r2["result"]["content"][0]["text"].startswith("[untrusted-content")


def test_embedded_call_shape_unchanged(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello\n", encoding="utf-8")
    r = registry.call("fs_read", {"path": str(p)})
    assert r.get("ok") and isinstance(r["result"], dict)
    assert "untrusted" not in str(r["result"])[:80]


def test_protocol_tools_list_forwards_annotations():
    r = server._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    tools = r["result"]["tools"]
    assert len(tools) == len(registry.list_tools())
    for t in tools:
        ann = t.get("annotations")
        assert ann and ann.get("title"), f"{t['name']} 的 annotations 没上线路"
        assert isinstance(ann.get("readOnlyHint"), bool), t["name"]
