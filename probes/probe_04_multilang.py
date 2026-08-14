"""多语言契约探针（IDE 增强 260：用户点名"没有多语言处理 包括扫描"收官——
能力契约化：bug_scan 十语言 / std_check 八语言 / ui_check Godot / cb_scan Godot）。"""
import json
import os

import _common
from _common import probe  # noqa: F401
import server as S

_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_tmp")
os.makedirs(_TMP, exist_ok=True)


@probe("p13_bug_scan_multilang")
def p13():
    """bug_scan 十语言（go/c/cpp 确定性规则生效——unwrap/panic 等价）。"""
    c_file = os.path.join(_TMP, "sample.c")
    with open(c_file, "w", encoding="utf-8") as f:
        f.write("int main(void) {\n    int *p = 0;\n    *p = 1;\n    return 0;\n}\n")
    out = S._call("bug_scan", {"path": c_file})
    data = json.loads(out[0].text)
    n = data.get("issue_count", -1)
    if n >= 1:
        return True, f"c 文件检出 {n} 条"
    return False, f"c 文件应有检出: {data}"


@probe("p14_std_check_c")
def p14():
    """std_check 八语言（c/cpp magic_number + name_conflict）。"""
    c_file = os.path.join(_TMP, "sample_c.c")
    with open(c_file, "w", encoding="utf-8") as f:
        f.write("int helper(void) { return 0; }\n"
                "int helper(int x) { return x; }\n"
                "int main(void) {\n    int speed = 999;\n    return 0;\n}\n")
    out = S._call("std_check", {"path": c_file})
    data = json.loads(out[0].text)
    rules = {i.get("rule") for i in data.get("issues", [])}
    if "name_conflict" in rules and "magic_number" in rules:
        return True, f"c 双规则检出: {sorted(rules)}"
    return False, f"c 应检出 name_conflict+magic_number: {rules}"


@probe("p15_ui_check_godot")
def p15():
    """ui_check Godot 死按钮（257）。"""
    gd_file = os.path.join(_TMP, "menu.gd")
    with open(gd_file, "w", encoding="utf-8") as f:
        f.write("func _ready():\n    var b = Button.new()\n    add_child(b)\n")
    out = S._call("ui_check", {"path": gd_file})
    data = json.loads(out[0].text)
    ni = [i for i in data.get("issues", []) if i.get("rule") == "no_interaction"]
    if ni:
        return True, f"gd 死按钮检出 L{ni[0]['line']}"
    return False, f"gd 死按钮应检出: {data}"


@probe("p16_cb_scan_godot")
def p16():
    """cb_scan 含 Godot（259）。"""
    repo = os.path.join(_TMP, "cbgd")
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, "menu.gd"), "w", encoding="utf-8") as f:
        f.write("func _ready():\n    var b = Button.new()\n    add_child(b)\n")
    S._call("cb_index", {"path": repo})
    out = S._call("cb_scan", {"path": repo})
    data = json.loads(out[0].text)
    gd = [i for i in data.get("issues", []) if i.get("file", "").endswith(".gd")
          and i.get("rule") == "no_interaction"]
    if gd:
        return True, f"cb_scan gd 死按钮检出"
    return False, f"cb_scan 应含 gd 检出: {data}"


@probe("p17_bug_scan_go_rules")
def p17():
    """bug_scan go 确定性规则（261：nil map 写入 / goroutine / recover）。"""
    go_file = os.path.join(_TMP, "sample.go")
    with open(go_file, "w", encoding="utf-8") as f:
        f.write("package main\n"
                "var m map[string]int\n"
                "func f() {\n"
                "    m[\"k\"] = 1\n"
                "}\n")
    out = S._call("bug_scan", {"path": go_file})
    data = json.loads(out[0].text)
    nm = [i for i in data.get("issues", []) if i.get("rule") == "nil_map_write"]
    if nm:
        return True, f"go nil map 写入检出 L{nm[0]['line']}"
    return False, f"go nil map 写入应检出: {data}"
