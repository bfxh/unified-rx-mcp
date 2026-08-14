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


@probe("p18_bug_scan_tsjs_gd_rules")
def p18():
    """bug_scan ts/js/gd 确定性规则（262：eval / loose_eq / innerHTML / get_node）。"""
    ts_file = os.path.join(_TMP, "sample.ts")
    with open(ts_file, "w", encoding="utf-8") as f:
        f.write("const s = eval('1+1');\nif (x == 1) {}\n")
    out = S._call("bug_scan", {"path": ts_file})
    data = json.loads(out[0].text)
    rules = {i.get("rule") for i in data.get("issues", [])}
    if "dynamic_exec" in rules and "loose_eq" in rules:
        return True, f"ts 双规则检出: {sorted(rules)}"
    return False, f"ts 应检出 dynamic_exec+loose_eq: {rules}"


@probe("p19_bug_scan_py_jsx_rules")
def p19():
    """bug_scan py 可变默认参数 + jsx XSS（263）。"""
    py_file = os.path.join(_TMP, "sample_mut.py")
    with open(py_file, "w", encoding="utf-8") as f:
        f.write("def f(x=[]):\n    x.append(1)\n    return x\n")
    out = S._call("bug_scan", {"path": py_file})
    data = json.loads(out[0].text)
    mda = [i for i in data.get("issues", []) if i.get("rule") == "mutable_default_arg"]
    jsx_file = os.path.join(_TMP, "sample.jsx")
    with open(jsx_file, "w", encoding="utf-8") as f:
        f.write("const C = () => <div dangerouslySetInnerHTML={{__html: u}} />;\n")
    out = S._call("bug_scan", {"path": jsx_file})
    data2 = json.loads(out[0].text)
    xss = [i for i in data2.get("issues", []) if i.get("rule") == "xss_risk"]
    if mda and xss:
        return True, f"py mutable_default_arg + jsx xss_risk 双检出"
    return False, f"应双检出: mda={bool(mda)} xss={bool(xss)}"


@probe("p20_bug_scan_js_c_gd_rules")
def p20():
    """bug_scan js setTimeout / c realloc / gd free（264）。"""
    js_file = os.path.join(_TMP, "sample_to.js")
    with open(js_file, "w", encoding="utf-8") as f:
        f.write("setTimeout('alert(1)', 100);\n")
    out = S._call("bug_scan", {"path": js_file})
    se = [i for i in json.loads(out[0].text).get("issues", [])
          if i.get("rule") == "string_exec"]
    c_file = os.path.join(_TMP, "sample_rl.c")
    with open(c_file, "w", encoding="utf-8") as f:
        f.write("int main(void) {\n    buf = realloc(buf, 1024);\n}\n")
    out = S._call("bug_scan", {"path": c_file})
    rl = [i for i in json.loads(out[0].text).get("issues", [])
          if i.get("rule") == "realloc_unchecked"]
    if se and rl:
        return True, "js string_exec + c realloc_unchecked 双检出"
    return False, f"应双检出: se={bool(se)} rl={bool(rl)}"


@probe("p21_bug_scan_cs_lua_bash")
def p21():
    """bug_scan C#/Lua/Bash 新语言（265）。"""
    cs_file = os.path.join(_TMP, "sample.cs")
    with open(cs_file, "w", encoding="utf-8") as f:
        f.write("class P {\n    void M() {\n        Debug.Log(\"x\");\n    }\n}\n")
    out = S._call("bug_scan", {"path": cs_file})
    cs_ok = any(i.get("rule") == "debug_residue"
                for i in json.loads(out[0].text).get("issues", []))
    sh_file = os.path.join(_TMP, "sample.sh")
    with open(sh_file, "w", encoding="utf-8") as f:
        f.write("eval \"$CMD\"\n")
    out = S._call("bug_scan", {"path": sh_file})
    sh_ok = any(i.get("rule") == "shell_injection"
                for i in json.loads(out[0].text).get("issues", []))
    if cs_ok and sh_ok:
        return True, "cs debug_residue + sh shell_injection 双检出"
    return False, f"应双检出: cs={cs_ok} sh={sh_ok}"


@probe("p22_std_cb_cs_lua_sh")
def p22():
    """std_check cs/lua + cb_index cs/lua/sh 符号（266）。"""
    repo = os.path.join(_TMP, "cslua")
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, "P.cs"), "w", encoding="utf-8") as f:
        f.write("class Player {\n    void Run() {\n        int speed = 999;\n    }\n}\n")
    out = S._call("std_check", {"path": os.path.join(repo, "P.cs")})
    mn = any(i.get("rule") == "magic_number"
             for i in json.loads(out[0].text).get("issues", []))
    S._call("cb_index", {"path": repo})
    out = S._call("cb_status", {"path": repo})
    idx = json.load(open(os.path.join(repo, ".unified-rx-index", "index.json"),
                         encoding="utf-8"))
    cs_syms = idx["files"].get("P.cs", {}).get("symbols", {})
    if mn and "Player" in cs_syms:
        return True, f"cs magic_number + 符号 Player 检出"
    return False, f"应双检出: mn={mn} syms={list(cs_syms)}"


@probe("p23_ui_check_cs_unity")
def p23():
    """ui_check Unity（.cs）死按钮（267）。"""
    cs_file = os.path.join(_TMP, "sample_unity.cs")
    with open(cs_file, "w", encoding="utf-8") as f:
        f.write("using UnityEngine.UI;\n"
                "public class M {\n"
                "    public Button startBtn;\n"
                "    void Start() {\n"
                "        startBtn.onClick.AddListener(Go);\n"
                "    }\n"
                "}\n")
    out = S._call("ui_check", {"path": cs_file})
    data = json.loads(out[0].text)
    ni = [i for i in data.get("issues", []) if i.get("rule") == "no_interaction"]
    # 有 onClick 连接 → 不报（契约：不误报）
    if not ni:
        return True, "Unity Button 有连接不报（无死按钮）"
    return False, f"有连接不应报死按钮: {ni}"


@probe("p24_cb_scan_cs_unity")
def p24():
    """cb_scan Unity（.cs）死按钮（268）。"""
    repo = os.path.join(_TMP, "cbcs")
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, "Menu.cs"), "w", encoding="utf-8") as f:
        f.write("using UnityEngine.UI;\n"
                "public class Menu {\n"
                "    public Button startBtn;\n"
                "}\n")
    S._call("cb_index", {"path": repo})
    out = S._call("cb_scan", {"path": repo})
    data = json.loads(out[0].text)
    cs = [i for i in data.get("issues", [])
          if i.get("file", "").endswith(".cs") and i.get("rule") == "no_interaction"]
    if cs:
        return True, "cb_scan cs 死按钮检出"
    return False, f"cb_scan 应含 cs 检出: {data}"


@probe("p25_bug_scan_java_ps1")
def p25():
    """bug_scan Java/PowerShell 新语言（269）。"""
    java_file = os.path.join(_TMP, "sample.java")
    with open(java_file, "w", encoding="utf-8") as f:
        f.write("class A {\n    void m() {\n        System.out.println(\"x\");\n    }\n}\n")
    out = S._call("bug_scan", {"path": java_file})
    java_ok = any(i.get("rule") == "debug_residue"
                  for i in json.loads(out[0].text).get("issues", []))
    ps_file = os.path.join(_TMP, "sample.ps1")
    with open(ps_file, "w", encoding="utf-8") as f:
        f.write("Invoke-Expression $input\n")
    out = S._call("bug_scan", {"path": ps_file})
    ps_ok = any(i.get("rule") == "shell_injection"
                for i in json.loads(out[0].text).get("issues", []))
    if java_ok and ps_ok:
        return True, "java debug_residue + ps1 shell_injection 双检出"
    return False, f"应双检出: java={java_ok} ps1={ps_ok}"


@probe("p26_std_cb_java_ps1")
def p26():
    """std_check java + cb_index ps1 符号（270）。"""
    repo = os.path.join(_TMP, "jk270")
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, "A.java"), "w", encoding="utf-8") as f:
        f.write("class App {\n    void run() {\n        int speed = 999;\n    }\n}\n")
    out = S._call("std_check", {"path": os.path.join(repo, "A.java")})
    mn = any(i.get("rule") == "magic_number"
             for i in json.loads(out[0].text).get("issues", []))
    with open(os.path.join(repo, "F.ps1"), "w", encoding="utf-8") as f:
        f.write("function Build-Module {\n    $speed = 444\n}\n")
    S._call("cb_index", {"path": repo})
    idx = json.load(open(os.path.join(repo, ".unified-rx-index", "index.json"),
                         encoding="utf-8"))
    ps = idx["files"].get("F.ps1", {}).get("symbols", {})
    if mn and "Build-Module" in ps:
        return True, "java magic_number + ps1 连字符符号 双检出"
    return False, f"应双检出: mn={mn} ps1={list(ps)}"


@probe("p27_bug_scan_rest_langs")
def p27():
    """bug_scan 剩余语言契约（271：swift/php/rb/kt/tsx——语言全覆盖收官）。"""
    cases = {
        "s.swift": ("print(\"x\")\nlet s: String? = nil\nprint(s!)\n", "debug_residue"),
        "p.php": ("<?php\neval($x);\n", "dynamic_exec"),
        "r.rb": ("def m\n  eval(\"1\")\nend\n", "dynamic_exec"),
        "k.kt": ("fun m() {\n    println(s!!)\n}\n", "nonnull_assert"),
        "t.tsx": ("const C = () => <div dangerouslySetInnerHTML={{__html: u}} />;\n", "xss_risk"),
    }
    for fn, (src, want) in cases.items():
        fp = os.path.join(_TMP, fn)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(src)
        out = S._call("bug_scan", {"path": fp})
        data = json.loads(out[0].text)
        rules = {i.get("rule") for i in data.get("issues", [])}
        if want not in rules:
            return False, f"{fn} 应检出 {want}: {rules}"
    return True, "swift/php/rb/kt/tsx 五语言规则全检出"


@probe("p28_bug_locate_multilang")
def p28():
    """bug_locate 多语言报错（272：Java/Go/C#/Swift 定位）。"""
    repo = os.path.join(_TMP, "bl272")
    os.makedirs(repo, exist_ok=True)
    p = os.path.join(repo, "App.java")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n" * 15)
    err = ("Exception in thread \"main\" java.lang.NullPointerException\n"
           "        at com.game.App.run(App.java:12)")
    out = S._call("bug_locate", {"error_text": err})
    data = json.loads(out[0].text)
    locs = data.get("locations", [])
    if data.get("matched") and locs and locs[0].get("line") == 12:
        return True, "Java 栈帧定位 L12"
    return False, f"Java 栈帧应定位: {data}"


@probe("p29_bug_scan_dart")
def p29():
    """bug_scan Dart（273：print/dynamic/as 强转）。"""
    dart_file = os.path.join(_TMP, "sample.dart")
    with open(dart_file, "w", encoding="utf-8") as f:
        f.write("void main() {\n"
                "  print('hi');\n"
                "  dynamic x = 1;\n"
                "  var s = obj as String;\n"
                "}\n")
    out = S._call("bug_scan", {"path": dart_file})
    data = json.loads(out[0].text)
    rules = {i.get("rule") for i in data.get("issues", [])}
    if {"debug_residue", "dynamic_abuse", "unsafe_cast"} <= rules:
        return True, f"dart 三规则检出: {sorted(rules)}"
    return False, f"dart 应三规则: {rules}"


@probe("p30_ui_check_dart_flutter")
def p30():
    """ui_check Flutter 死按钮（274）。"""
    dart_file = os.path.join(_TMP, "sample_flutter.dart")
    with open(dart_file, "w", encoding="utf-8") as f:
        f.write("class App extends StatelessWidget {\n"
                "  Widget build(BuildContext c) {\n"
                "    return TextButton(\n"
                "      child: Text('Start'),\n"
                "    );\n"
                "  }\n"
                "}\n")
    out = S._call("ui_check", {"path": dart_file})
    data = json.loads(out[0].text)
    ni = [i for i in data.get("issues", []) if i.get("rule") == "no_interaction"]
    if ni:
        return True, f"Flutter 死按钮检出 L{ni[0]['line']}"
    return False, f"Flutter 死按钮应检出: {data}"


@probe("p31_bug_locate_dart_uri")
def p31():
    """bug_locate dart file:/// URI 定位（273）。"""
    repo = os.path.join(_TMP, "dartloc")
    os.makedirs(repo, exist_ok=True)
    p = os.path.join(repo, "app2.dart")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n" * 10)
    err = "EXCEPTION: x\n#0 main (file:///u/app2.dart:7)"
    out = S._call("bug_locate", {"error_text": err})
    data = json.loads(out[0].text)
    locs = data.get("locations", [])
    if data.get("matched") and locs and locs[0].get("line") == 7 \
            and "file:" not in locs[0].get("file", ""):
        return True, "dart file:/// URI 定位 L7（前缀清洗）"
    return False, f"dart URI 应定位: {data}"


@probe("p32_cheatsheet_lang_domains")
def p32():
    """cmd_cheatsheet 多语言命令域（292）。"""
    out = S._call("cmd_cheatsheet", {})
    data = json.loads(out[0].text)
    doms = data.get("domains", [])
    missing = [d for d in ("lang_go", "lang_ts", "lang_cs", "lang_dart")
               if d not in doms]
    if not missing:
        return True, "4 语言命令域在 cheatsheet"
    return False, f"缺语言域: {missing}"


@probe("p33_bug_scan_bare_except")
def p33():
    """bug_scan py 裸 except（299）。"""
    py_file = os.path.join(_TMP, "sample_bare.py")
    with open(py_file, "w", encoding="utf-8") as f:
        f.write("def f():\n    try:\n        x = 1\n    except:\n        pass\n")
    out = S._call("bug_scan", {"path": py_file})
    data = json.loads(out[0].text)
    be = [i for i in data.get("issues", []) if i.get("rule") == "bare_except"]
    if be:
        return True, f"py 裸 except 检出 L{be[0]['line']}"
    return False, f"裸 except 应检出: {data}"
