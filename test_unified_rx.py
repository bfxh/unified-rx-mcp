"""unified-rx 测试：注册表完整性 + 工具正确性 + 协议层 + 性能。"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 测试禁用沙盒（fs 测试用 tmp_path 在沙盒外）；生产默认沙盒=启动 cwd
os.environ["UNIFIED_RX_SANDBOX"] = ""

import server


# ── 注册表完整性 ──────────────────────────────────────────────
def test_tools_count_and_schema():
    defs = server._definitions()
    # 核心 + 可用扩展；CI 上部分扩展可能加载失败（缺失依赖），只断言核心固定
    assert len(server._TOOLS) == 44, f"核心工具数变化: {len(server._TOOLS)}"
    assert len(defs) == len(server._TOOLS) + len(server._EXT_DEFS), "定义数≠核心+扩展"
    names = [d.name for d in defs]
    assert len(names) == len(set(names)), "工具名重复"


def test_tool_card():
    """Tool 角色回喂：结构化卡片视图（Aether AiRole::Tool 启发）。"""
    # 纯函数工具 → 卡片 JSON
    r = server._call("tool_card", {"name": "math_add", "arguments": {"a": 2, "b": 3}})[0]
    assert r.role == "tool"
    d = json.loads(r.text)
    assert d["ok"] is True and "math_add" in d["summary"] and d["detail"] == 5
    # JSON 结果工具 → 透传 detail
    r2 = server._call("tool_card", {"name": "bug_locate", "arguments": {"error_text": "server.py:1"}})[0]
    d2 = json.loads(r2.text)
    assert d2["ok"] is True and isinstance(d2["detail"], dict)
    # 未知工具 → ok=False
    r3 = server._call("tool_card", {"name": "nope"})[0]
    assert json.loads(r3.text)["ok"] is False
    # 工具内部错误 → ok=False
    r4 = server._call("tool_card", {"name": "math_div", "arguments": {"a": 1, "b": 0}})[0]
    assert json.loads(r4.text)["ok"] is False
    # 缺 name → ok=False
    r5 = server._call("tool_card", {"name": ""})[0]
    assert json.loads(r5.text)["ok"] is False


def test_tool_card_truncates_detail():
    """max_detail_len 截断：大列表只留前 20 条 + truncated 标记（防撑爆上下文）。"""
    # 大列表（10000 素数）→ 截断
    r = server._call("tool_card", {"name": "prime_generate", "arguments": {"limit": 10000}, "max_detail_len": 200})[0]
    d = json.loads(r.text)
    detail = d["detail"]
    assert isinstance(detail, dict) and detail.get("truncated") is True
    assert detail.get("total") and detail.get("shown") == 20
    assert len(detail.get("items", [])) == 20
    # 小结果不受截断影响
    r2 = server._call("tool_card", {"name": "math_add", "arguments": {"a": 2, "b": 3}})[0]
    assert json.loads(r2.text)["detail"] == 5
    # 非法 max_detail_len → 报错
    r3 = server._call("tool_card", {"name": "math_add", "arguments": {"a": 1, "b": 1}, "max_detail_len": 0})[0]
    assert "Error" in r3.text or "max_detail_len" in r3.text


def test_prefix_groups():
    names = set(server._TOOLS)
    assert {"fs_read", "fs_write", "fs_stat", "fs_list"} <= names
    assert {"math_add", "math_div", "math_power", "math_sqrt"} <= names
    assert {"sort_quick", "sort_bubble"} <= names
    assert {"prime_is_prime", "prime_generate"} <= names


# ── 工具正确性 ────────────────────────────────────────────────
def test_math():
    assert server._call("math_add", {"a": 2, "b": 3})[0].text == "5"
    assert server._call("math_div", {"a": 7, "b": 2})[0].text == "3.5"
    assert "Error" in server._call("math_div", {"a": 1, "b": 0})[0].text
    assert server._call("math_power", {"base": 2, "exponent": 10})[0].text == "1024"
    assert server._call("math_sqrt", {"x": 16})[0].text == "4.0"
    assert server._call("math_factorial", {"n": 5})[0].text == "120"
    assert "Error" in server._call("math_factorial", {"n": 100000})[0].text


def test_fib():
    assert server._call("fib_fibonacci", {"n": 0})[0].text == "0"
    assert server._call("fib_fibonacci", {"n": 1})[0].text == "1"
    assert server._call("fib_fibonacci", {"n": 10})[0].text == "55"


def test_str():
    assert server._call("str_reverse", {"s": "abc"})[0].text == "cba"
    assert server._call("str_upper", {"s": "abc"})[0].text == "ABC"
    assert server._call("str_palindrome", {"s": "abba"})[0].text == "True"
    assert server._call("str_palindrome", {"s": "ab"})[0].text == "False"


def test_sort_search():
    assert json.loads(server._call("sort_quick", {"arr": [3, 1, 2]})[0].text) == [1, 2, 3]
    assert json.loads(server._call("sort_bubble", {"arr": [3, 1, 2]})[0].text) == [1, 2, 3]
    assert server._call("search_binary", {"arr": [1, 2, 3, 4], "target": 3})[0].text == "2"
    assert server._call("search_binary", {"arr": [1, 2, 3], "target": 9})[0].text == "-1"


def test_stat_geo_conv():
    assert server._call("stat_mean", {"data": [1, 2, 3]})[0].text == "2.0"
    assert server._call("stat_median", {"data": [1, 2, 3]})[0].text == "2"
    assert abs(float(server._call("geo_circle_area", {"radius": 1})[0].text) - 3.14159) < 0.001
    assert server._call("geo_rect_perimeter", {"length": 3, "width": 4})[0].text == "14"
    assert server._call("conv_c2f", {"celsius": 0})[0].text == "32.0"
    assert server._call("conv_f2c", {"fahrenheit": 32})[0].text == "0.0"


def test_json_valid_prime_list():
    assert server._call("json_valid", {"json_string": '{"a":1}'})[0].text == "true"
    assert server._call("json_valid", {"json_string": "{bad}"})[0].text == "false"
    assert server._call("json_parse", {"json_string": '{"a":1}'})[0].text == '{"a": 1}'
    assert server._call("prime_is_prime", {"n": 17})[0].text == "true"
    assert server._call("prime_is_prime", {"n": 18})[0].text == "false"
    assert server._call("valid_email", {"email": "a@b.com"})[0].text == "True"
    assert json.loads(server._call("list_unique", {"lst": [1, 1, 2]})[0].text) == [1, 2]
    assert json.loads(server._call("list_flatten", {"nested_list": [1, [2, [3]]]})[0].text) == [1, 2, 3]


# ── 文件层 ───────────────────────────────────────────────────
def test_fs_roundtrip(tmp_path):
    f = tmp_path / "t.txt"
    server._tool_fs_write({"path": str(f), "content": "hello"})
    assert server._tool_fs_read({"path": str(f)})[0].text == "hello"
    st = json.loads(server._tool_fs_stat({"path": str(f)})[0].text)
    assert st["exists"] and st["size"] == 5
    listing = json.loads(server._tool_fs_list({"path": str(tmp_path)})[0].text)
    assert any(e["name"] == "t.txt" for e in listing["entries"])


def test_fs_errors(tmp_path):
    # 网关层统一返回错误文本（不抛异常）
    out = server._call("fs_read", {"path": str(tmp_path / "nope.txt")})[0].text
    assert "Error" in out and "不存在" in out
    # NUL 拒绝（工具函数层抛 ValueError，网关转文本）
    out = server._call("fs_read", {"path": "a\x00b"})[0].text
    assert "Error" in out


# ── 性能基准 ─────────────────────────────────────────────────
def test_perf_fast_dispatch():
    start = time.perf_counter()
    for _ in range(1000):
        server._call("math_add", {"a": 1, "b": 2})
    elapsed = (time.perf_counter() - start) * 1000
    assert elapsed < 500, f"1000 次调用 {elapsed:.0f}ms 过慢"


def test_unknown_tool_error():
    assert "unknown tool" in server._call("nope", {})[0].text


# ── 扩展层（懒加载合并 pr-oracle/tautest/cae）────────────────
def test_fs_sandbox_enforced():
    """默认沙盒（锚定 cwd）拦截越界路径（security 审查修复验证）。"""
    # 模拟：重新加载带沙盒的配置（cwd 作为根）
    import importlib
    saved = os.environ.get("UNIFIED_RX_SANDBOX", "")
    try:
        os.environ["UNIFIED_RX_SANDBOX"] = os.getcwd()
        mod = importlib.reload(server)
        # 沙盒外路径（系统临时目录）应拒绝
        import tempfile
        out = mod._call("fs_read", {"path": os.path.join(tempfile.gettempdir(), "x.txt")})[0].text
        assert "Error" in out and "越界" in out, f"沙盒未拦截: {out}"
        # 沙盒内路径（cwd 下）应放行（不存在则报文件不存在而非越界）
        out2 = mod._call("fs_read", {"path": os.path.join(os.getcwd(), "no_such_file_xyz.txt")})[0].text
        assert "越界" not in out2, f"沙盒内路径被误拒: {out2}"
    finally:
        os.environ["UNIFIED_RX_SANDBOX"] = saved
        importlib.reload(server)


def test_math_power_limits():
    """math_power 指数/底数上限（HIGH-2 修复验证）。"""
    assert server._call("math_power", {"base": 2, "exponent": 10})[0].text == "1024"
    out = server._call("math_power", {"base": 2, "exponent": 100000})[0].text
    assert "Error" in out and "指数" in out, f"指数未限制: {out}"
    out2 = server._call("math_power", {"base": 2, "exponent": 2000})[0].text
    assert "Error" in out2, f"指数上限 1000 未生效: {out2}"


def test_array_limits():
    """search/stat 数组上限（security LOW 修复验证）。"""
    big = list(range(100001))
    assert "Error" in server._call("search_binary", {"arr": big, "target": 1})[0].text
    assert "Error" in server._call("stat_mean", {"data": big})[0].text
    assert "Error" in server._call("stat_median", {"data": big})[0].text
    assert server._call("stat_median", {"data": [1, 2, 3]})[0].text == "2"


def test_bigint_limits():
    """factorial/fib/bubble 上限与 Python int→str 位限对齐（review should-fix 验证）。"""
    assert server._call("math_factorial", {"n": 1000})[0].text != ""
    assert "Error" in server._call("math_factorial", {"n": 1001})[0].text
    assert server._call("fib_fibonacci", {"n": 20000})[0].text != ""
    assert "Error" in server._call("fib_fibonacci", {"n": 20001})[0].text
    assert "Error" in server._call("sort_bubble", {"arr": list(range(2001))})[0].text


# ── ui_check（Bevy UI 静态检查，程序驱动）────────────────────
def test_ui_check_detects_patterns(tmp_path):
    """ui_check 检出：无相机/全屏无 FocusPolicy/Text 无字体。"""
    f = tmp_path / "ui.rs"
    f.write_text(
        "fn spawn_ui(mut c: Commands) {\n"
        "    c.spawn(Node { position_type: PositionType::Absolute,\n"
        "        width: Val::Percent(100.0), height: Val::Percent(100.0),\n"
        "        ..default() })\n"
        "        .insert(MyPanel)\n"
        "        .with_children(|p| { p.spawn(Text::new(\"hello\")); });\n"
        "}\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("ui_check", {"path": str(f)})[0].text)
    assert out["ok"], f"ui_check 失败: {out}"
    rules = {i["rule"] for i in out["issues"]}
    assert "focus_pass" in rules, f"全屏 Node 无 FocusPolicy 未检出: {rules}"
    assert "font_missing" in rules, f"Text 无字体未检出: {rules}"


def test_ui_check_clean_file(tmp_path):
    """干净 UI 代码不误报（有 Node + FocusPolicy + 字体 + 相机）。"""
    f = tmp_path / "clean.rs"
    f.write_text(
        "fn ui(mut c: Commands) {\n"
        "    c.spawn(Node { position_type: PositionType::Absolute,\n"
        "        width: Val::Percent(100.0), ..default() })\n"
        "        .insert(FocusPolicy::Pass)\n"
        "        .with_children(|p| { p.spawn(Text::new(\"x\")).insert(font); });\n"
        "}\n"
        "fn cam(mut c: Commands) { c.spawn(Camera3d::default()); }\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("ui_check", {"path": str(f)})[0].text)
    assert out["ok"], f"ui_check 失败: {out}"
    rules = {i["rule"] for i in out["issues"]}
    assert "focus_pass" not in rules, f"误报 focus_pass: {rules}"
    assert "font_missing" not in rules, f"误报 font_missing: {rules}"


def test_ui_check_node_style_fn(tmp_path):
    """spawn(node_style_fn()) 不误报 ui_root_missing（误报修复验证）。"""
    f = tmp_path / "style.rs"
    f.write_text(
        "fn panel_node_style() -> Node { Node::default() }\n"
        "fn ui(mut c: Commands) {\n"
        "    c.spawn(panel_node_style()).insert(MyPanel).insert(FocusPolicy::Block);\n"
        "}\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("ui_check", {"path": str(f)})[0].text)
    rules = {i["rule"] for i in out["issues"]}
    assert "ui_root_missing" not in rules, f"误报 ui_root_missing: {rules}"


def test_ui_check_limits():
    """max_files 越界走 Error 契约。"""
    r = server._call("ui_check", {"path": os.path.abspath(__file__), "max_files": 9999})[0].text
    assert "Error" in r


# ── 代码库认知层（cb_index/cb_status/cb_scan）───────────────
def test_cb_index_change_aware(tmp_path):
    """cb_index 变更感知：首次索引 changed 空，改文件后 changed 含该文件（认知层验证）。"""
    src = tmp_path / "repo"
    src.mkdir()
    (src / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (src / "b.py").write_text("def bar():\n    return 2\n", encoding="utf-8")

    r1 = json.loads(server._call("cb_index", {"path": str(src)})[0].text)
    assert r1["ok"] and r1["file_count"] == 2, f"首次索引失败: {r1}"
    assert r1["is_first_index"], "首次应标记 first_index"
    assert not r1["changed"], f"首次索引不应有变更: {r1['changed']}"

    # 修改 a.py → 再次索引应感知变更
    (src / "a.py").write_text("def foo():\n    return 2\n", encoding="utf-8")
    r2 = json.loads(server._call("cb_index", {"path": str(src)})[0].text)
    assert "a.py" in r2["changed"], f"变更未感知: {r2['changed']}"
    assert not r2["is_first_index"], "第二次不应是 first_index"


def test_cb_status_summary(tmp_path):
    """cb_status 读取索引摘要（不重建）。"""
    src = tmp_path / "repo2"
    src.mkdir()
    (src / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    server._call("cb_index", {"path": str(src)})
    out = json.loads(server._call("cb_status", {"path": str(src)})[0].text)
    assert out["ok"] and out["indexed"], f"status 失败: {out}"
    assert out["file_count"] == 1, f"file_count 错误: {out}"


def test_cb_scan_change_priority(tmp_path):
    """cb_scan 全库扫描：变更优先排序（priority=changed 在前）。"""
    src = tmp_path / "repo3"
    src.mkdir()
    (src / "ui.rs").write_text(
        "fn ui(mut c: Commands) {\n"
        "    c.spawn(Node { position_type: PositionType::Absolute,\n"
        "        width: Val::Percent(100.0), ..default() });\n"
        "}\n",
        encoding="utf-8",
    )
    server._call("cb_index", {"path": str(src)})
    # 修改 ui.rs 触发变更
    (src / "ui.rs").write_text(
        "fn ui(mut c: Commands) {\n"
        "    c.spawn(Node { position_type: PositionType::Absolute,\n"
        "        width: Val::Percent(100.0), ..default() });\n"
        "    let x = 1;\n"
        "}\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("cb_scan", {"path": str(src)})[0].text)
    assert out["ok"], f"cb_scan 失败: {out}"
    assert out["changed_files"] == 1, f"变更文件数错误: {out}"
    assert out["scanned_files"] >= 1, "未扫描到文件"
    if out["issues"]:
        assert out["issues"][0]["priority"] == "changed", f"变更优先失效: {out['issues'][0]}"


def test_cb_index_corrupt_tolerant(tmp_path):
    """索引损坏容错：截断 JSON / 非 dict 结构不崩溃（review should-fix 验证）。"""
    src = tmp_path / "repo4"
    src.mkdir()
    (src / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    idx_dir = src / ".unified-rx-index"
    idx_dir.mkdir()
    # 截断 JSON
    (idx_dir / "index.json").write_text('{"root": "x", "files": ', encoding="utf-8")
    out = json.loads(server._call("cb_index", {"path": str(src)})[0].text)
    assert out["ok"] and out["file_count"] == 1, f"截断容错失败: {out}"
    assert out["is_first_index"], "截断后应视为首次索引"
    # 合法 JSON 但 files 是 list（非 dict）
    (idx_dir / "index.json").write_text('{"root": "x", "files": [1,2,3]}', encoding="utf-8")
    out2 = json.loads(server._call("cb_status", {"path": str(src)})[0].text)
    assert not out2["indexed"], f"非 dict files 未容错: {out2}"


def test_ui_check_camera_dir_aggregate(tmp_path):
    """目录级 camera 聚合：有 UI 无相机 → error；有相机 → 不报（review 验证）。"""
    src = tmp_path / "ui_src"
    src.mkdir()
    (src / "a.rs").write_text(
        "fn ui(mut c: Commands) {\n"
        "    c.spawn(Node::default()).insert(MyPanel);\n"
        "}\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("ui_check", {"path": str(src)})[0].text)
    rules = {i["rule"] for i in out["issues"]}
    assert "camera_missing" in rules, f"目录无相机未检出: {rules}"
    # 加相机后不报
    (src / "b.rs").write_text("fn cam(mut c: Commands) { c.spawn(Camera3d::default()); }\n", encoding="utf-8")
    out2 = json.loads(server._call("ui_check", {"path": str(src)})[0].text)
    rules2 = {i["rule"] for i in out2["issues"]}
    assert "camera_missing" not in rules2, f"有相机仍报: {rules2}"


def test_ui_check_mode_isolation(tmp_path):
    """mode_isolation：编辑模式标记 + UI 但无显隐 → 检出；纯逻辑文件 → 不报（review 验证）。"""
    src = tmp_path / "mi"
    src.mkdir()
    (src / "e.rs").write_text(
        "fn ui(mut c: Commands) {\n"
        "    c.spawn(Node::default()).insert(HudRoot);\n"
        "    if is_editing { }\n"
        "}\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("ui_check", {"path": str(src)})[0].text)
    rules = {i["rule"] for i in out["issues"]}
    assert "mode_isolation" in rules, f"模式隔离缺失未检出: {rules}"
    # 有 Hidden 显隐 → 不报
    (src / "ok.rs").write_text(
        "fn ui(mut c: Commands) {\n"
        "    c.spawn(Node::default()).insert(HudRoot);\n"
        "    if is_editing { c.entity(e).insert(Visibility::Hidden); }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "e.rs").unlink()
    out2 = json.loads(server._call("ui_check", {"path": str(src)})[0].text)
    rules2 = {i["rule"] for i in out2["issues"]}
    assert "mode_isolation" not in rules2, f"有显隐仍报: {rules2}"


def test_ui_check_z_ordering_dedup(tmp_path):
    """z_ordering 窗口去重：同一窗口只报一条（review 修复验证）。"""
    f = tmp_path / "z.rs"
    f.write_text(
        "fn ui(mut c: Commands) {\n"
        "    c.spawn(Node { position_type: PositionType::Absolute, ..default() });\n"
        "    c.spawn(Node { position_type: PositionType::Absolute, ..default() });\n"
        "    c.spawn(Node { position_type: PositionType::Absolute, ..default() });\n"
        "}\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("ui_check", {"path": str(f)})[0].text)
    z = [i for i in out["issues"] if i["rule"] == "z_ordering"]
    assert len(z) == 1, f"z_ordering 应恰 1 条，实际 {len(z)}"


def _ext_available() -> bool:
    """扩展是否可加载（CI 无扩展/依赖时返回 False，扩展用例跳过而非失败）。"""
    try:
        server._ext_definitions()
        return len(server._EXT_DEFS) > 0
    except Exception:
        return False


ext_available = pytest.mark.skipif(
    not _ext_available(),
    reason="扩展（cae/pr-oracle/tautest）或其依赖（mcp/httpx/pr_test_oracle）不可用",
)


@ext_available
def test_ext_tools_registered():
    # 同步入口（协议层用 _ext_definitions_async）
    server._ext_definitions()
    names = set(server._EXT_DEFS)
    assert names, "扩展注册为空"
    # 前缀存在性按实际加载结果验证（CI 可能缺 pr-oracle 依赖，前缀可能缺失）
    for prefix in ("pr_oracle_", "tautest_", "cae_"):
        assert any(n.startswith(prefix) for n in names), f"{prefix} 前缀缺失"


@ext_available
def test_ext_async_schema_production_path():
    """回归：生产路径（_ext_definitions_async，事件循环内 await cae.list_tools()）
    必须拿到真实 schema（上轮 bug：_ai.run 在循环内必炸致 schema 全空）。"""
    import asyncio
    # 清缓存强制走 async 构建（模拟真实协议层首次 list_tools）
    server._EXT_DEFS.clear()
    tools = asyncio.run(server._ext_definitions_async())
    by_name = {t.name: t for t in tools}
    assert "cae_file_dedup_state" in by_name, "async 构建缺 cae 工具"
    fd = by_name["cae_file_dedup_state"]
    props = fd.inputSchema.get("properties", {})
    assert "path" in props, f"async 路径 schema 为空（回归!）: {props}"
    ci = by_name["cae_change_impact"]
    assert "repo_path" in ci.inputSchema.get("properties", {}), "change_impact schema 缺失"


@ext_available
def test_ext_route_cae():
    # cae 是 fn 风格（_tool_* 返回 list[TextContent]）；用例内显式构建（防顺序依赖）
    server._ext_definitions()
    r = server._call("cae_file_dedup_state", {"path": os.path.abspath(__file__)})
    d = json.loads(r[0].text)
    assert "unchanged" in d, f"意外结果: {r[0].text[:100]}"
    r2 = server._call("cae_aether_lang_support", {"content": "fn main() {}"})
    assert r2[0].text, "aether_lang_support 无输出"


@ext_available
def test_ext_route_pure():
    # pure 风格（_call 返回 str）；用例内显式构建（防顺序依赖）
    server._ext_definitions()
    r = server._call("tautest_demo", {"repo_path": os.path.dirname(os.path.abspath(__file__))})
    assert isinstance(r[0].text, str) and r[0].text, "tautest_demo 无输出"


# ── bug_* 代码缺陷扫描 + 精准定位 ────────────────────────────
def test_bug_scan_detects_all_rules(tmp_path):
    """五类规则全部命中且行号精确。"""
    f = tmp_path / "bad.py"
    f.write_text(
        "def f(a):\n"
        "    x = None\n"
        "    x.name\n"
        "    r = 1 / 0\n"
        "    s = [1, 2]\n"
        "    s[5]\n"
        "    h = open('t.txt')\n"
        "    return missing_var\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("bug_scan", {"path": str(f)})[0].text)
    assert out["ok"] and out["files"] == 1
    rules = {i["rule"] for i in out["issues"]}
    assert {"divide_by_zero", "index_out_of_range", "resource_leak",
            "undefined_name", "none_deref"} <= rules, f"缺规则: {rules}"
    by_rule = {i["rule"]: i for i in out["issues"]}
    assert by_rule["divide_by_zero"]["line"] == 4
    assert by_rule["index_out_of_range"]["line"] == 6
    assert by_rule["resource_leak"]["line"] == 7
    assert by_rule["undefined_name"]["line"] == 8
    assert by_rule["none_deref"]["line"] == 3
    assert by_rule["divide_by_zero"]["snippet"].startswith("r = 1 / 0")


def test_bug_scan_clean_file(tmp_path):
    """干净文件零 issue；非 .py 文件报错走 Error 契约。"""
    f = tmp_path / "ok.py"
    f.write_text("def f(a, b):\n    return a + b\n", encoding="utf-8")
    out = json.loads(server._call("bug_scan", {"path": str(f)})[0].text)
    assert out["ok"] and out["issue_count"] == 0
    txt = tmp_path / "note.txt"
    txt.write_text("hi", encoding="utf-8")
    r = server._call("bug_scan", {"path": str(txt)})[0].text
    assert "Error" in r


def test_bug_scan_with_close_tolerated(tmp_path):
    """open() 后显式 .close() 不报 resource_leak；with 也不报。"""
    f = tmp_path / "close_ok.py"
    f.write_text(
        "h = open('a')\n"
        "h.close()\n"
        "with open('b') as g:\n"
        "    pass\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("bug_scan", {"path": str(f)})[0].text)
    assert not [i for i in out["issues"] if i["rule"] == "resource_leak"], out["issues"]


def test_bug_locate_traceback(tmp_path):
    """traceback 文本 → file:line + 上下文片段。"""
    target = tmp_path / "target.py"
    target.write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")
    tb = f'Traceback (most recent call last):\n  File "{target}", line 5, in worker\n    boom()\n'
    out = json.loads(server._call("bug_locate", {"error_text": tb})[0].text)
    assert out["ok"] and out["matched"]
    loc = out["locations"][0]
    assert loc["status"] == "ok" and loc["line"] == 5 and loc["func"] == "worker"
    assert Path(loc["file"]).resolve() == target.resolve()
    assert "5: line5" in loc["context"], f"上下文缺目标行: {loc['context']}"
    assert len(loc["context"]) <= 7  # 前后各 3 行


def test_bug_locate_simple_and_nomatch():
    """简洁 x.py:line 格式可定位；无匹配返回 matched=false（非错误）。"""
    self_path = os.path.abspath(__file__)
    out = json.loads(server._call("bug_locate", {"error_text": f"{self_path}:42"})[0].text)
    assert out["ok"] and out["matched"]
    loc = out["locations"][0]
    assert Path(loc["file"]).resolve() == Path(self_path).resolve() and loc["line"] == 42
    r = server._call("bug_locate", {"error_text": "nothing here"})[0].text
    d = json.loads(r)
    assert d["matched"] is False and d["locations"] == []


def test_bug_scan_limits():
    """max_files 越界走 Error 契约。"""
    r = server._call("bug_scan", {"path": os.path.abspath(__file__), "max_files": 9999})[0].text
    assert "Error" in r
    r2 = server._call("bug_scan", {"path": os.path.abspath(__file__), "max_files": 0})[0].text
    assert "Error" in r2


def test_bug_scan_append_mutation(tmp_path):
    """append 变异后越界不误报（review should-fix 3 验证）。"""
    f = tmp_path / "mut.py"
    f.write_text("s = [1, 2]\ns.append(3)\nx = s[2]\n", encoding="utf-8")
    out = json.loads(server._call("bug_scan", {"path": str(f)})[0].text)
    rules = {i["rule"] for i in out["issues"]}
    assert "index_out_of_range" not in rules, f"append 后误报越界: {rules}"


def test_bug_scan_os_open_excluded(tmp_path):
    """os.open 配 os.close 不报 resource_leak（review nit 4 验证）。"""
    f = tmp_path / "oso.py"
    f.write_text("import os\nfd = os.open('x', os.O_WRONLY)\nos.close(fd)\n", encoding="utf-8")
    out = json.loads(server._call("bug_scan", {"path": str(f)})[0].text)
    rules = {i["rule"] for i in out["issues"]}
    assert "resource_leak" not in rules, f"os.open 误报泄漏: {rules}"


def test_bug_locate_windows_drive():
    """盘符路径定位（review should-fix 验证）：真实文件 + 断言 status=ok。"""
    self_path = os.path.abspath(__file__)  # 例如 C:\...\test_unified_rx.py
    out = json.loads(server._call("bug_locate", {"error_text": f"{self_path}:1"})[0].text)
    assert out["ok"], f"盘符解析失败: {out}"
    loc = out["locations"][0]
    assert loc["status"] == "ok", f"期望 ok 实际 {loc['status']}: {loc}"
    assert Path(loc["file"]).resolve() == Path(self_path).resolve(), loc["file"]


def test_bug_scan_close_order(tmp_path):
    """close 在 open 赋值之前不掩盖泄漏（review should-fix 1 验证）。"""
    f = tmp_path / "ord.py"
    f.write_text("def a():\n    f = open('x', 'w')\n    return f\n\ndef b():\n    f = open('y', 'w')\n    f.close()\n",
                 encoding="utf-8")
    out = json.loads(server._call("bug_scan", {"path": str(f)})[0].text)
    leaks = [i for i in out["issues"] if i["rule"] == "resource_leak"]
    # 函数 a 的 open 无 close → 仍报泄漏；函数 b 的 close 在赋值后 → 容忍
    assert len(leaks) == 1, f"期望 1 个泄漏（a），实际 {len(leaks)}"


def test_bug_scan_try_finally_ok(tmp_path):
    """try/finally: f.close() 教科书模式不误报（review 块共享修复验证）。"""
    f = tmp_path / "tf.py"
    f.write_text("def r():\n    f = open('x', 'w')\n    try:\n        f.write('hi')\n    finally:\n        f.close()\n",
                 encoding="utf-8")
    out = json.loads(server._call("bug_scan", {"path": str(f)})[0].text)
    leaks = [i for i in out["issues"] if i["rule"] == "resource_leak"]
    assert not leaks, f"try/finally 误报泄漏: {out['issues']}"


def test_bug_scan_except_block_leak(tmp_path):
    """except 块内 open 泄漏应检出（review except 展开修复验证）。"""
    f = tmp_path / "ex.py"
    f.write_text("try:\n    pass\nexcept Exception:\n    g = open('y', 'w')\n",
                 encoding="utf-8")
    out = json.loads(server._call("bug_scan", {"path": str(f)})[0].text)
    leaks = [i for i in out["issues"] if i["rule"] == "resource_leak"]
    assert len(leaks) == 1, f"except 内泄漏未检出: {out['issues']}"


def test_bug_scan_nested_block_leak(tmp_path):
    """嵌套块（if 套 for）内 open 泄漏应检出（review 递归展开修复验证）。"""
    f = tmp_path / "nb.py"
    f.write_text("def r(items):\n    if items:\n        for i in items:\n            g = open('z', 'w')\n",
                 encoding="utf-8")
    out = json.loads(server._call("bug_scan", {"path": str(f)})[0].text)
    leaks = [i for i in out["issues"] if i["rule"] == "resource_leak"]
    assert len(leaks) == 1, f"嵌套块泄漏未检出: {out['issues']}"


def test_bug_scan_nested_close_ok(tmp_path):
    """嵌套块内 close 仍容忍（if 套 if 内 f.close() 配对块外 open）。"""
    f = tmp_path / "nc.py"
    f.write_text("def r():\n    f = open('w', 'w')\n    if True:\n        if True:\n            f.close()\n",
                 encoding="utf-8")
    out = json.loads(server._call("bug_scan", {"path": str(f)})[0].text)
    leaks = [i for i in out["issues"] if i["rule"] == "resource_leak"]
    assert not leaks, f"嵌套 close 误报: {out['issues']}"


# ── 设计系统（ds_lookup/ds_check）───────────────────────────
def test_ds_lookup_tokens():
    """ds_lookup 返回全部 tokens（AI 引用设计系统）。"""
    out = json.loads(server._call("ds_lookup", {})[0].text)
    assert out["ok"], f"ds_lookup 失败: {out}"
    assert out["token_count"] >= 20, f"token 数不足: {out['token_count']}"
    tokens = out["tokens"]
    assert "color.text.primary" in tokens, f"缺 color.text.primary: {list(tokens)[:5]}"
    assert tokens["color.text.primary"]["value"].startswith("rgba"), f"颜色值格式错: {tokens['color.text.primary']}"
    assert "typography.size.base" in tokens, "缺 typography.size.base"


def test_ds_check_hardcoded_violation(tmp_path):
    """ds_check 检出硬编码值偏离 tokens。"""
    f = tmp_path / "bad.rs"
    f.write_text(
        "fn ui(mut c: Commands) {\n"
        "    c.spawn(Node { position_type: PositionType::Absolute })\n"
        "        .with_children(|p| { p.spawn(Text::new(\"x\"))\n"
        "            .insert(font_size: 24.0); });\n"
        "}\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("ds_check", {"path": str(f)})[0].text)
    assert out["ok"], f"ds_check 失败: {out}"
    rules = {i["rule"] for i in out["issues"]}
    assert "hardcoded_value" in rules, f"硬编码未检出: {rules}"


def test_ds_check_clean(tmp_path):
    """合规代码不报 hardcoded（token 值 13/16/12 允许）。"""
    f = tmp_path / "ok.rs"
    f.write_text(
        "fn ui(mut c: Commands) {\n"
        "    c.spawn(Node { position_type: PositionType::Absolute })\n"
        "        .with_children(|p| { p.spawn(Text::new(\"x\"))\n"
        "            .insert(font_size: 13.0); });\n"
        "}\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("ds_check", {"path": str(f)})[0].text)
    rules = {i["rule"] for i in out["issues"]}
    assert "hardcoded_value" not in rules, f"误报硬编码: {rules}"


def test_ds_check_dynamic_tokens(tmp_path):
    """动态派生：新增 dimension token 后合法值不误报（review should-fix 验证）。"""
    import ds_core
    # 修改 tokens 加一个新 dimension（28px）→ 动态派生应纳入
    tokens_path = os.path.join(os.path.dirname(ds_core.__file__), "design_tokens.json")
    with open(tokens_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["spacing"]["xl"] = {"$type": "dimension", "$value": {"value": 28, "unit": "px"}}
    with open(tokens_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    try:
        allowed = ds_core._allowed_dimensions()
        assert 28.0 in allowed, f"动态派生未纳入新 token: {allowed}"
        f = tmp_path / "dyn.rs"
        f.write_text("fn ui(mut c: Commands) {\n    c.spawn(Node { left: Val::Px(28.0) });\n}\n", encoding="utf-8")
        out = json.loads(server._call("ds_check", {"path": str(f)})[0].text)
        rules = {i["rule"] for i in out["issues"]}
        assert "hardcoded_value" not in rules, f"新 token 值误报: {rules}"
    finally:
        # 恢复 tokens
        del data["spacing"]["xl"]
        with open(tokens_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)


# ── std_check 通用工程标准检查 ───────────────────────────────
def test_std_check_detects_placeholder_and_magic(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text(
        "name = 'your_name'\n"
        "size = 4096\n"
        "def dup():\n    pass\n"
        "def dup():\n    pass\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("std_check", {"path": str(f)})[0].text)
    rules = {i["rule"] for i in out["issues"]}
    assert "text_placeholder" in rules, f"占位文字未检出: {rules}"
    assert "magic_number" in rules, f"魔法数字未检出: {rules}"
    assert "name_conflict" in rules, f"重复定义未检出: {rules}"
    assert "todo_markers" in out["summary"]


def test_std_check_clean_file(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("MAX_SIZE = 4096\n\ndef process(value):\n    return value * MAX_SIZE\n", encoding="utf-8")
    out = json.loads(server._call("std_check", {"path": str(f)})[0].text)
    # MAX_SIZE 命名了魔法数字；无占位/冲突 → 仅 magic_number 规则可能命中
    rules = {i["rule"] for i in out["issues"]}
    assert "text_placeholder" not in rules
    assert "name_conflict" not in rules


def test_std_check_directory_scan(tmp_path):
    (tmp_path / "a.py").write_text("x = 'lorem ipsum'\n", encoding="utf-8")
    (tmp_path / "b.rs").write_text("fn ui() { let w = 1024; }\n", encoding="utf-8")
    out = json.loads(server._call("std_check", {"path": str(tmp_path)})[0].text)
    assert out["summary"]["files"] >= 2
    rules = {i["rule"] for i in out["issues"]}
    assert "text_placeholder" in rules
    assert "ui_hardcode" in rules or "magic_number" in rules


# ── locate_edit Qoder 式定位 ────────────────────────────────
def test_locate_edit_symbol_exact(tmp_path):
    f = tmp_path / "app.py"
    f.write_text(
        "def load_config(path):\n"
        "    return path\n"
        "\n"
        "def save_config(cfg):\n"
        "    pass\n",
        encoding="utf-8",
    )
    out = json.loads(server._call("locate_edit", {"path": str(tmp_path), "query": "load_config", "limit": 5})[0].text)
    assert out["ok"] is True
    top = out["candidates"][0]
    assert top["symbol"] == "load_config" and top["line"] == 1
    assert top["score"] == 200, f"精确符号应 200 分: {top['score']}"


def test_locate_edit_guidance_hint(tmp_path):
    (tmp_path / "mod.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    out = json.loads(server._call("locate_edit", {"path": str(tmp_path), "query": "helper"})[0].text)
    assert "AI 引导" in out["hint"], "应返回 AI 引导 hint"


def test_locate_edit_requires_query(tmp_path):
    r = server._call("locate_edit", {"path": str(tmp_path), "query": "  "})[0]
    assert "query" in r.text or "Error" in r.text, "空 query 应报错"

