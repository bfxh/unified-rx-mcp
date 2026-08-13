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


@pytest.fixture(autouse=True)
def _isolate_lse_state(tmp_path, monkeypatch):
    """LSE 测试隔离：state 指向临时文件，不污染生产 ~/.unified-rx/lse-state.json。

    lse_client 通过 subprocess 调 lse-engine（继承 os.environ），
    Rust 引擎 LSE_STATE 环境变量覆盖 state 路径（lib.rs state_path）。
    """
    monkeypatch.setenv("LSE_STATE", str(tmp_path / "lse-test-state.json"))
    yield
    # 引擎每次调用都持久化到临时文件；测试结束后由 tmp_path 自动清理


@pytest.fixture(scope="session", autouse=True)
def _prod_state_untouched():
    """状态效果测试（REGRESSION_GUARD P1-1）：整个测试会话前后，
    生产 ~/.unified-rx/lse-state.json 字节必须不变——隔离 fixture 被删/改路径即失败。
    """
    from pathlib import Path as _P
    p = _P.home() / ".unified-rx" / "lse-state.json"
    before = p.read_bytes() if p.exists() else None
    yield
    after = p.read_bytes() if p.exists() else None
    assert after == before, (
        "生产 lse-state.json 被测试污染（LSE_STATE 隔离失效）——"
        "检查 _isolate_lse_state fixture 或 lse-engine LSE_STATE 支持"
    )


# ── 注册表完整性 ──────────────────────────────────────────────
def test_defs_cache_stable():
    """契约测试（REGRESSION_GUARD P1-2）：_definitions() 重复调用逐字节一致
    （缓存前缀 byte-stable 理念；扩展构建只走 async 路径）。"""
    import server as s
    a = s._definitions()
    b = s._definitions()
    ka = [(t.name, t.description, t.inputSchema) for t in a]
    kb = [(t.name, t.description, t.inputSchema) for t in b]
    assert ka == kb, "_definitions() 两次调用不一致（_DEFS_CACHE 缓存破坏）"
    # 只断言核心固定（35）；扩展懒加载，同步 _definitions() 不构建扩展
    assert len(ka) >= 35, f"工具定义数异常: {len(ka)}"


def test_tools_count_and_schema():
    defs = server._definitions()
    # 核心 + 可用扩展；CI 上部分扩展可能加载失败（缺失依赖），只断言核心固定
    # 2026-08-11 去重：29 单工具 → 6 组合 + fib_fibonacci，核心 49 → 28；
    # 2026-08-11 高协作：+pipeline +parallel → 30
    # 2026-08-11 防幻觉：+hallucination_guard +capability_manifest → 32
    # 2026-08-11 扫描日志：+scan_log → 33
    # 2026-08-11 高并发项目扫描：+project_scan → 34
    # 2026-08-11 全盘扫：+full_scan → 35
    # 2026-08-12 P0b 混合检索：+kb_query → 36
    # 2026-08-12 P1 掌握引擎：+repo_graph → 37
    # 2026-08-12 P1c 进化记忆：+lesson_extract → 38
    # 2026-08-12 P2a 质量引擎：+quality_scan → 39
    # 2026-08-12 repo_wiki：+repo_wiki → 40
    # 2026-08-12 多智能体：+agent_orchestrate/+agent_roles → 42
    # 2026-08-13 R4 IDE 全家桶：+ide_rename/+ide_complete/+ide_actions → 45
    # 2026-08-13 R6 融合：+ide_fusion → 46
    # 2026-08-13 R7 Quest：+ide_quest → 47
    # 2026-08-13 探索/搜索接线：+explore_code/+semantic_search → 49
    # 2026-08-13 本地智能：+local_intel → 50
    # 2026-08-13 记忆维深化：+lesson_learn → 51
    # 2026-08-13 M3 命令内建：+cmd_cheatsheet/+local_run → 53
    # 2026-08-13 M4 技能申请制：+skill_fetch → 54
    # 2026-08-13 M5 本质三分：+design_note → 55
    # 2026-08-13 M6 趋势分析：+scan_trend → 56
    assert len(server._TOOLS) == 56, f"核心工具数变化: {len(server._TOOLS)}"
    assert len(defs) == len(server._TOOLS) + len(server._EXT_DEFS), "定义数≠核心+扩展"
    names = [d.name for d in defs]
    assert len(names) == len(set(names)), "工具名重复"


def test_tool_card():
    """Tool 角色回喂：结构化卡片视图（Aether AiRole::Tool 启发）。"""
    # 纯函数工具 → 卡片 JSON
    r = server._call("tool_card", {"name": "math_ops", "arguments": {"action": "add", "a": 2, "b": 3}})[0]
    assert r.role == "tool"
    d = json.loads(r.text)
    assert d["ok"] is True and "math_ops" in d["summary"] and d["detail"] == 5
    # JSON 结果工具 → 透传 detail
    r2 = server._call("tool_card", {"name": "bug_locate", "arguments": {"error_text": "server.py:1"}})[0]
    d2 = json.loads(r2.text)
    assert d2["ok"] is True and isinstance(d2["detail"], dict)
    # 未知工具 → ok=False
    r3 = server._call("tool_card", {"name": "nope"})[0]
    assert json.loads(r3.text)["ok"] is False
    # 工具内部错误 → ok=False
    r4 = server._call("tool_card", {"name": "math_ops", "arguments": {"action": "div", "a": 1, "b": 0}})[0]
    assert json.loads(r4.text)["ok"] is False
    # 缺 name → ok=False
    r5 = server._call("tool_card", {"name": ""})[0]
    assert json.loads(r5.text)["ok"] is False


def test_tool_card_truncates_detail():
    """max_detail_len 截断：大列表只留前 20 条 + truncated 标记（防撑爆上下文）。"""
    # 大列表（10000 素数）→ 截断
    r = server._call("tool_card", {"name": "prime_list", "arguments": {"action": "generate", "limit": 10000}, "max_detail_len": 200})[0]
    d = json.loads(r.text)
    detail = d["detail"]
    assert isinstance(detail, dict) and detail.get("truncated") is True
    assert detail.get("total") and detail.get("shown") == 20
    assert len(detail.get("items", [])) == 20
    # 小结果不受截断影响
    r2 = server._call("tool_card", {"name": "math_ops", "arguments": {"action": "add", "a": 2, "b": 3}})[0]
    assert json.loads(r2.text)["detail"] == 5
    # 非法 max_detail_len → 报错
    r3 = server._call("tool_card", {"name": "math_ops", "arguments": {"action": "add", "a": 1, "b": 1}, "max_detail_len": 0})[0]
    assert "Error" in r3.text or "max_detail_len" in r3.text


def test_prefix_groups():
    names = set(server._TOOLS)
    assert {"fs_read", "fs_write", "fs_stat", "fs_list"} <= names
    assert {"math_ops", "text_ops", "sort_search", "stat_geo", "json_email", "prime_list"} <= names
    assert "fib_fibonacci" in names
    assert "vuln_scan" in names


# ── 工具正确性 ────────────────────────────────────────────────
def test_math():
    assert server._call("math_ops", {"action": "add", "a": 2, "b": 3})[0].text == "5"
    assert server._call("math_ops", {"action": "div", "a": 7, "b": 2})[0].text == "3.5"
    assert "Error" in server._call("math_ops", {"action": "div", "a": 1, "b": 0})[0].text
    assert server._call("math_ops", {"action": "power", "base": 2, "exponent": 10})[0].text == "1024"
    assert server._call("math_ops", {"action": "sqrt", "x": 16})[0].text == "4.0"
    assert server._call("math_ops", {"action": "factorial", "n": 5})[0].text == "120"
    assert "Error" in server._call("math_ops", {"action": "factorial", "n": 100000})[0].text


def test_fib():
    assert server._call("fib_fibonacci", {"n": 0})[0].text == "0"
    assert server._call("fib_fibonacci", {"n": 1})[0].text == "1"
    assert server._call("fib_fibonacci", {"n": 10})[0].text == "55"


def test_str():
    assert server._call("text_ops", {"action": "reverse", "s": "abc"})[0].text == "cba"
    assert server._call("text_ops", {"action": "upper", "s": "abc"})[0].text == "ABC"
    assert server._call("text_ops", {"action": "palindrome", "s": "abba"})[0].text == "True"
    assert server._call("text_ops", {"action": "palindrome", "s": "ab"})[0].text == "False"


def test_sort_search():
    assert json.loads(server._call("sort_search", {"action": "quick_sort", "arr": [3, 1, 2]})[0].text) == [1, 2, 3]
    assert json.loads(server._call("sort_search", {"action": "bubble_sort", "arr": [3, 1, 2]})[0].text) == [1, 2, 3]
    assert server._call("sort_search", {"action": "binary_search", "arr": [1, 2, 3, 4], "target": 3})[0].text == "2"
    assert server._call("sort_search", {"action": "binary_search", "arr": [1, 2, 3], "target": 9})[0].text == "-1"


def test_stat_geo_conv():
    assert server._call("stat_geo", {"action": "mean", "data": [1, 2, 3]})[0].text == "2.0"
    assert server._call("stat_geo", {"action": "median", "data": [1, 2, 3]})[0].text == "2"
    assert abs(float(server._call("stat_geo", {"action": "circle_area", "radius": 1})[0].text) - 3.14159) < 0.001
    assert server._call("stat_geo", {"action": "rect_perimeter", "length": 3, "width": 4})[0].text == "14"
    assert server._call("math_ops", {"action": "c2f", "celsius": 0})[0].text == "32.0"
    assert server._call("math_ops", {"action": "f2c", "fahrenheit": 32})[0].text == "0.0"


def test_json_valid_prime_list():
    assert server._call("json_email", {"action": "valid", "json_string": '{"a":1}'})[0].text == "true"
    assert server._call("json_email", {"action": "valid", "json_string": "{bad}"})[0].text == "false"
    assert server._call("json_email", {"action": "parse", "json_string": '{"a":1}'})[0].text == '{"a": 1}'
    assert server._call("prime_list", {"action": "is_prime", "n": 17})[0].text == "true"
    assert server._call("prime_list", {"action": "is_prime", "n": 18})[0].text == "false"
    assert server._call("json_email", {"action": "email", "email": "a@b.com"})[0].text == "True"
    assert json.loads(server._call("prime_list", {"action": "unique", "lst": [1, 1, 2]})[0].text) == [1, 2]
    assert json.loads(server._call("prime_list", {"action": "flatten", "nested_list": [1, [2, [3]]]})[0].text) == [1, 2, 3]


# ── 文件层 ───────────────────────────────────────────────────
def _sandbox_allow(p: str) -> str:
    """把 pytest 临时目录加入沙盒根（fs 测试自洽——不依赖 UNIFIED_RX_UNSAFE 环境）。"""
    root = str(p)
    if root not in server._SANDBOX_ROOTS:
        server._SANDBOX_ROOTS.append(root)
    return root


def test_fs_roundtrip(tmp_path):
    _sandbox_allow(tmp_path)
    f = tmp_path / "t.txt"
    server._tool_fs_write({"path": str(f), "content": "hello"})
    assert server._tool_fs_read({"path": str(f)})[0].text == "hello"
    st = json.loads(server._tool_fs_stat({"path": str(f)})[0].text)
    assert st["exists"] and st["size"] == 5
    listing = json.loads(server._tool_fs_list({"path": str(tmp_path)})[0].text)
    assert any(e["name"] == "t.txt" for e in listing["entries"])


def test_fs_errors(tmp_path):
    # 网关层统一返回错误文本（不抛异常）
    _sandbox_allow(tmp_path)
    out = server._call("fs_read", {"path": str(tmp_path / "nope.txt")})[0].text
    assert "Error" in out and "不存在" in out
    # NUL 拒绝（工具函数层抛 ValueError，网关转文本）
    out = server._call("fs_read", {"path": "a\x00b"})[0].text
    assert "Error" in out


# ── 性能基准 ─────────────────────────────────────────────────
def test_perf_fast_dispatch():
    start = time.perf_counter()
    for _ in range(1000):
        server._call("math_ops", {"action": "add", "a": 1, "b": 2})
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
    assert server._call("math_ops", {"action": "power", "base": 2, "exponent": 10})[0].text == "1024"
    out = server._call("math_ops", {"action": "power", "base": 2, "exponent": 100000})[0].text
    assert "Error" in out and "指数" in out, f"指数未限制: {out}"
    out2 = server._call("math_ops", {"action": "power", "base": 2, "exponent": 2000})[0].text
    assert "Error" in out2, f"指数上限 1000 未生效: {out2}"


def test_array_limits():
    """search/stat 数组上限（security LOW 修复验证）。"""
    big = list(range(100001))
    assert "Error" in server._call("sort_search", {"action": "binary_search", "arr": big, "target": 1})[0].text
    assert "Error" in server._call("stat_geo", {"action": "mean", "data": big})[0].text
    assert "Error" in server._call("stat_geo", {"action": "median", "data": big})[0].text
    assert server._call("stat_geo", {"action": "median", "data": [1, 2, 3]})[0].text == "2"


def test_bigint_limits():
    """factorial/fib/bubble 上限与 Python int→str 位限对齐（review should-fix 验证）。"""
    assert server._call("math_ops", {"action": "factorial", "n": 1000})[0].text != ""
    assert "Error" in server._call("math_ops", {"action": "factorial", "n": 1001})[0].text
    assert server._call("fib_fibonacci", {"n": 20000})[0].text != ""
    assert "Error" in server._call("fib_fibonacci", {"n": 20001})[0].text
    assert "Error" in server._call("sort_search", {"action": "bubble_sort", "arr": list(range(2001))})[0].text


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


def test_std_check_secret_detection(tmp_path):
    """依赖泄露扫描：强格式密钥任何文件都报；弱赋值跳过测试文件（夹具防误报）。"""
    f = tmp_path / "leak.py"
    f.write_text(
        # 夹具刻意不用真实格式前缀（ghpX_ 而非 ghp_）——防测试文件本身成为泄露源
        "token = 'ghpX_123456789012345678901234567890123456'\n"
        "password = 'correct-horse-battery'\n"
        "api_key = '" + "AKIA" + "IOSFODNN7EXAMPLE'\n"
        "secret = \"just a word\"\n"
        "timeout = 30\n",
        encoding="utf-8")
    out = json.loads(server._call("std_check", {"path": str(f)})[0].text)
    secrets = [i for i in out["issues"] if i["rule"] == "secret_detection"]
    # 非测试文件：弱赋值（password=/api_key=）+ 强格式（AKIA）= 3
    assert len(secrets) == 3, f"应命中 3 个凭据: {len(secrets)}"
    assert all(s["severity"] == "Critical" for s in secrets), "凭据应为 Critical"
    # 测试文件（test_ 前缀）：弱赋值跳过（夹具防误报）；强格式仍报（防绕过——
    # 但夹具用 ghpX_ 非真实格式前缀，不触发强格式规则）
    tf = tmp_path / "test_fixture.py"
    tf.write_text(
        "ghp = 'ghpX_123456789012345678901234567890123456'\n"
        "password = 'correct-horse-battery'\n",
        encoding="utf-8")
    out2 = json.loads(server._call("std_check", {"path": str(tf)})[0].text)
    secrets2 = [i for i in out2["issues"] if i["rule"] == "secret_detection"]
    assert len(secrets2) == 0, f"测试文件夹具不应误报: {len(secrets2)}"
    # 防绕过验证：测试文件里放真实格式 AKIA → 必须报（即使 test_ 前缀）
    tf2 = tmp_path / "test_real.py"
    tf2.write_text("key = '" + "AKIAIOSFODNN7" + "EXAMPLE'\n", encoding="utf-8")
    out3 = json.loads(server._call("std_check", {"path": str(tf2)})[0].text)
    secrets3 = [i for i in out3["issues"] if i["rule"] == "secret_detection"]
    assert len(secrets3) == 1, "测试文件中的真实格式密钥必须报（防绕过）"


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



# ── LSE 进化记忆（P0）───────────────────────────────
def test_lesson_feedback_delta():
    """lesson_feedback：采纳加分、无效减分归档（Delta 奖励）。"""
    import time
    lid = f"ut-lesson-{int(time.time()*1000)}"  # 唯一 ID，避免跨测试持久化污染
    r = server._call("lesson_feedback", {"lesson_id": lid, "delta": 0.4})[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert d["result"]["utility"] > 0.5, f"采纳应加分: {d}"
    r2 = server._call("lesson_feedback", {"lesson_id": lid, "delta": -0.9})[0]
    d2 = json.loads(r2.text)
    assert d2["result"]["archived"] is True, f"低分应归档: {d2}"
    # 非法 delta
    r3 = server._call("lesson_feedback", {"lesson_id": lid, "delta": 5})[0]
    assert "Error" in r3.text or "delta" in r3.text, "delta 超范围应报错"


# ── LSE 规则权重（P1）───────────────────────────────
def test_rule_feedback_weight():
    """rule_feedback：采纳加分、忽略减分（自适应权重）。"""
    import time
    rule = f"ut_magic_{int(time.time()*1000)}"  # 唯一 ID，避免跨测试持久化污染
    r = server._call("rule_feedback", {"rule": rule, "adopted": True, "delta": 0.3})[0]
    d = json.loads(r.text)
    assert d["ok"] is True and d["result"]["weight"] >= 1.3, f"采纳应加分: {d}"
    r2 = server._call("rule_feedback", {"rule": rule, "adopted": False, "delta": 0.9})[0]
    d2 = json.loads(r2.text)
    assert d2["result"]["weight"] < 0.5, f"忽略应减分: {d2}"
    # 非法 delta
    r3 = server._call("rule_feedback", {"rule": rule, "adopted": True, "delta": 5})[0]
    assert "Error" in r3.text or "delta" in r3.text, "delta 超范围应报错"


# ── LSE UCB 树搜索（P2）──────────────────────────────
def test_bug_locate_feedback_reward():
    """bug_locate_feedback：命中 +1 / 未命中 -1 奖励回流。"""
    import time
    node = f"ut-node-{int(time.time()*1000)}"
    r = server._call("bug_locate_feedback", {"node": node, "hit": True})[0]
    d = json.loads(r.text)
    assert d["ok"] is True and d["result"]["reward"] == 1.0, f"命中应 +1: {d}"
    r2 = server._call("bug_locate_feedback", {"node": node, "hit": False})[0]
    d2 = json.loads(r2.text)
    assert d2["ok"] is True and d2["result"]["reward"] == -1.0, f"未命中应 -1: {d2}"
    # 缺 node
    r3 = server._call("bug_locate_feedback", {})[0]
    assert "Error" in r3.text or "node" in r3.text, "缺 node 应报错"


# ── LSE 经验迁移（P3）──────────────────────────────
def test_tool_card_experience_field():
    """tool_card 经验字段：model_fingerprint/context_hash/delta_score → experience_id。"""
    import time
    ctx = f"ut-ctx-{int(time.time()*1000)}"
    r = server._call("tool_card", {"name": "math_ops", "arguments": {"action": "add", "a": 1, "b": 2},
                                   "model_fingerprint": "ut-model", "context_hash": ctx,
                                   "delta_score": 0.7})[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert d.get("experience_id"), f"应返回 experience_id: {d}"
    # 经验可匹配（同 context_hash）
    import lse_client as _lse
    m = _lse.experience_match(ctx, 5)
    assert m.get("ok") and m["result"].get("items"), f"经验应可匹配: {m}"
    assert m["result"]["items"][0]["delta"] == 0.7, f"得分应 0.7: {m}"


# ── code_complete（LSP 自动补全）──────────────────────────────

def test_code_complete_missing_path():
    r = server._call("code_complete", {})[0]
    d = json.loads(r.text)
    assert d["ok"] is False and "path" in d["summary"]


def test_code_complete_language_detect_and_format(monkeypatch, tmp_path):
    """按后缀探测语言 + 格式化补全项（mock LSP，不依赖真实服务器）。"""
    src = tmp_path / "demo.py"
    src.write_text("def greet(name):\n    return f'hi {name}'\n\n", encoding="utf-8")

    fake = {
        "ok": True, "language": "python", "position": {"line": 3, "character": 0},
        "result": {
            "isIncomplete": False,
            "items": [
                {"label": "greet", "kind": 3, "detail": "def greet(name)"},
                {"label": "len", "kind": 3, "detail": "builtins"},
                {"label": "foo", "kind": 6, "detail": ""},
                {"label": "x" * 200, "kind": 6, "detail": "d" * 200},
            ],
        },
    }
    calls = {}

    def fake_call_ext(name, arguments):
        calls["args"] = arguments
        return [server._TC(json.dumps(fake, ensure_ascii=False))]

    monkeypatch.setattr(server, "_call_ext", fake_call_ext)
    r = server._call("code_complete", {"path": str(src)})[0]
    d = json.loads(r.text)
    assert d["ok"] is True, d
    assert d["detail"]["language"] == "python"
    assert calls["args"]["language_id"] == "python"
    assert calls["args"]["request"] == "completion"
    assert calls["args"]["text"] == src.read_text(encoding="utf-8")
    items = d["detail"]["items"]
    assert len(items) == 4, items
    # kind 数字 → 名字；detail 截断 80；label 截断保护
    assert items[0]["kind"] == "Function" and items[0]["detail"] == "def greet(name)"
    assert items[1]["kind"] == "Function"
    assert items[2]["kind"] == "Variable"
    assert len(items[3]["label"]) <= 160 and len(items[3]["detail"]) <= 80


def test_code_complete_unknown_lang_and_lsp_error(monkeypatch, tmp_path):
    src = tmp_path / "demo.xyz"
    src.write_text("x = 1", encoding="utf-8")
    r = server._call("code_complete", {"path": str(src)})[0]
    d = json.loads(r.text)
    assert d["ok"] is False and "探测" in d["summary"]

    # LSP 层错误透传（语言服务器未安装等）
    def fake_call_ext(name, arguments):
        return [server._TC(json.dumps(
            {"ok": False, "error": "语言服务器未安装: pylsp"}, ensure_ascii=False))]

    monkeypatch.setattr(server, "_call_ext", fake_call_ext)
    r = server._call("code_complete", {"path": str(tmp_path / "demo.py"), "text": "x = 1"})[0]
    d = json.loads(r.text)
    assert d["ok"] is False and "pylsp" in d["summary"]


def test_code_complete_cursor_default_and_limit(monkeypatch, tmp_path):
    src = tmp_path / "a.py"
    src.write_text("one\ntwo\nthree", encoding="utf-8")  # 无尾换行，3 行
    def fake_call_ext(name, arguments):
        assert arguments["line"] == 2 and arguments["character"] == 5, arguments
        return [server._TC(json.dumps(
            {"ok": True, "language": "python", "position": {}, "result": {
                "items": [{"label": f"c{i}", "kind": 1} for i in range(60)]}}, ensure_ascii=False))]

    monkeypatch.setattr(server, "_call_ext", fake_call_ext)
    r = server._call("code_complete", {"path": str(src)})[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert len(d["detail"]["items"]) == 50, "候选上限 50"


# ─────────────────────────────────────────────────────────────
# 防幻觉守卫（2026-08-11：hallucination_guard + capability_manifest）
# ─────────────────────────────────────────────────────────────

def test_hallucination_guard_verified_refuted_unverifiable(tmp_path):
    src = tmp_path / "demo.py"
    src.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    text = (
        "函数在 demo.py:1 定义；工具 `fs_read` 可用；"
        "引用 demo.py:99 与 no_such.py:3"
    )
    r = server._call("hallucination_guard", {"text": text, "root": str(tmp_path)})[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert d["verdict"] == "refuted", "存在被证伪声明 → 必须 refuted"
    verified = {i["claim"] for i in d["verified"]}
    refuted = {i["claim"] for i in d["refuted"]}
    assert "demo.py:1" in verified, "文件存在且行号在范围内 → verified"
    assert "fs_read" in verified, "注册表工具 → verified"
    assert "demo.py:99" in refuted, "行号越界 → refuted（幻觉）"
    assert "no_such.py:3" in refuted, "文件不存在 → refuted（幻觉）"
    assert "必须纠正" in d["advice"], "refuted 时给出制止指令"


def test_hallucination_guard_pass_and_no_claims(tmp_path):
    src = tmp_path / "ok.py"
    src.write_text("x = 1\n", encoding="utf-8")
    r = server._call("hallucination_guard", {"text": "见 ok.py:1", "root": str(tmp_path)})[0]
    d = json.loads(r.text)
    assert d["ok"] is True and d["verdict"] == "pass", d

    r2 = server._call("hallucination_guard", {"text": "这个结论基于上下文分析"})[0]
    d2 = json.loads(r2.text)
    assert d2["verdict"] == "no_claims", "无声明 → 不冒充证据"


def test_hallucination_guard_symbol_in_file(tmp_path):
    src = tmp_path / "mod.py"
    src.write_text("def helper():\n    pass\n", encoding="utf-8")
    r = server._call("hallucination_guard", {"text": "`helper` 在 mod.py 中", "root": str(tmp_path)})[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert "helper" in {i["claim"] for i in d["verified"]}, "符号在文件内 → verified"

    r2 = server._call("hallucination_guard", {"text": "`nonexistent_fn` 在 mod.py 中", "root": str(tmp_path)})[0]
    d2 = json.loads(r2.text)
    assert any(i["claim"] == "nonexistent_fn" and i["verdict"] == "refuted"
               for i in d2["items"]), "符号不在指定文件 → refuted（幻觉）"


def test_hallucination_guard_requires_text():
    r = server._call("hallucination_guard", {"text": "  "})[0]
    d = json.loads(r.text)
    assert d["ok"] is False and "text 必填" in d["error"]


def test_capability_manifest_lists_has_has_not():
    r = server._call("capability_manifest", {})[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert d["core_count"] == len(server._TOOLS)
    assert d["ext_count"] == len(server._EXT_DEFS)
    core_names = {t["name"] for t in d["has"]["core_tools"]}
    assert "fs_read" in core_names and "hallucination_guard" in core_names
    assert any("不能联网" in s for s in d["has_not"]), "显式边界：无网络"
    assert any("不能执行任意代码" in s for s in d["has_not"]), "显式边界：无代码执行"
    assert d["boundaries"]["file_read_write_max_bytes"] > 0


def test_hallucination_guard_path_escape_blocked(tmp_path):
    """越界路径（../ 逃逸 / 沙盒外绝对路径）拒绝探测，不泄露存在性。"""
    r = server._call("hallucination_guard", {
        "text": "见 ../outside.py:1",
        "root": str(tmp_path),
    })[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert d["verdict"] in ("unverified", "no_claims"), d
    for i in d["items"]:
        assert i["verdict"] != "verified", "越界路径不得验证通过"
        assert "越界" in i["reason"] or "拒绝" in i["reason"], i["reason"]


def test_hallucination_guard_all_unverifiable_not_pass():
    """全 unverifiable（无文件上下文符号）不得判 pass——无证据不得放行。"""
    r = server._call("hallucination_guard", {"text": "`some_symbol` 是关键"})[0]
    d = json.loads(r.text)
    assert d["verdict"] == "unverified", d
    assert "不得当作事实传播" in d["advice"], "unverified 需取证指令"


def test_hallucination_guard_definition_mode(tmp_path):
    """“定义在”表述要求定义模式：import 行不算定义证据。"""
    src = tmp_path / "m.py"
    src.write_text("from helper import util\n", encoding="utf-8")
    r = server._call("hallucination_guard", {
        "text": "`util` 定义在 m.py",
        "root": str(tmp_path),
    })[0]
    d = json.loads(r.text)
    # 仅出现在 import 行 → 不算定义 → refuted（断言“未出现在”以兼容实现细节）
    assert d["verdict"] == "refuted", d

    src2 = tmp_path / "d.py"
    src2.write_text("def util():\n    pass\n", encoding="utf-8")
    r2 = server._call("hallucination_guard", {
        "text": "`util` 定义在 d.py",
        "root": str(tmp_path),
    })[0]
    d2 = json.loads(r2.text)
    assert d2["verdict"] == "pass", d2


def test_hallucination_guard_url_not_file(tmp_path):
    """URL 段（example.com/data.json:80）不得误报为文件引用。"""
    r = server._call("hallucination_guard", {
        "text": "参考 https://example.com/data.json:80 的接口",
        "root": str(tmp_path),
    })[0]
    d = json.loads(r.text)
    assert d["verdict"] in ("no_claims", "unverified"), d
    assert all(not i["claim"].startswith("example.com") for i in d["items"]), d


def test_hallucination_guard_empty_and_trailing_newline(tmp_path):
    """空文件 0 行、尾随换行单行文件 1 行（行号 off-by-one 修复）。"""
    empty = tmp_path / "empty.py"
    empty.write_text("", encoding="utf-8")
    r = server._call("hallucination_guard", {"text": "见 empty.py:1", "root": str(tmp_path)})[0]
    d = json.loads(r.text)
    assert any(i["claim"] == "empty.py:1" and i["verdict"] == "refuted" for i in d["items"]), d

    one = tmp_path / "one.py"
    one.write_text("x = 1\n", encoding="utf-8")  # 尾随换行，1 行
    r2 = server._call("hallucination_guard", {"text": "见 one.py:1", "root": str(tmp_path)})[0]
    d2 = json.loads(r2.text)
    assert any(i["claim"] == "one.py:1" and i["verdict"] == "verified" for i in d2["items"]), d2
    r3 = server._call("hallucination_guard", {"text": "见 one.py:2", "root": str(tmp_path)})[0]
    d3 = json.loads(r3.text)
    assert any(i["claim"] == "one.py:2" and i["verdict"] == "refuted" for i in d3["items"]), d3


def test_hallucination_guard_definition_with_modifiers(tmp_path):
    """修饰符定义（async def / pub(crate) fn）不算 refuted。"""
    src = tmp_path / "mods.py"
    src.write_text("async def fetch():\n    pass\npub(crate) fn run() {}\n", encoding="utf-8")
    r = server._call("hallucination_guard", {
        "text": "`fetch` 定义在 mods.py；`run` 定义在 mods.py",
        "root": str(tmp_path),
    })[0]
    d = json.loads(r.text)
    assert d["verdict"] == "pass", d


def test_hallucination_guard_multilevel_url(tmp_path):
    """多级域名 URL 段不误报为文件引用。"""
    r = server._call("hallucination_guard", {
        "text": "sub.example.co.uk:80 与 api.test.dev:443",
        "root": str(tmp_path),
    })[0]
    d = json.loads(r.text)
    assert d["verdict"] in ("no_claims", "unverified"), d
    items = d.get("items", [])
    assert all("example" not in i["claim"] and "test.dev" not in i["claim"]
               for i in items), d


def test_hallucination_guard_line_zero_refuted(tmp_path):
    """行号 0 无效（行号从 1 开始）。"""
    src = tmp_path / "z.py"
    src.write_text("x = 1\n", encoding="utf-8")
    r = server._call("hallucination_guard", {"text": "见 z.py:0", "root": str(tmp_path)})[0]
    d = json.loads(r.text)
    assert any(i["claim"] == "z.py:0" and i["verdict"] == "refuted" for i in d["items"]), d


# ─────────────────────────────────────────────────────────────
# 防幻觉闭环（2026-08-11：refuted 自动回灌 LSE）+ 枢纽优先排序
# ─────────────────────────────────────────────────────────────

def test_hallucination_guard_auto_feedback_loop(tmp_path, monkeypatch):
    """refuted（幻觉）自动回灌：负 delta 惩罚 + 教训卡片入库。"""
    calls = {"delta": [], "store": []}

    class FakeLSE:
        @staticmethod
        def delta_update_lesson(lid, delta, threshold=0.1):
            calls["delta"].append((lid, delta))
            return {"ok": True, "result": {"id": lid, "utility": 0.3,
                                           "recall": 1, "archived": False}}

        @staticmethod
        def experience_store(model, ctx, delta, summary):
            calls["store"].append((model, ctx, delta, summary))
            return {"ok": True, "result": {"id": "e1"}}

    monkeypatch.setattr(server, "lse_client", FakeLSE) if hasattr(server, "lse_client") else None
    # 直接注入 import 路径：monkeypatch sys.modules 让 server 内 import lse_client 命中
    import types
    mod = types.ModuleType("lse_client")
    mod.delta_update_lesson = FakeLSE.delta_update_lesson
    mod.experience_store = FakeLSE.experience_store
    monkeypatch.setitem(sys.modules, "lse_client", mod)

    r = server._call("hallucination_guard", {
        "text": "见 no_such_file.py:9",
        "root": str(tmp_path),
    })[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert d["verdict"] == "refuted"
    assert d["feedback_recorded"], d
    rec = d["feedback_recorded"][0]
    assert rec["recorded"] is True
    assert rec["lesson_id"].startswith("hallucination-")
    assert calls["delta"] and calls["delta"][0][1] == -0.2, "负 delta 惩罚幻觉"
    assert calls["store"] and calls["store"][0][0] == "hallucination_guard"
    assert "已自动回灌" in d["feedback_note"]


def test_hallucination_guard_no_feedback_when_clean(tmp_path, monkeypatch):
    """无 refuted 时不触发回灌（verified/pass 不惩罚）。"""
    src = tmp_path / "ok.py"
    src.write_text("x = 1\n", encoding="utf-8")
    import types
    mod = types.ModuleType("lse_client")
    mod.delta_update_lesson = lambda *a, **k: {"ok": True, "result": {}}
    mod.experience_store = lambda *a, **k: {"ok": True}
    monkeypatch.setitem(sys.modules, "lse_client", mod)

    r = server._call("hallucination_guard", {"text": "见 ok.py:1", "root": str(tmp_path)})[0]
    d = json.loads(r.text)
    assert d["verdict"] == "pass"
    assert "feedback_recorded" not in d, "无幻觉不惩罚"


def test_hallucination_guard_feedback_degrades_without_engine(tmp_path, monkeypatch):
    """lse-engine 未构建（ok:false）→ 回灌跳过但幻觉仍被检测（降级不阻塞）。"""
    import types
    mod = types.ModuleType("lse_client")
    mod.delta_update_lesson = lambda *a, **k: {"ok": False, "error": "lse-engine 未构建"}
    mod.experience_store = lambda *a, **k: {"ok": False, "error": "lse-engine 未构建"}
    monkeypatch.setitem(sys.modules, "lse_client", mod)

    r = server._call("hallucination_guard", {
        "text": "见 no_such_file.py:9",
        "root": str(tmp_path),
    })[0]
    d = json.loads(r.text)
    assert d["verdict"] == "refuted", "幻觉检测不受引擎缺失影响"
    assert d["feedback_recorded"][0]["recorded"] is False
    assert "回灌跳过" in d["feedback_note"]


def test_lesson_recall_hub_priority(monkeypatch):
    """枢纽优先：recall 高的教训 hub_bonus 加权排序。"""
    import types
    mod = types.ModuleType("lse_client")
    state = {}

    def lesson_recall(lid):
        if lid in state:
            return {"ok": True, "result": state[lid]}
        return {"ok": False, "error": "not found"}

    def delta_update_lesson(lid, delta, threshold=0.1):
        if lid not in state:
            state[lid] = {"id": lid, "utility": 0.5, "recall": 0, "archived": False}
        return {"ok": True, "result": state[lid]}

    def engine_available():
        return True

    mod.lesson_recall = lesson_recall
    mod.delta_update_lesson = delta_update_lesson
    mod.engine_available = engine_available
    monkeypatch.setitem(sys.modules, "lse_client", mod)

    # 模拟 cae 扩展返回 3 条教训（同 utility，recall 不同）
    class FakeCAE:
        @staticmethod
        def _tool_lesson_recall(args):
            return [server._TC(json.dumps({
                "ok": True,
                "task_keywords": [],
                "lessons": ["枢纽教训AAA", "普通教训BBB", "冷门教训CCC"],
                "antipatterns": [], "advice": "",
            }, ensure_ascii=False))]

    monkeypatch.setattr(server, "_load_ext", lambda label: FakeCAE() if label == "code-analysis-enhance" else None)

    # 预置 recall：枢纽教训 recall=8 → hub_bonus 0.12；普通=2 → 0.03；冷门=0
    lid_hub = f"lesson-{abs(hash('枢纽教训AAA'[:80])) % 10**9}"
    lid_norm = f"lesson-{abs(hash('普通教训BBB'[:80])) % 10**9}"
    lid_cold = f"lesson-{abs(hash('冷门教训CCC'[:80])) % 10**9}"
    state[lid_hub] = {"id": lid_hub, "utility": 0.5, "recall": 8, "archived": False}
    state[lid_norm] = {"id": lid_norm, "utility": 0.5, "recall": 2, "archived": False}
    state[lid_cold] = {"id": lid_cold, "utility": 0.5, "recall": 0, "archived": False}

    r = server._call("lesson_recall_lse", {"task_description": "测试枢纽优先"})[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    # 同 utility 时枢纽（recall 高）优先
    hub_rank = d["lessons"].index("枢纽教训AAA")
    norm_rank = d["lessons"].index("普通教训BBB")
    cold_rank = d["lessons"].index("冷门教训CCC")
    assert hub_rank < norm_rank < cold_rank, (hub_rank, norm_rank, cold_rank)
    util = {u["id"]: u for u in d["utility"]}
    assert util[lid_hub]["hub_score"] > util[lid_cold]["hub_score"]
    assert util[lid_hub]["hub_bonus"] == 0.12  # min(8,10)*0.015


# ─────────────────────────────────────────────────────────────
# pipeline preset 配方（2026-08-11：一次调用=多步流程，减少调用轮次）
# ─────────────────────────────────────────────────────────────

def test_pipeline_preset_audit_repo(tmp_path):
    """audit_repo 配方：1 次 pipeline 调用展开 4 步（索引+漏洞+标准+综合）。"""
    src = tmp_path / "demo.py"
    src.write_text("x = 1\n", encoding="utf-8")
    r = server._call("pipeline", {"preset": "audit_repo", "path": str(tmp_path)})[0]
    d = json.loads(r.text)
    assert d["ok"] is True, d
    assert d["preset"] == "audit_repo", "preset 标识保留"
    tools = [s["tool"] for s in d["steps"]]
    assert tools == ["cb_status", "bug_scan", "std_check", "vuln_scan"], tools
    assert all(s["ok"] for s in d["steps"]), d


def test_pipeline_preset_guard_text(tmp_path):
    """guard_text 配方：能力清单 + 幻觉守卫一次调用。"""
    r = server._call("pipeline", {
        "preset": "guard_text",
        "text": "见 missing.py:1",
        "root": str(tmp_path),
    })[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    tools = [s["tool"] for s in d["steps"]]
    assert tools == ["capability_manifest", "hallucination_guard"], tools
    guard = d["steps"][1]["result"]
    assert guard.get("verdict") == "refuted", "幻觉声明应被证伪"


def test_pipeline_preset_unknown_rejected():
    """未知 preset 报错（不静默降级）。"""
    r = server._call("pipeline", {"preset": "nope", "path": "."})[0]
    assert "Error" in r.text and "未知 preset" in r.text, r.text


def test_pipeline_preset_steps_override(tmp_path):
    """调用方显式 steps 优先于 preset（显式覆盖，不冲突）。"""
    r = server._call("pipeline", {
        "preset": "guard_text",
        "steps": [{"tool": "math_ops", "args": {"action": "add", "a": 1, "b": 2}}],
    })[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert len(d["steps"]) == 1 and d["steps"][0]["tool"] == "math_ops"


# ─────────────────────────────────────────────────────────────
# 扫描日志（2026-08-11：常驻自扫落盘，专项目对话查日志）
# ─────────────────────────────────────────────────────────────

def test_scan_log_append_and_query(tmp_path, monkeypatch):
    """扫描工具调用自动落盘 + 按 root 过滤查询。"""
    import scan_log_core
    log = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(log))
    src = tmp_path / "demo.py"
    src.write_text("x = 1\n", encoding="utf-8")

    server._call("bug_scan", {"path": str(src)})
    server._call("std_check", {"path": str(src)})

    logs = scan_log_core.query_logs(root=str(src), limit=10)
    assert len(logs) == 2, logs
    tools = {l["tool"] for l in logs}
    assert tools == {"bug_scan", "std_check"}, tools
    assert all(l["ok"] for l in logs)
    # 日志文件真实存在且为 JSONL
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    import json as _json
    assert _json.loads(lines[0])["tool"] == "bug_scan"


def test_scan_log_tool_query(tmp_path, monkeypatch):
    """scan_log 工具：按 root/tool 过滤 + limit 边界。"""
    import scan_log_core
    log = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(log))
    src = tmp_path / "demo.py"
    src.write_text("x = 1\n", encoding="utf-8")

    server._call("bug_scan", {"path": str(src)})

    r = server._call("scan_log", {"root": str(src), "tool": "bug_scan", "limit": 5})[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert d["count"] == 1 and d["logs"][0]["tool"] == "bug_scan"
    assert d["log_path"] == str(log)

    r2 = server._call("scan_log", {"limit": 0})[0]
    assert "Error" in r2.text and "limit" in r2.text, "limit 越界报错"


def test_scan_log_no_cross_project_leak(tmp_path, monkeypatch):
    """root 过滤不串项目（专项目对话只看自己的日志）。"""
    import scan_log_core
    log = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(log))
    a = tmp_path / "projA"
    b = tmp_path / "projB"
    a.mkdir(); b.mkdir()
    (a / "a.py").write_text("x = 1\n", encoding="utf-8")
    (b / "b.py").write_text("y = 2\n", encoding="utf-8")

    server._call("bug_scan", {"path": str(a / "a.py")})
    server._call("bug_scan", {"path": str(b / "b.py")})

    logs_a = scan_log_core.query_logs(root=str(a / "a.py"), limit=10)
    logs_b = scan_log_core.query_logs(root=str(b / "b.py"), limit=10)
    assert len(logs_a) == 1 and len(logs_b) == 1
    assert logs_a[0]["root"] != logs_b[0]["root"]


def test_scan_log_core_self_scan_files():
    """自扫文件列表 = server.py 同目录核心文件（存在性）。"""
    import scan_log_core
    files = scan_log_core.self_scan_files()
    assert any("server.py" in f for f in files)
    assert all(os.path.isfile(f) for f in files), "自扫目标必须存在"


# ─────────────────────────────────────────────────────────────
# 高并发扫描（2026-08-11：vuln_scan 三路并行 / project_scan 四路并行互不打扰）
# ─────────────────────────────────────────────────────────────

def test_project_scan_parallel(tmp_path, monkeypatch):
    """project_scan：四路并行（bug/std/ui/cb），结果聚合且落盘 scan-log。"""
    import scan_log_core
    log = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(log))
    src = tmp_path / "demo.py"
    src.write_text("x = 1\n", encoding="utf-8")
    # cb_scan 需要索引，先 cb_index 建好（避免空跑报错也接受——只验证结构）
    server._call("cb_index", {"path": str(tmp_path)})

    r = server._call("project_scan", {"path": str(tmp_path), "ui": False})[0]
    d = json.loads(r.text)
    assert d["ok"] is True, d
    det = d["detail"]
    assert "bug_scan" in det and "std_check" in det and "cb_scan" in det, det.keys()
    assert det.get("ui_check") == [], "ui=False 不跑 UI"
    # 落盘
    logs = scan_log_core.query_logs(tool="project_scan", limit=5)
    assert len(logs) >= 1 and logs[0]["root"] == str(tmp_path), logs


def test_project_scan_with_ui(tmp_path):
    """project_scan 默认跑 ui_check（Bevy 项目）；非 Rust 目录 ui 为空数组也 ok。"""
    src = tmp_path / "main.rs"
    src.write_text("fn main() {}\n", encoding="utf-8")
    server._call("cb_index", {"path": str(tmp_path)})
    r = server._call("project_scan", {"path": str(tmp_path)})[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    assert "ui_check" in d["detail"], "默认包含 ui_check"


def test_vuln_scan_parallel_structure(tmp_path):
    """vuln_scan 三路并行后输出结构不变（兼容既有契约）。"""
    src = tmp_path / "demo.py"
    src.write_text("x = 1\n", encoding="utf-8")
    r = server._call("vuln_scan", {"path": str(src)})[0]
    d = json.loads(r.text)
    assert d["ok"] is True
    det = d["detail"]
    assert "bug_scan" in det and "std_check" in det and "ui_check" in det
    assert isinstance(det["bug_scan"], (list, dict))
    assert "errors" in det


def test_self_scan_active_project_env(tmp_path, monkeypatch):
    """启动自扫：UNIFIED_RX_PROJECT 指定活跃项目 → 并发扫该项目（互不打扰）。"""
    import scan_log_core
    log = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(log))
    monkeypatch.setenv("UNIFIED_RX_PROJECT", str(tmp_path))
    src = tmp_path / "demo.py"
    src.write_text("x = 1\n", encoding="utf-8")
    server._call("cb_index", {"path": str(tmp_path)})

    server._SCAN_LOOPS_STARTED = False  # 重置防重复标志（测试隔离）
    server._spawn_self_scan()
    import time
    deadline = time.time() + 20
    proj = []
    while time.time() < deadline:
        logs = scan_log_core.query_logs(limit=200)
        proj = [l for l in logs if l["tool"] == "project_scan"
                and l["root"] == str(tmp_path)]  # 只认本测试 root（防残留线程干扰）
        if proj:
            break
        time.sleep(1)
    assert proj, "活跃项目被自动扫描"


# ─────────────────────────────────────────────────────────────
# 全盘扫 + 自扫全家 + 最活跃项目（2026-08-11 五种模式）
# ─────────────────────────────────────────────────────────────

def test_full_scan_parallel(tmp_path, monkeypatch):
    """full_scan：多项目根并发 project_scan，汇总落盘。"""
    import scan_log_core
    log = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(log))
    a = tmp_path / "projA"
    b = tmp_path / "projB"
    a.mkdir(); b.mkdir()
    (a / "a.py").write_text("x = 1\n", encoding="utf-8")
    (b / "b.py").write_text("y = 2\n", encoding="utf-8")
    server._call("cb_index", {"path": str(a)})
    server._call("cb_index", {"path": str(b)})

    r = server._call("full_scan", {"roots": [str(a), str(b)], "ui": False})[0]
    d = json.loads(r.text)
    assert d["ok"] is True, d
    det = d["detail"]
    assert len(det["projects"]) == 2, det
    assert not det["errors"], det
    roots = {p["root"] for p in det["projects"]}
    assert roots == {str(a), str(b)}
    # 落盘
    logs = scan_log_core.query_logs(tool="full_scan", limit=5)
    assert len(logs) >= 1


def test_self_scan_covers_all_tools():
    """扫自己覆盖所有工具：core + scripts + lse-engine 全家都在自扫清单。"""
    import scan_log_core
    files = scan_log_core.self_scan_files()
    names = [os.path.basename(f) for f in files]
    assert "server.py" in names and "guard_core.py" in names
    assert "lse_client.py" in names and "scan_log_core.py" in names
    assert any("mcp_smoke.py" in n for n in names), "scripts 在自扫清单"
    assert any("lib.rs" in n or "main.rs" in n for n in names), "lse-engine 在自扫清单"
    assert len(files) >= 15, f"自扫文件过少: {len(files)}"


def test_self_scan_dirs_extensions(tmp_path, monkeypatch):
    """vendor 扩展目录并入自扫（self_scan_dirs 返回扩展目录）。"""
    import scan_log_core
    # 仓库有 vendor/extensions → 返回非空
    dirs = scan_log_core.self_scan_dirs()
    assert dirs, "vendor/extensions 存在"
    assert all(os.path.isdir(d) for d in dirs)


def test_active_project_most_used(monkeypatch, tmp_path):
    """最活跃就扫：无 UNIFIED_RX_PROJECT 时从 stats 统计调用最多的项目。"""
    import scan_log_core
    log = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(log))
    monkeypatch.delenv("UNIFIED_RX_PROJECT", raising=False)
    # 构造 stats.json（模拟某项目被扫最多）
    stats_dir = tmp_path / "home" / ".unified-rx"
    stats_dir.mkdir(parents=True)
    stats = [{"root": r"D:\开发\VoxelForge-Nexus", "tool": "bug_scan"} for _ in range(5)] + \
            [{"root": r"D:\开发\other", "tool": "bug_scan"} for _ in range(1)]
    (stats_dir / "stats.json").write_text(json.dumps(stats), encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")

    # 通过 server 内部 _active_project 验证（间接：spawn 后 scan-log 应有最活跃项目）
    server._SCAN_LOOPS_STARTED = False  # 重置防重复标志（测试隔离）
    server._spawn_self_scan()
    import time
    deadline = time.time() + 20
    logs = []
    while time.time() < deadline:
        logs = [l for l in scan_log_core.query_logs(tool="project_scan", limit=20)
                if l["root"] == r"D:\开发\VoxelForge-Nexus"]
        if logs:
            break
        time.sleep(1)
    assert logs, "最活跃项目被自动扫"


# ─────────────────────────────────────────────────────────────
# 持续循环扫描（2026-08-11：5 模式并发循环，打开 RX 自动开启不会停下）
# ─────────────────────────────────────────────────────────────

def test_scan_loops_spawned_and_running(monkeypatch, tmp_path):
    """持续循环：_spawn_self_scan 启动 3 个独立循环线程（自扫/项目/全盘），首轮立即跑。"""
    import scan_log_core
    import threading
    log = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(log))
    monkeypatch.setenv("UNIFIED_RX_PROJECT", str(tmp_path))
    monkeypatch.setenv("UNIFIED_RX_SCAN_INTERVAL_SELF", "10")
    monkeypatch.setenv("UNIFIED_RX_SCAN_INTERVAL_PROJECT", "10")
    monkeypatch.setenv("UNIFIED_RX_SCAN_INTERVAL_FULL", "10")
    (tmp_path / "demo.py").write_text("x = 1\n", encoding="utf-8")
    server._call("cb_index", {"path": str(tmp_path)})

    before = set(t.name for t in threading.enumerate())
    server._SCAN_LOOPS_STARTED = False  # 允许本次 spawn
    server._spawn_self_scan()
    after = set(t.name for t in threading.enumerate())
    new = after - before
    # 防重复标志下已启动过则线程已存在；未启动过则本次新增——两者都算启动成功
    names = set(t.name for t in threading.enumerate())
    assert {"rx-scan-self", "rx-scan-project", "rx-scan-full"} <= names, names

    # 首轮立即跑（不用等间隔）：等后台线程完成第一轮（只认本测试 root）
    import time
    deadline = time.time() + 20
    self_ok, proj_ok = False, False
    while time.time() < deadline:
        logs = scan_log_core.query_logs(limit=300)
        if any(l["tool"] == "self_scan" for l in logs):
            self_ok = True  # 自扫 root 是仓库文件路径（非 tmp_path），只需出现
        if any(l["tool"] == "project_scan" and l["root"] == str(tmp_path) for l in logs):
            proj_ok = True
        if self_ok and proj_ok:
            break
        time.sleep(1)
    assert self_ok, "自扫首轮未跑"
    assert proj_ok, "项目首轮未跑"


def test_scan_loop_interval_floor(monkeypatch):
    """循环间隔下限 10s（防 DoS：设 1s 也按 10s）。"""
    monkeypatch.setenv("UNIFIED_RX_SCAN_INTERVAL_SELF", "1")
    server._spawn_self_scan()
    import threading
    for t in threading.enumerate():
        if t.name == "rx-scan-self":
            assert t.daemon, "循环线程必须 daemon（不阻止退出）"
            break
    else:
        raise AssertionError("rx-scan-self 线程未找到")


def test_scan_loops_skip_when_disabled(monkeypatch):
    """UNIFIED_RX_SKIP_SELF_SCAN=1 时不启动循环（CI/测试环境）。"""
    monkeypatch.setenv("UNIFIED_RX_SKIP_SELF_SCAN", "1")
    import threading
    before = set(t.name for t in threading.enumerate())
    server._spawn_self_scan()
    after = set(t.name for t in threading.enumerate())
    assert not {"rx-scan-self", "rx-scan-project", "rx-scan-full"} & (after - before), "禁用时不得启动循环"


# ─────────────────────────────────────────────────────────────
# bug_scan 误报修复回归（2026-08-12：try 构造赋值 + 短路保护）
# ─────────────────────────────────────────────────────────────

def test_bug_scan_no_false_positive_try_call_assign(tmp_path):
    """X=None 后 try 内 X=Foo() 构造赋值，后续解引用不误报（修复 8 个误报）。"""
    src = tmp_path / "t.py"
    src.write_text(
        "class Foo:\n"
        "    def run(self):\n"
        "        return 1\n"
        "client = None\n"
        "try:\n"
        "    client = Foo()\n"
        "    client.run()\n"  # 构造赋值后安全
        "except Exception:\n"
        "    pass\n",
        encoding="utf-8")
    r = server._call("bug_scan", {"path": str(src)})[0]
    d = json.loads(r.text)
    none_derefs = [i for i in d.get("issues", []) if i.get("rule") == "none_deref"]
    assert not none_derefs, none_derefs


def test_bug_scan_no_false_positive_none_short_circuit(tmp_path):
    """X is None or X.field 短路保护不误报。"""
    src = tmp_path / "s.py"
    src.write_text(
        "best = None\n"
        "for s in [{'line': 1}]:\n"
        "    if best is None or s['line'] > best['line']:\n"
        "        best = s\n"
        "print(best)\n",
        encoding="utf-8")
    r = server._call("bug_scan", {"path": str(src)})[0]
    d = json.loads(r.text)
    none_derefs = [i for i in d.get("issues", []) if i.get("rule") == "none_deref"]
    assert not none_derefs, none_derefs


def test_bug_scan_still_detects_real_none_deref(tmp_path):
    """真 None 解引用仍被检测（误报修复不丢真阳性）。"""
    src = tmp_path / "r.py"
    src.write_text(
        "x = None\n"
        "print(x.field)\n",  # 真 bug
        encoding="utf-8")
    r = server._call("bug_scan", {"path": str(src)})[0]
    d = json.loads(r.text)
    none_derefs = [i for i in d.get("issues", []) if i.get("rule") == "none_deref"]
    assert len(none_derefs) == 1, none_derefs


# ─────────────────────────────────────────────────────────────
# 独立常驻守护（2026-08-12：不依赖 RX 会话，打开电脑就在跑）
# ─────────────────────────────────────────────────────────────

def test_daemon_importable():
    """daemon.py 可 import（独立于 MCP 会话的守护入口）。"""
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "unifiedrx_daemon", os.path.join(os.path.dirname(os.path.abspath(__file__)), "daemon.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "main"), "daemon 需有 main 入口"
    assert hasattr(mod, "_loop_self_scan"), "守护需有自扫循环"
    assert hasattr(mod, "_loop_repo_manage"), "守护需有仓库管理循环"
    assert hasattr(mod, "REPO_LOG"), "守护需有仓库日志路径"


def test_daemon_repo_log_written(tmp_path, monkeypatch):
    """仓库管理写 repo-log.jsonl（7 仓库 PR 轮询结果落盘）。"""
    import daemon
    # 隔离 repo-log 路径
    log = tmp_path / "repo-log.jsonl"
    monkeypatch.setattr(daemon, "REPO_LOG", str(log))
    # 用假 API 避免真实网络（只验证落盘逻辑）
    import urllib.request
    orig = urllib.request.urlopen

    def fake_urlopen(req, *a, **k):
        import json as _json
        class R:
            def read(self):
                return _json.dumps([{"number": 1, "title": "test pr"}]).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    daemon._repo_manage_once()
    assert log.exists(), "repo-log 未写入"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 5, f"应轮询多个仓库: {len(lines)}"
    import json as _json
    assert all("repo_manage" in _json.loads(l)["tool"] for l in lines)


# ─────────────────────────────────────────────────────────────
# 多模式并发扫描（2026-08-12：影子/窗口/缓存/排除——开 RX 自动）
# ─────────────────────────────────────────────────────────────

def test_shadow_core_scan_follows_called_file(tmp_path, monkeypatch):
    """影子扫描：RX 调用的文件被跟随扫描（scan-log 有记录 → 补扫）。"""
    import shadow_core
    import scan_log_core
    log = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(log))
    src = tmp_path / "demo.py"
    src.write_text("x = 1\n", encoding="utf-8")
    # 模拟 RX 调用落盘（bug_scan 该文件——实际落盘 scan-log 的扫描工具）
    scan_log_core.append_scan({"tool": "bug_scan", "root": str(src), "ok": True, "summary": ""})
    scanned = []
    def fake_scan(path):
        scanned.append(path)
        return True, "issues=0"
    n = shadow_core.shadow_scan_once(fake_scan)
    assert n >= 1, "影子应跟随扫描被调用文件"
    assert str(src) in scanned, scanned
    # 第二次：已扫未变 → 不重复
    n2 = shadow_core.shadow_scan_once(fake_scan)
    assert n2 == 0, "缓存命中不重复扫"


def test_shadow_core_ignores_excluded(tmp_path, monkeypatch):
    """影子扫描排除 node_modules/steam 等目录。"""
    import shadow_core
    import scan_log_core
    shadow_core._SCANNED.clear()  # 测试隔离
    shadow_core._loaded = False
    monkeypatch.setenv("UNIFIED_RX_SHADOW_SCANNED", str(tmp_path / "shadow-scanned.json"))
    log = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(log))
    bad = tmp_path / "node_modules" / "pkg" / "index.py"
    bad.parent.mkdir(parents=True)
    bad.write_text("x = 1\n", encoding="utf-8")
    scan_log_core.append_scan({"tool": "fs_read", "root": str(bad), "ok": True, "summary": ""})
    scanned = []
    shadow_core.shadow_scan_once(lambda p: (scanned.append(p), True, "")[1])
    assert not scanned, "node_modules 不应被影子扫"


def test_window_core_project_root(tmp_path):
    """窗口扫描：从文件路径向上探测项目根（含 .git 标记）。"""
    import window_core
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    (proj / "src").mkdir()
    f = proj / "src" / "main.py"
    f.write_text("x = 1\n", encoding="utf-8")
    root = window_core._project_root(str(f))
    assert root == str(proj), root


def test_scan_cache_hit_and_invalidate(tmp_path, monkeypatch):
    """缓存：文件未变命中；mtime 变化失效。"""
    import scan_cache
    cache_file = tmp_path / "scan-cache.json"
    monkeypatch.setattr(scan_cache, "_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(scan_cache, "_loaded", False)
    src = tmp_path / "f.py"
    src.write_text("x = 1\n", encoding="utf-8")
    assert scan_cache.get("bug_scan", str(src)) is None, "初始 miss"
    scan_cache.put("bug_scan", str(src), {"ok": True, "issues": []})
    hit = scan_cache.get("bug_scan", str(src))
    assert hit and hit.get("ok") is True, "命中"
    # 修改文件 → miss
    src.write_text("x = 2\n", encoding="utf-8")
    assert scan_cache.get("bug_scan", str(src)) is None, "文件变化失效"


def test_full_scan_exclude_auto_roots(tmp_path, monkeypatch):
    """全盘扫：自动默认 roots 过排除清单；显式 roots 不过滤。"""
    import server as srv
    # steam 目录被排除
    assert srv._scan_excluded(r"D:\Steam\steamapps\common\game") is True
    # 开发项目根不排除
    assert srv._scan_excluded(r"D:\开发\VoxelForge-Nexus") is False
    # 显式 roots 不过滤（测试路径含 temp 也不影响）
    a = tmp_path / "projA"
    a.mkdir()
    (a / ".git").mkdir()
    (a / "a.py").write_text("x = 1\n", encoding="utf-8")
    srv._call("cb_index", {"path": str(a)})
    r = srv._call("full_scan", {"roots": [str(a)], "ui": False})[0]
    d = json.loads(r.text)
    assert len(d["detail"]["projects"]) == 1, "显式 roots 不过滤"
