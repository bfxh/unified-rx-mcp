"""test_agents.py — 多智能体接入脚本测试（2026-08-13）。

覆盖 scripts/install_agents.py：
  1. _merge_servers：保留已有其他 MCP 服务器条目，unified-rx 覆盖更新
  2. _write_agent：写入/合并/坏 JSON 跳过不覆盖
  3. 生成的配置可直接被标准 MCP 客户端读取（结构合法）
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))

from install_agents import _AGENTS, _merge_servers, _server_entry, _write_agent  # noqa: E402


def test_agents_supported_list():
    # 至少覆盖主流智能体
    assert {"claude", "cursor", "windsurf", "trae", "aider"} <= set(_AGENTS)
    # 每个智能体都有配置文件（project/user 为 JSON mcpServers；yaml 为 mcp_servers）
    for name, (mode, rel, top) in _AGENTS.items():
        if mode == "yaml":
            assert rel.endswith(".yaml") and top == "mcp_servers", f"{name}: {rel}/{top}"
        else:
            assert rel.endswith(".json") and top == "mcpServers", f"{name}: {rel}/{top}"


def test_merge_servers_keeps_existing():
    existing = {"other-server": {"command": "node", "args": ["x.js"]}}
    merged = _merge_servers(existing, _server_entry())
    assert "other-server" in merged, "已有服务器条目必须保留"
    assert "unified-rx" in merged
    assert merged["unified-rx"]["args"][0].endswith("server.py")


def test_write_agent_creates_config():
    repo = tempfile.mkdtemp(prefix="agent_test_")
    r = _write_agent(repo, "claude", _server_entry(), dry_run=False)
    assert r["ok"] and r.get("written")
    path = os.path.join(repo, ".mcp.json")
    assert os.path.exists(path)
    data = json.load(open(path, encoding="utf-8"))
    assert "unified-rx" in data["mcpServers"]
    # 配置可被标准 MCP 客户端消费：command + args 都是字符串
    entry = data["mcpServers"]["unified-rx"]
    assert isinstance(entry["command"], str)
    assert isinstance(entry["args"], list) and all(isinstance(a, str) for a in entry["args"])


def test_write_agent_merges_existing_config():
    repo = tempfile.mkdtemp(prefix="agent_test2_")
    _, rel, _ = _AGENTS["cursor"]
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"gh": {"command": "gh-mcp"}}}, f)
    r = _write_agent(repo, "cursor", _server_entry(), dry_run=False)
    assert r["ok"]
    data = json.load(open(path, encoding="utf-8"))
    assert "gh" in data["mcpServers"], "已有 gh 服务器不能丢"
    assert "unified-rx" in data["mcpServers"], "unified-rx 应并入"


def test_write_agent_skips_bad_existing():
    repo = tempfile.mkdtemp(prefix="agent_test3_")
    _, rel, _ = _AGENTS["trae"]
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ 坏 JSON !!!")
    r = _write_agent(repo, "trae", _server_entry(), dry_run=False)
    assert not r["ok"], "坏 JSON 应跳过不覆盖"
    with open(path, encoding="utf-8") as f:
        assert f.read().startswith("{ 坏 JSON"), "坏配置不能被覆盖破坏"


def test_dry_run_no_write():
    repo = tempfile.mkdtemp(prefix="agent_test4_")
    r = _write_agent(repo, "aider", _server_entry(), dry_run=True)
    assert r["ok"] and r.get("dry_run")
    _, rel, _ = _AGENTS["aider"]
    assert not os.path.exists(os.path.join(repo, rel)), "dry-run 不应落盘"
