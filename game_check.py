#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_check —— 引擎中立游戏工程检查（2026-08-14，skill 方法论 M1/M2/M5 落地）。

原理（来自 game-design skill 通读）：
- M1 实现不变量：设计意图 → 引擎中立不变量 → 代码校验
  （"failure mode is rule-level rather than API-level"——同一样例在
  Bevy/Godot/Unity 写法都命中）
- M2 表现寄存器：character/abstract/serious 三档先定寄存器再选效果
- M5 通用红线：每帧 IO/资源加载、帧率无关逻辑、物理参数数量级

全部规则引擎中立（不依赖具体引擎 API——按"每帧循环/输入事件/物理参数"
等跨引擎模式匹配）。规则数据驱动（可调）。
"""
import json
import os
import re

# ── 每帧循环入口（跨引擎：Bevy fn/Godot func/Unity void + _input 输入回调）──
_FRAME_LOOP_RE = re.compile(
    r"(?:fn|func)\s+(?:update|fixed_update|_process|_physics_process|_integrate_forces|_input)"
    r"|void\s+(?:Update|FixedUpdate)\s*\("
    r"|def\s+(?:_process|_physics_process|_integrate_forces|_input)"
)
# 输入事件处理（跨引擎）
_INPUT_RE = re.compile(
    r"(?:Input|on_|_input|OnPointer|HandleInput|event|just_pressed|"
    r"Input\.GetKey|Input\.GetButton|action_)"
)
# 冷却/节流检查（跨引擎：cooldown/timer/elapsed/since/next_）
_COOLDOWN_RE = re.compile(
    r"cooldown|timer|elapsed|time_since|next_(?:fire|attack|use)|"
    r"Time\.delta|delta\s*[<>]|_delta|dt\s*[<>]"
)
# IO/资源加载（跨引擎——含大写 API 变体：ReadAllText/WriteAllText、Godot FileAccess）
_FRAME_IO_RE = re.compile(
    r"File\.(?:open|read|write|Open|Read|Write|ReadAllText|WriteAllText)"
    r"|FileAccess\.(?:open|get_file_as|store)"
    r"|fs\.(?:open|read|write|Open|Read|Write)"
    r"|load\(|AssetServer|ResourceLoader|read_to_string|FileStream|StreamReader|"
    r"HttpClient|reqwest|ureq|std::fs"
)
# 帧率无关（delta 使用）
_DELTA_RE = re.compile(
    r"delta|dt\b|_dt\b|Time\.deltaTime|get_process_delta_time|frame_delta"
)
# 物理参数名（跨引擎——允许类型声明前缀与赋值符：`wheel_radius: f32 = 5000.0`、
# `spring_stiffness = 1e6`、`this.mass = 0.00001f`）
_PHYS_RE = re.compile(
    r"(?:wheel|suspension|radius|gravity|friction|mass|damping|spring)[a-z_]*"
    r"\s*[=:]\s*(?:[A-Za-z0-9_:<>\[\]]+\s*=\s*)?([-\d.]+(?:e[+-]?\d+)?)"
)

# ── M2 表现寄存器信号 ──
_REGISTER_CHARACTER = re.compile(
    r"slime|bug|mascot|creature|critter|puppy|kitten|blob|cute|可爱|宠物|萌"
)
_REGISTER_ABSTRACT = re.compile(
    r"shard|drone|block|node|neon|grid|puzzle|abstract|几何|霓虹|方块"
)
_REGISTER_SERIOUS = re.compile(
    r"survival|horror|realistic|tense|sim\b|scavenger|grim|dark|生存|恐怖|写实"
)


def check_game_invariants(src: str, path: str = "",
                          game_rules: dict | None = None) -> list[dict]:
    """M1+M5：引擎中立不变量/红线检查（单文件）。

    game_rules 可覆盖物理参数范围（项目级配置——通用默认 1e-3..1e4）。
    """
    gr = game_rules or {}
    _pr = gr.get("physics_range") or {}
    try:
        _pmin = float(_pr.get("min", 1e-3))
        _pmax = float(_pr.get("max", 1e4))
    except (TypeError, ValueError):
        _pmin, _pmax = 1e-3, 1e4
    issues: list[dict] = []
    lines = src.splitlines()
    is_test = "test" in os.path.basename(path).lower()
    in_frame = False
    for i, line in enumerate(lines, 1):
        s = line.strip()
        # 进入每帧循环体（粗粒度：GDScript 冒号结尾也算——引擎中立）
        if _FRAME_LOOP_RE.search(s) and ("{" in s or s.endswith(":")):
            in_frame = True
        # 每帧 IO/资源加载（M5 红线 1：帧内做 IO 卡顿）
        if in_frame and _FRAME_IO_RE.search(s) and not is_test:
            issues.append({"rule": "frame_io", "severity": "warning", "line": i,
                           "msg": "每帧循环内做 IO/资源加载（卡顿红线——"
                                  "建议预加载/异步）", "file": path})
        # 输入无冷却（M1：连打不变量——输入处理且窗口无冷却即报）
        if _INPUT_RE.search(s) and not _COOLDOWN_RE.search(
                "\n".join(lines[max(0, i - 6):i + 3])) \
                and not is_test:
            issues.append({"rule": "input_unthrottled", "severity": "info",
                           "line": i,
                           "msg": "输入处理无冷却/节流（连打可能优于精打——"
                                  "建议不变量：冷却/输入队列）", "file": path})
        # 物理参数数量级（M5 红线 2——范围可由项目 game_rules 覆盖）
        m = _PHYS_RE.search(s)
        if m:
            try:
                v = float(m.group(1).replace("_", ""))
                if v != 0 and (abs(v) > _pmax or abs(v) < _pmin):
                    issues.append({"rule": "physics_scale", "severity": "warning",
                                   "line": i,
                                   "msg": f"物理参数数量级异常（{m.group(1)}——"
                                          f"项目范围 {_pmin}..{_pmax}）",
                                   "file": path})
            except ValueError:
                pass
        # 帧率无关（M5 红线 3：循环内移动/计时无 delta——粗糙窗口提示）
        if in_frame and _FRAME_LOOP_RE.search(s) and "position" in s \
                and not _DELTA_RE.search(s):
            issues.append({"rule": "frame_rate_dependent", "severity": "info",
                           "line": i,
                           "msg": "每帧移动/计时未见 delta 缩放（帧率无关逻辑——"
                                  "高刷/低刷表现不一致）", "file": path})
        if s == "}":
            in_frame = False
    return issues


def judge_register(src: str, path: str = "") -> dict:
    """M2：表现寄存器判定（character/abstract/serious——代码信号推断）。

    引擎中立：按对象命名/调色板/氛围词匹配三档信号。
    """
    scores = {"character": 0, "abstract": 0, "serious": 0}
    signals: list[str] = []
    for name, rx in (("character", _REGISTER_CHARACTER),
                     ("abstract", _REGISTER_ABSTRACT),
                     ("serious", _REGISTER_SERIOUS)):
        for m in rx.finditer(src):
            scores[name] += 1
            signals.append(f"{name}:{m.group(0)}")
    total = sum(scores.values())
    if total == 0:
        return {"register": "unknown",
                "advice": "无寄存器信号——建议按项目 README/视觉方向说明判断，"
                          "不确定时选更克制的（motion-based juice 三档通用）",
                "signals": []}
    top = max(scores, key=scores.get)
    # 建议效果（skill 表：character=全套/abstract=tilt+trails/serious=克制）
    _LEAN = {
        "character": "全套手法可用（含 eyes/挤压拉伸——可爱画风）",
        "abstract": "倾斜/拖尾/粒子/光效（避免卡通挤压与眼睛）",
        "serious": "克制挤压+重量感倾斜+命中粒子+hit pause",
    }
    return {"register": top, "score": scores, "signals": signals[:10],
            "advice": f"寄存器={top}——{_LEAN[top]}"}


def check_project(path: str, rules: list | None = None) -> dict:
    """目录级入口（引擎中立检查聚合）。rules=None 全规则。

    消费项目级 game_rules.json（若存在——通用默认 + 项目覆盖）：
    {"engine": "bevy", "physics_range": {"min": 1e-3, "max": 1e4}}
    """
    only = set(rules or [])
    gr_path = os.path.join(path, "game_rules.json")
    gr: dict = {}
    try:
        with open(gr_path, encoding="utf-8") as _f:
            _d = json.load(_f)
            if isinstance(_d, dict):
                gr = _d
    except (OSError, ValueError):
        pass
    issues: list[dict] = []
    regs: dict[str, int] = {}
    exts = (".rs", ".gd", ".cs", ".ts", ".js", ".cpp", ".py")
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames
                       if d not in ("target", "node_modules", ".git",
                                    "release", "bin", ".godot")]
        for fn in filenames:
            if not fn.endswith(exts):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    src = f.read()
            except OSError:
                continue
            for iss in check_game_invariants(src, p, gr):
                if not only or iss["rule"] in only:
                    issues.append(iss)
            reg = judge_register(src, p)
            if reg["register"] != "unknown":
                regs[reg["register"]] = regs.get(reg["register"], 0) + 1
    return {"ok": True, "files": None, "issue_count": len(issues),
            "issues": issues[:200], "registers": regs,
            "game_rules": gr or None,
            "advice": (f"项目寄存器分布：{regs or '未检出'}"
                       "——效果建议先按主导寄存器选择")}


def verify_headless_setup(path: str) -> dict:
    """M4 可复现验证检查（skill running-headless-godot 原则落地）。

    检查项目是否具备可复现验证配置：smoke 脚本/项目本地 XDG/日志捕获。
    Godot：tools/smoke.sh + XDG 三件套 + logs/；Bevy：cargo test 冒烟。
    引擎中立：任何游戏项目都应有"无头可复现验证"入口。
    """
    checks: list[dict] = []
    ok = True
    godot = any(f.endswith(".gd") for _, _, fs in os.walk(path)
                for f in fs) or os.path.exists(os.path.join(path, "project.godot"))
    bevy = os.path.exists(os.path.join(path, "Cargo.toml"))
    if godot:
        smoke = os.path.join(path, "tools", "smoke.sh")
        logs = os.path.join(path, "logs")
        if not os.path.exists(smoke):
            ok = False
            checks.append({"item": "tools/smoke.sh", "pass": False,
                           "msg": "无 smoke 脚本（Godot 可复现验证入口——"
                                  "建议 tools/smoke.sh 模板）"})
        else:
            checks.append({"item": "tools/smoke.sh", "pass": True})
        if not os.path.isdir(logs):
            checks.append({"item": "logs/", "pass": False,
                           "msg": "无 logs 目录（headless 运行日志捕获——"
                                  "smoke 验证证据）"})
        else:
            checks.append({"item": "logs/", "pass": True})
    if bevy:
        checks.append({"item": "cargo test", "pass": True,
                       "msg": "Bevy 项目——cargo test 冒烟入口（headless 测试）"})
    if not godot and not bevy:
        ok = False
        checks.append({"item": "engine", "pass": False,
                       "msg": "未识别引擎（Godot project.godot / Bevy Cargo.toml）"})
    return {"ok": ok, "engine": "godot" if godot else ("bevy" if bevy else "unknown"),
            "checks": checks,
            "advice": "可复现验证原则（skill M4）：无头运行 + 项目本地状态 + "
                      "日志捕获——验证不靠猜"}


def load_game_rules(path: str) -> dict:
    """读项目 game_rules.json（通用默认 + 项目覆盖——在游戏文件里再搞一个）。"""
    gr_path = os.path.join(path, "game_rules.json")
    try:
        with open(gr_path, encoding="utf-8") as f:
            d = json.load(f)
            if not isinstance(d, dict):
                return {"ok": False, "error": "game_rules.json 须为 JSON 对象"}
            return {"ok": True, "path": gr_path, "rules": d}
    except FileNotFoundError:
        return {"ok": False, "error": f"无 game_rules.json（{gr_path}——"
                "可选：项目特殊规则文件）"}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"读取失败: {e}"}


def save_game_rules(path: str, rules: dict) -> dict:
    """写项目 game_rules.json（项目级规则——用户理念"在游戏文件里再搞一个"）。"""
    if not isinstance(rules, dict):
        return {"ok": False, "error": "rules 须为 JSON 对象"}
    gr_path = os.path.join(path, "game_rules.json")
    try:
        with open(gr_path, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
        return {"ok": True, "path": gr_path, "rules": rules}
    except OSError as e:
        return {"ok": False, "error": f"写入失败: {e}"}
    only = set(rules or [])
    # 项目级规则（阶段4b：在游戏文件里再搞一个——通用默认 + 项目覆盖）
    gr_path = os.path.join(path, "game_rules.json")
    gr: dict = {}
    try:
        with open(gr_path, encoding="utf-8") as _f:
            _d = json.load(_f)
            if isinstance(_d, dict):
                gr = _d
    except (OSError, ValueError):
        pass
    issues: list[dict] = []
    regs: dict[str, int] = {}
    exts = (".rs", ".gd", ".cs", ".ts", ".js", ".cpp", ".py")
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames
                       if d not in ("target", "node_modules", ".git",
                                    "release", "bin", ".godot")]
        for fn in filenames:
            if not fn.endswith(exts):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    src = f.read()
            except OSError:
                continue
            for iss in check_game_invariants(src, p, gr):
                if not only or iss["rule"] in only:
                    issues.append(iss)
            reg = judge_register(src, p)
            if reg["register"] != "unknown":
                regs[reg["register"]] = regs.get(reg["register"], 0) + 1
    return {"ok": True, "files": None, "issue_count": len(issues),
            "issues": issues[:200], "registers": regs,
            "game_rules": gr or None,
            "advice": (f"项目寄存器分布：{regs or '未检出'}"
                       "——效果建议先按主导寄存器选择")}
