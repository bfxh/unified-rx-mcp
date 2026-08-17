from __future__ import annotations
import sys as _sys
for _m in ['patch_learn', 'differentiable_code', 'explore_engine', 'distill_pipeline', 'quality_engine', 'failure_analyze', 'mini_bert_tokenizer', 'replay_core']:
    _sys.modules.setdefault(_m, _sys.modules[__name__])



"""learn_engine — 学习引擎（合并自 8 个学习模块：补丁学习/可微分/探索/蒸馏/质量/失败分析/分词/回放）。
新技术 = 往本模块增量加函数（不新建零散文件）。
"""

# ══════════════ patch_learn（合并） ══════════════
# -*- coding: utf-8 -*-
"""patch_learn：从漏洞修复 diff 提取模式 → 生成检测规则。

KNighter（SOSP 2025）概念轻量版：让"补丁学习"产出确定性规则——
本实现从标准 diff 的 `-` 行（修复前漏洞代码）提取含危险 sink 的行，
正则化（字面量/变量→\\w+，sink 调用名保留）→ 输出可直接加入
vuln_rules.json 的规则建议。零依赖、确定性、不依赖 LLM。
"""

import re


_SINKS = ("execute", "query", "eval", "system", "Popen", "open",
          "loads", "urlopen", "render", "join", "subprocess")


def patch_learn(diff_text: str, language: str = ".py") -> dict:
    """从修复 diff 提取漏洞模式 → 规则建议。

    diff 输入：标准统一 diff（`-` 行 = 修复前漏洞代码）。
    返回 {ok, extracted, rules: [{pattern, suggested_msg, source_line}]}。
    """
    if not diff_text or len(diff_text) > 200_000:
        return {"ok": False, "error": "diff 需 1..200000 字符"}
    removed = []
    for line in diff_text.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            body = line[1:].strip()
            if any(s in body for s in _SINKS):
                removed.append(body)
    if not removed:
        return {"ok": False,
                "error": "diff 中未找到含危险 sink 的删除行（无规则可提取）"}
    rules = []
    for body in removed[:8]:
        rx = _regularize(body)
        rules.append({
            "pattern": rx,
            "suggested_msg": f"疑似漏洞模式（从修复 diff 学得）：{body[:60]}",
            "source_line": body[:80],
        })
    return {"ok": True, "extracted": len(removed), "rules": rules,
            "advice": "规则可加入 vuln_rules.json（id/pattern/language/"
                      "severity/msg）——确定性规则规模化（KNighter 概念）"}


def _regularize(body: str) -> str:
    """正则化：字符串/数字→占位；sink 名保留；其余变量→\\w+；转义特殊字符。

    结果可直接 re.compile——漏洞代码命中、修复代码（参数化/安全写法）
    不命中（sink 调用结构保留 + 字面量占位）。
    """
    rx = re.sub(r"'[^']*'|\"[^\"]*\"", "@@STR@@", body)
    # 数字直接占位（security-review MEDIUM：先替换为 @@D@@——避免
    # 中间态 \d+ 的 d 被后续标识符替换破坏）
    rx = re.sub(r"\b\d+\b", "@@D@@", rx)
    keep = "|".join(_SINKS)
    rx = re.sub(r"\b(?!(?:" + keep + r")\b)[a-z_]\w*\b", r"\\w+", rx)
    # 占位保护量词（\w+ 整体——防 + 被误转义为字面）
    rx = rx.replace(r"\w+", "@@W@@")
    # 特殊字符转义（先保护占位符反斜杠；@@STR@@/@@D@@ 无特殊字符）
    rx = rx.replace("\\", "@@BS@@")
    for ch in "().+*?[]{}|^$":
        rx = rx.replace(ch, "\\" + ch)
    rx = rx.replace("@@BS@@", "\\")
    rx = rx.replace("@@W@@", r"\w+").replace("@@D@@", r"\d+")
    # 字符串占位最后恢复（字符类括号不被转义）
    rx = rx.replace("@@STR@@", r"['\"][^'\"]*['\"]")
    return rx[:120]


# ══════════════ differentiable_code（合并） ══════════════
# -*- coding: utf-8 -*-
"""differentiable_code —— 可微分编程环境（2026-08-15，阶段2 务实落地）。

科幻愿景：代码结构嵌入连续向量空间 → 性能目标梯度下降 → 自动重写算法。
务实落地（工程正确性优先——不交付假的梯度下降）：
① AST 符号嵌入：函数/符号 → 向量（mini_bert_tokenizer 或哈希嵌入）——
   相似函数可检索（向量空间是"可训练"的基础设施）
② optimize_code：性能目标驱动优化器——复杂度分析 + 热点定位 +
   等价重写建议（规则/搜索驱动——诚实标注"梯度下降为未来方向"）
③ performance_target：目标声明 → 检查建议（响应时间/内存/复杂度目标
   映射到代码检查）

调整的是"损失函数（性能目标）"而非代码行——目标驱动优化。
"""
import ast
import hashlib
import os

# 引擎根（合并后 __file__ 在 engine/ 下——数据文件在仓库根）
_ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))



# ── ① AST 符号嵌入（函数签名向量——符号级可检索）──────────
def embed_function(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    """函数符号 → 特征向量（哈希嵌入——零依赖，确定性）。

    特征：名称/参数数/返回标注/装饰器/体内调用名——结构特征向量。
    真·embedding（mini_bert_tokenizer）可升级接入——接口不变。
    """
    name = func_node.name
    args = func_node.args.args
    decorators = [ast.unparse(d) for d in func_node.decorator_list]
    calls = [n.func.id for n in ast.walk(func_node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    features = {
        "name": name, "arg_count": len(args),
        "decorators": decorators, "calls": calls[:10],
        "has_docstring": bool(ast.get_docstring(func_node)),
        "stmt_count": len(func_node.body),
    }
    # 哈希嵌入（确定性 8 维桶）
    vec = []
    for key in ("name", "arg_count", "has_docstring", "stmt_count",
                "calls", "decorators"):
        h = hashlib.sha256(f"{key}:{features[key]}".encode("utf-8"))
        vec.append(int(h.hexdigest()[:4], 16) % 1024)
    return {"features": features, "vector": vec,
            "embedding": "hash-bucket(8d)——可替换 mini_bert 真嵌入"}


def similar_functions(src: str, target: str, limit: int = 5) -> dict:
    """AST 相似函数检索：解析两个源码 → 全部函数嵌入 → 余弦相似度 TopN。"""
    def _collect(code: str) -> list[tuple[str, dict]]:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []
        out = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((n.name, embed_function(n)))
        return out

    src_fns = _collect(src)
    tgt_fns = _collect(target)
    if not src_fns or not tgt_fns:
        return {"ok": False, "error": "无函数可比较（语法错误或空）"}
    results = []
    for t_name, t_emb in tgt_fns:
        tv = t_emb["vector"]
        for s_name, s_emb in src_fns:
            sv = s_emb["vector"]
            sim = _cosine(tv, sv)
            results.append({"target": t_name, "source": s_name,
                            "similarity": round(sim, 3)})
    results.sort(key=lambda r: -r["similarity"])
    return {"ok": True, "top": results[:limit],
            "note": "结构特征向量相似度——真·语义相似需嵌入模型（mini_bert 可替换）"}


def _cosine(a: list[int], b: list[int]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── ② 性能目标驱动优化器 ──────────────────────────────────
def optimize_code(src: str, path: str = "",
                  perf_goal: str = "响应时间<10ms") -> dict:
    """性能目标 → 复杂度分析 + 热点定位 + 等价重写建议。

    规则/搜索驱动（诚实标注：真·梯度下降重写为未来方向——本实现给出
    可执行的优化候选，由调用方确认应用）。
    """
    issues: list[dict] = []
    lines = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {"ok": False, "error": f"语法错误: {e}"}
    # 复杂度热点（嵌套循环/递归/大分配）
    for n in ast.walk(tree):
        if isinstance(n, ast.For) or isinstance(n, ast.While):
            depth = _loop_depth(n)
            if depth >= 2:
                issues.append({"kind": "complexity",
                               "line": n.lineno,
                               "msg": f"嵌套循环深度 {depth}——O(n^{depth}) 风险，"
                                      f"与目标 {perf_goal} 冲突",
                               "fix": "考虑提前退出/缓存/索引（等价重写）"})
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in ("sorted", "list.sort"):
            # 排序调用——数据量大时建议
            issues.append({"kind": "hotspot", "line": n.lineno,
                           "msg": "排序调用——若每次请求都排序，O(n log n) 热点",
                           "fix": "预排序/增量维护有序结构"})
    # 目标检查：响应时间目标 → 同步 IO 检测
    if "ms" in perf_goal or "响应" in perf_goal:
        for i, line in enumerate(lines, 1):
            if re.search(r"\b(open|read|requests|urlopen|connect)\s*\(", line):
                issues.append({"kind": "io_in_hot_path", "line": i,
                               "msg": f"同步 IO 在热点路径（与 {perf_goal} 冲突）",
                               "fix": "异步/缓存/批处理"})
    # 内存目标 → 大容器
    if "内存" in perf_goal or "memory" in perf_goal.lower():
        for n in ast.walk(tree):
            if isinstance(n, ast.ListComp) and len(n.generators) >= 2:
                issues.append({"kind": "memory", "line": n.lineno,
                               "msg": "嵌套推导式——大输入内存峰值",
                               "fix": "生成器表达式/流式处理"})
    return {"ok": True, "path": path, "perf_goal": perf_goal,
            "findings": issues[:15], "count": len(issues),
            "rewrites": _gen_rewrites(issues),
            "note": "规则/搜索驱动优化建议 + 等价重写片段（可直接应用）——"
                    "'损失函数'=性能目标；真·AST 梯度下降重写为未来方向"}


def _gen_rewrites(findings: list[dict]) -> list[dict]:
    """等价重写片段生成（风险解决 2026-08-15——梯度下降的可工作简化版）。

    对每个 hotspot finding 生成可直接粘贴的优化代码（规则模板——
    不改变语义的等价重写）。
    """
    out = []
    for f in findings:
        if f["kind"] == "io_in_hot_path":
            out.append({"kind": "io_in_hot_path", "line": f["line"],
                        "rewrite": ("# 优化：IO 移出热点路径——预加载 + 缓存\n"
                                    "import functools\n"
                                    "@functools.lru_cache(maxsize=128)\n"
                                    "def _cached_load(path):\n"
                                    "    with open(path) as fh:\n"
                                    "        return fh.read()\n")})
        elif f["kind"] == "complexity":
            out.append({"kind": "complexity", "line": f["line"],
                        "rewrite": ("# 优化：嵌套循环 → 提前退出/索引（等价语义）\n"
                                    "# 内层循环前加卫语句：if not cond: break/continue\n"
                                    "# 或用 dict 索引替代线性查找（O(n²)→O(n)）\n")})
        elif f["kind"] == "memory":
            out.append({"kind": "memory", "line": f["line"],
                        "rewrite": ("# 优化：嵌套推导式 → 生成器（惰性求值降内存峰值）\n"
                                    "# (expr for a in A for b in B) 替代 [expr for a in A for b in B]\n")})
    return out[:5]


def _loop_depth(node) -> int:
    """循环嵌套深度（ast.walk 自身含子循环——统计最大嵌套）。"""
    depth = 1
    for sub in ast.walk(node):
        if sub is node:
            continue
        if isinstance(sub, (ast.For, ast.While)):
            depth = max(depth, _loop_depth(sub) + 1)
    return depth


# ── ③ 性能目标声明 → 检查建议 ─────────────────────────────
def check_perf_target(root: str, perf_goal: str) -> dict:
    """项目级性能目标检查：目录下所有 py 文件跑 optimize_code（聚合）。"""
    findings = []
    files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ("node_modules", "target", ".git",
                                    "__pycache__")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    src = f.read()
            except OSError:
                continue
            files += 1
            r = optimize_code(src, p, perf_goal)
            if r.get("ok") and r["findings"]:
                for f in r["findings"]:
                    f["file"] = p
                findings.extend(r["findings"])
    return {"ok": True, "files": files, "perf_goal": perf_goal,
            "findings": findings[:100], "count": len(findings),
            "advice": f"性能目标 {perf_goal}——{len(findings)} 处优化点"
                      "（规则驱动——确认后应用）"}


# ══════════════ explore_engine（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""explore_engine.py — P4 探索引擎：LATS 值函数引导树搜索（抄 ICML'24 LATS + 旧 UCB）。

核心（对应 TOP_TIER_PLAN ④）：
  - 选择：UCT = Q + c·√(ln N / n)（继承旧 UCB）
  - 扩展：候选动作生成（外部注入，如 bug 定位候选）
  - 模拟：值函数评估（LocalIntel.value 或注入的 scorer；无值函数降级 UCB 纯探索）
  - 回溯：delta 奖励更新（复用 lse_client.ucb_backprop 语义）

与 bug_locate 的关系：bug_locate 已用 lse-engine UCB；本引擎是通用版 +
值函数引导（value_fn 可用时 UCT 加 value 项），供多轮因果探索使用。

用法：
  eng = ExploreEngine(value_fn=LocalIntel().value)   # value_fn 可 None（纯 UCB）
  eng.search(root, candidates, expand_fn, evaluate_fn, budget=20)
"""
import math
import random
import time
from dataclasses import dataclass, field


@dataclass
class _Node:
    id: str
    parent: "_Node | None" = None
    children: list["_Node"] = field(default_factory=list)
    visits: int = 0
    reward_sum: float = 0.0
    value_est: float | None = None  # 值函数估计（LATS 特点）

    @property
    def q(self) -> float:
        """平均奖励（Q 值）。"""
        return self.reward_sum / self.visits if self.visits else 0.0

    def uct(self, c: float = 1.41, value_weight: float = 0.3) -> float:
        """UCT 分数（含值函数引导项）。

        UCT = Q + c·√(ln N / n) + value_weight·V(s)
        前两项继承旧 UCB；第三项是 LATS 的值函数引导。
        """
        if self.visits == 0:
            return float("inf")  # 未访问优先（探索）
        explore = c * math.sqrt(math.log(max(1, self.parent.visits)) / self.visits)
        value_bonus = value_weight * (self.value_est or 0.0)
        return self.q + explore + value_bonus


class ExploreEngine:
    """LATS 值函数引导树搜索（可降级纯 UCB）。"""

    def __init__(self, value_fn=None, c: float = 1.41,
                 value_weight: float = 0.3, seed: int | None = None):
        """
        value_fn:    状态值函数 (state_id: str) -> float | None（None=无值函数，纯 UCB）
        c:           UCB 探索常数
        value_weight: 值函数引导权重（0=纯 UCB）
        """
        self._value_fn = value_fn
        self._c = c
        self._value_weight = value_weight
        self._rng = random.Random(seed)
        self._nodes: dict[str, _Node] = {}

    # ── 主入口 ────────────────────────────────────────────
    def search(self, root_id: str, candidates: list[str],
               expand_fn=None, evaluate_fn=None,
               budget: int = 20, max_depth: int = 4) -> dict:
        """树搜索主循环。

        root_id:     根状态 id
        candidates:  初始候选动作（树的第一层分支）
        expand_fn:   (state_id) -> list[str] 子候选（None=只用初始候选）
        evaluate_fn: (state_id) -> float 真实奖励（None=用值函数估计）
        budget:      总模拟预算
        max_depth:   最大深度
        返回 {best, tree_size, stats}
        """
        t0 = time.perf_counter()
        root = _Node(id=root_id)
        root.value_est = self._call_value(root_id)
        self._nodes[root_id] = root
        # 第一层：候选即根的子节点
        for cand in candidates:
            child = _Node(id=cand, parent=root)
            child.value_est = self._call_value(cand)
            root.children.append(child)
            self._nodes[cand] = child

        stats = {"simulations": 0, "expanded": 0, "depth_reached": 0,
                 "ms": 0.0}
        for _ in range(budget):
            # 1. 选择（UCT 下降）
            path = self._select(root, max_depth)
            leaf = path[-1]
            stats["depth_reached"] = max(stats["depth_reached"], len(path) - 1)
            # 2. 扩展（若可扩展且有预算）
            if expand_fn and len(path) < max_depth:
                subs = expand_fn(leaf.id) or []
                stats["expanded"] += len(subs)
                for s in subs:
                    if s not in self._nodes:
                        n = _Node(id=s, parent=leaf)
                        n.value_est = self._call_value(s)
                        leaf.children.append(n)
                        self._nodes[s] = n
            # 3. 模拟（评估）
            if leaf.children:
                leaf = self._rng.choice(leaf.children)
            reward = evaluate_fn(leaf.id) if evaluate_fn else (leaf.value_est or 0.0)
            # 4. 回溯（沿路径更新）
            self._backprop(path + [leaf] if leaf not in path else path, reward)
            stats["simulations"] += 1
        stats["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        best = self._best_child(root)
        return {"best": best.id if best else root_id,
                "best_q": round(best.q, 4) if best else 0.0,
                "tree_size": len(self._nodes),
                "root": root_id, "stats": stats,
                "top": [{"id": ch.id, "q": round(ch.q, 4),
                         "visits": ch.visits, "value": ch.value_est}
                        for ch in sorted(root.children,
                                         key=lambda x: x.q, reverse=True)[:5]]}

    # ── 内部 ──────────────────────────────────────────────
    def _select(self, root: _Node, max_depth: int) -> list[_Node]:
        """UCT 选择路径（下降到叶子/深度上限）。"""
        path = [root]
        cur = root
        while cur.children and len(path) < max_depth:
            cur = max(cur.children, key=lambda n: n.uct(self._c, self._value_weight))
            path.append(cur)
        return path

    def _backprop(self, path: list[_Node], reward: float) -> None:
        """奖励沿路径回溯。"""
        for n in reversed(path):
            n.visits += 1
            n.reward_sum += reward

    def _best_child(self, root: _Node) -> _Node | None:
        """最高 Q 的子节点（最终选择）。"""
        if not root.children:
            return None
        return max(root.children, key=lambda n: n.q)

    def _call_value(self, state_id: str) -> float | None:
        """值函数调用（异常/不可用 → None）。"""
        if self._value_fn is None:
            return None
        try:
            return self._value_fn(state_id)
        except Exception:
            return None

# ── 中英同义词表（2026-08-15 从 server.py 拆出——_tool_explore_code 纯逻辑）──
_SYN = {
    "车轮": "wheel", "驱动": "drive", "物理": "physic", "地形": "terrain",
    "粒子": "particle", "光影": "light", "材质": "material", "模块": "module",
    "放置": "place", "拾取": "pickup", "载具": "vehicle", "燃料": "fuel",
    "碰撞": "collision", "相机": "camera", "输入": "input", "渲染": "render",
    "任务": "quest", "任务目标": "task", "血量": "health", "伤害": "damage",
    "存储": "storage", "缓存": "cache", "索引": "index", "搜索": "search",
    # IDE 增强 268：cs/lua/sh 代码词（游戏脚本/CI 探索）
    "类": "class", "函数": "function", "方法": "method", "脚本": "script",
    "按钮": "button", "点击": "click", "构建": "build", "部署": "deploy",
    # IDE 增强 293：移动端/桌面词（安卓/苹果/窗口/控件树）
    "安卓": "android", "苹果": "ios", "窗口": "window", "控件树": "widget",
    # IDE 增强 299：AI/数据词（模型/训练/推理/数据集/特征）
    "模型": "model", "训练": "train", "推理": "inference", "数据集": "dataset",
    # IDE 增强 300：测试/工程词（断言/模拟/覆盖率/基准）
    "测试": "test", "断言": "assert", "模拟": "mock", "覆盖率": "coverage",
    "基准": "benchmark", "调试": "debug", "日志": "log", "性能": "performance",
    "特征": "feature", "权重": "weight", "损失": "loss", "梯度": "gradient",
    # IDE 增强 305：安全词（权限/认证/加密/令牌/密钥——中文目标直达安全代码）
    "权限": "permission", "认证": "auth", "加密": "encrypt", "令牌": "token",
    "密钥": "secret", "签名": "signature", "校验": "verify", "防火墙": "firewall",
    # IDE 增强 306：网络/协议词（请求/响应/连接/同步——中文目标直达网络代码）
    "网络": "network", "请求": "request", "响应": "response", "连接": "connection",
    # IDE 增强 307：并发/性能词（线程/锁/队列/缓存——中文目标直达并发代码）
    "线程": "thread", "锁": "lock", "队列": "queue", "进程": "process",
    # IDE 增强 308：算法/结构词（哈希/图/树/排序/递归——中文目标直达算法代码）
    "哈希": "hash", "图": "graph", "树": "tree", "排序": "sort",
    # IDE 增强 309：业务词（订单/支付/用户/账户/库存/价格）
    "订单": "order", "支付": "payment", "用户": "user", "账户": "account",
    # IDE 增强 310：游戏资源词（声音/音乐/纹理/模型/特效/音效）
    "声音": "sound", "音乐": "music", "纹理": "texture", "模型": "model",
    # IDE 增强 311：UI 状态词（加载/错误/成功/空态/重试/取消）
    "加载": "loading", "错误": "error", "成功": "success", "空态": "empty",
    # IDE 增强 312：运维词（监控/指标/告警/健康检查/降级/熔断）
    "监控": "monitor", "指标": "metric", "告警": "alert", "健康检查": "health",
    # IDE 增强 315：时间/调度词（日期/时间/定时/调度/时区/延迟）
    "日期": "date", "时间": "time", "定时": "schedule", "调度": "cron",
    # IDE 增强 318：格式/编解码词（解析/序列化/编码/解码/压缩）
    "解析": "parse", "序列化": "serialize", "编码": "encode", "解码": "decode",
    # IDE 增强 319：文件/IO 词（文件/路径/目录/上传/下载/读写）
    "文件": "file", "路径": "path", "目录": "dir", "上传": "upload",
    # IDE 增强 320：配置词（配置/设置/选项/参数/默认值）
    "配置": "config", "设置": "setting", "选项": "option", "参数": "param",
    # IDE 增强 321：统计词（统计/聚合/分布/平均/方差/样本）
    "统计": "stats", "聚合": "aggregate", "分布": "distribution", "平均": "average",
    # IDE 增强 322：绘图词（线条/形状/画笔/画布/填充/描边）
    "线条": "line", "形状": "shape", "画笔": "brush", "画布": "canvas",
    # IDE 增强 323：通信词（消息/事件/广播/订阅/通知/回调）
    "消息": "message", "事件": "event", "广播": "broadcast", "订阅": "subscribe",
    # IDE 增强 324：渲染词（光照/着色器/景深/抗锯齿/遮挡/阴影）
    "光照": "light", "着色器": "shader", "景深": "depth", "抗锯齿": "antialias",
    # IDE 增强 325：物理词（重力/摩擦/速度/加速度/扭矩/阻尼）
    "重力": "gravity", "摩擦": "friction", "速度": "velocity", "加速度": "accel",
    # IDE 增强 326：音视频词（视频/播放/流媒体/录制/音频流）
    "视频": "video", "播放": "play", "流媒体": "stream", "录制": "record",
    # IDE 增强 327：UI 组件词（对话框/弹窗/菜单/标签页/工具提示）
    "对话框": "dialog", "弹窗": "modal", "菜单": "menu", "标签页": "tab",
    # IDE 增强 328：数据词（数据库/表/行/列/事务/主键/索引/查询）
    "数据库": "database", "表": "table", "行": "row", "列": "column",
    # IDE 增强 329：文本词（文本/字符串/字符/正则/分词/截取）
    "文本": "text", "字符串": "string", "字符": "char", "正则": "regex",
    # IDE 增强 330：输入词（键盘/鼠标/手柄/摇杆/按键/快捷键）
    "键盘": "keyboard", "鼠标": "mouse", "手柄": "gamepad", "摇杆": "joystick",
    # IDE 增强 331：地图词（地图/坐标/方向/寻路/路径寻找/视野）
    "地图": "map", "坐标": "coord", "方向": "direction", "寻路": "path",
    # IDE 增强 332：布局词（对齐/间距/边距/留白/溢出/换行）
    "对齐": "align", "间距": "spacing", "边距": "margin", "留白": "padding",
    # IDE 增强 333：存档词（存档/读档/检查点/进度/世界生成）
    "存档": "save", "读档": "load", "检查点": "checkpoint", "进度": "progress",
    # IDE 增强 334：特效词（爆炸/冲击/震荡/火/烟/碎片/闪电/毒）
    "爆炸": "explosion", "冲击": "impact", "震荡": "shock", "火": "fire",
    # IDE 增强 335：资源管理词（卸载/池化/回收/引用计数/预加载）
    "卸载": "unload", "池化": "pool", "回收": "recycle", "引用计数": "refcount",
    # IDE 增强 336：物品词（背包/装备/武器/防具/道具/材料）
    "背包": "inventory", "装备": "equip", "武器": "weapon", "防具": "armor",
    # IDE 增强 337：战斗词（攻击/防御/生命/魔法/技能/伤害/暴击/护盾）
    "攻击": "attack", "防御": "defense", "生命": "health", "魔法": "mana",
    # IDE 增强 338：地形词（地形/山脉/河流/森林/沙漠/洞穴/高原/沼泽）
    "地形": "terrain", "山脉": "mountain", "河流": "river", "森林": "forest",
    # IDE 增强 339：天气词（天气/下雨/下雪/风暴/昼夜/光照/雾/风）
    "天气": "weather", "下雨": "rain", "下雪": "snow", "风暴": "storm",
    # IDE 增强 340：生物词（动物/怪物/鸟/鱼/野兽/NPC/宠物/坐骑）
    "动物": "animal", "怪物": "monster", "鸟": "bird", "鱼": "fish",
    # IDE 增强 341：建筑词（建筑/房子/城堡/村庄/桥梁/道路/塔/城墙）
    "建筑": "building", "房子": "house", "城堡": "castle", "村庄": "village",
    # IDE 增强 342：任务词（任务/支线/主线/委托/奖励/成就/目标）
    "任务": "quest", "支线": "side", "主线": "mainline", "委托": "commission",
    # IDE 增强 343：贸易词（贸易/交易/金币/商店/价格/商人/货币/买卖）
    "贸易": "trade", "交易": "barter", "金币": "coin", "商店": "shop",
    # IDE 增强 344：社会词（势力/阵营/声望/友好/敌对/联盟/关系/招募）
    "势力": "faction", "阵营": "alignment", "声望": "reputation", "友好": "friendly",
    # IDE 增强 345：剧情词（剧情/对话/台词/演出/过场/叙事/剧本/镜头）
    "剧情": "story", "对话": "dialogue", "台词": "line", "演出": "scene",
    # IDE 增强 346：系统词（系统/内核/引擎/插件/框架/运行时/驱动/接口）
    "系统": "system", "内核": "kernel", "引擎": "engine", "插件": "plugin",
    # IDE 增强 347：环境词（环境/生态/生物群系/季节/温度/湿度/气压）
    "环境": "environment", "生态": "ecosystem", "生物群系": "biome", "季节": "season",
    # IDE 增强 348：矿脉词（矿脉/矿石/采矿/采集/收获/种植/木材/水源）
    "矿脉": "vein", "矿石": "ore", "采矿": "mine", "采集": "gather",
    # IDE 增强 349：工具词（工具/锤子/斧头/镐/铲/绳索/扳手/焊枪）
    "工具": "tool", "锤子": "hammer", "斧头": "axe", "镐": "pickaxe",
    # IDE 增强 350：载具词（载具/车辆/坦克/飞船/船/潜艇/飞行器/机甲）
    "载具": "vehicle", "车辆": "car", "坦克": "tank", "飞船": "spaceship",
    # IDE 增强 351：生存词（生存/饥饿/口渴/体温/睡眠/精力/疲惫/伤口）
    "生存": "survival", "饥饿": "hunger", "口渴": "thirst", "体温": "temperature",
    # IDE 增强 352：界面词（面板/侧边栏/状态栏/标题栏/页脚/标签栏/通知/提示）
    "面板": "panel", "侧边栏": "sidebar", "状态栏": "statusbar", "标题栏": "titlebar",
    # IDE 增强 353：科技词（科技/研究/解锁/蓝图/发明/升级/科技树/实验）
    "科技": "tech", "研究": "research", "解锁": "unlock", "蓝图": "blueprint",
    # IDE 增强 354：音乐词（音乐/旋律/节奏/和弦/音调/节拍/音符/音效库）
    "音乐": "music", "旋律": "melody", "节奏": "rhythm", "和弦": "chord",
    # IDE 增强 355：农业词（农业/农田/作物/播种/灌溉/施肥/畜牧/温室）
    "农业": "farm", "农田": "field", "作物": "crop", "播种": "sow",
    # IDE 增强 356：潜水词（潜水/水下/呼吸/水压/游泳/浮力/潜水艇/水肺）
    "潜水": "dive", "水下": "underwater", "呼吸": "breathe", "水压": "pressure",
    # IDE 增强 357：战役词（战役/战争/战场/征服/占领/入侵/围攻/哨塔）
    "战役": "battle", "战争": "war", "战场": "battlefield", "征服": "conquer",
    # IDE 增强 358：飞行词（起飞/降落/滑翔/盘旋/升空/俯冲/急升/盘旋）
    "起飞": "takeoff", "降落": "landing", "滑翔": "glide", "盘旋": "hover",
    # IDE 增强 359：工业词（工业/工厂/冶炼/精炼/加工/流水线/发电机/能源）
    "工业": "industry", "工厂": "factory", "冶炼": "smelt", "精炼": "refine",
    # IDE 增强 360：车辆配件词（轮胎/底盘/悬挂/刹车/燃料/方向盘/传动/油箱）
    "轮胎": "tire", "底盘": "chassis", "悬挂": "suspension", "刹车": "brake",
    # IDE 增强 361：材料词（钢材/玻璃/合金/陶瓷/布料/皮革/混凝土/塑料）
    "钢材": "steel", "玻璃": "glass", "合金": "alloy", "陶瓷": "ceramic",
    # IDE 增强 362：设施词（实验室/反应堆/离心机/装配机/熔炉/钻机/雷达/天线）
    "实验室": "lab", "反应堆": "reactor", "离心机": "centrifuge", "装配机": "assembler",
    # IDE 增强 363：物流词（传送带/输送机/物流/码头/仓库/货架/叉车/装卸）
    "传送带": "conveyor", "输送机": "transport", "物流": "logistics", "码头": "dock",
    # IDE 增强 364：电力词（电力/电线/电缆/变压器/电池/充电/电网/断电）
    "电力": "power", "电线": "wire", "电缆": "cable", "变压器": "transformer",
    # IDE 增强 365：流体词（液体/气体/流体/管道/泵/阀门/蒸汽/液压）
    "液体": "liquid", "气体": "gas", "流体": "fluid", "管道": "pipe",
    # IDE 增强 366：矿产词（金矿/铁矿/铜矿/煤矿/铀矿/稀土/硅矿/石油）
    "金矿": "gold", "铁矿": "iron", "铜矿": "copper", "煤矿": "coal",
    # IDE 增强 367：农机词（拖拉机/收割机/播种机/无人机/翻耕机/喷灌机）
    "拖拉机": "tractor", "收割机": "harvester", "播种机": "seeder", "无人机": "drone",
    # IDE 增强 368：太空词（空间站/轨道/行星/星系/恒星/卫星/探测器/陨石）
    "空间站": "station", "轨道": "orbit", "行星": "planet", "星系": "galaxy",
    # IDE 增强 369：配方词（配方/炼金/附魔/强化/改造/修理/分解/组装）
    "配方": "recipe", "炼金": "alchemy", "附魔": "enchant", "强化": "enhance",
    # IDE 增强 370：装备词（弹药/枪械/剑/盾牌/弓箭/弩/法杖/锤）
    "弹药": "ammo", "枪械": "gun", "剑": "sword", "盾牌": "shield",
    # IDE 增强 371：防御词（防御塔/炮台/陷阱/地雷/哨戒/碉堡/路障/铁丝网）
    "防御塔": "tower", "炮台": "turret", "陷阱": "trap", "地雷": "mine",
    # IDE 增强 372：存储词（货箱/容器/箱子/柜子/冰箱/冷冻/货架已有货架/桶）
    "货箱": "crate", "容器": "container", "箱子": "chest", "柜子": "cabinet",
    # IDE 增强 373：交通词（铁路/火车/地铁/车站/信号灯/高架/隧道/路口）
    "铁路": "railway", "火车": "train", "地铁": "subway", "车站": "station",
    # IDE 增强 374：饰品词（项链/戒指/护符/手镯/腰带/徽章/耳环/头饰）
    "项链": "necklace", "戒指": "ring", "护符": "charm", "手镯": "bracelet",
    # IDE 增强 375：染料词（染料/颜料/染色/调色/油漆/涂料/喷漆/上色）
    "染料": "dye", "颜料": "pigment", "染色": "color", "调色": "palette",
    # IDE 增强 376：烹饪词（烹饪/炉灶/烤箱/锅/煎/烤/炖/烘焙）
    "烹饪": "cook", "炉灶": "stove", "烤箱": "oven", "锅": "pot",
    # IDE 增强 377：建材词（砖块/木板/石材/屋顶/地基/墙板/横梁/脚手架）
    "砖块": "brick", "木板": "plank", "石材": "stone", "屋顶": "roof",
    # IDE 增强 378：建筑内部词（楼层/走廊/楼梯/门/窗/房间/天窗/壁炉）
    "楼层": "floor", "走廊": "corridor", "楼梯": "stairs", "门": "door",
    # IDE 增强 379：矿业机器词（矿机/挖掘机/洗矿机/分拣机/压缩机/磨矿机）
    "矿机": "miner", "挖掘机": "excavator", "洗矿机": "washer", "分拣机": "sorter",
    # IDE 增强 380：食物词（面包/肉/蔬菜/水果/蘑菇/鱼干/蜂蜜/奶酪）
    "面包": "bread", "肉": "meat", "蔬菜": "vegetable", "水果": "fruit",
    # IDE 增强 381：饮品词（饮料/啤酒/葡萄酒/咖啡/茶/烈酒/果汁/牛奶）
    "饮料": "drink", "啤酒": "beer", "葡萄酒": "wine", "咖啡": "coffee",
    # IDE 增强 382：作物词（小麦/水稻/玉米/土豆/番茄/胡萝卜/南瓜/棉花）
    "小麦": "wheat", "水稻": "rice", "玉米": "corn", "土豆": "potato",
    # IDE 增强 383：矿物加工词（锭/粉/矿渣/晶体/宝石/浓缩物/块/碎屑）
    "锭": "ingot", "粉": "powder", "矿渣": "slag", "晶体": "crystal",
    # IDE 增强 384：装置词（装置/设备/机器/组件/零件/部件/模块已有模块/电路）
    "装置": "device", "设备": "equipment", "机器": "machine", "组件": "component",
    # IDE 增强 385：调料词（香料/调料/盐/糖/油/酱汁/醋/胡椒）
    "香料": "spice", "调料": "seasoning", "盐": "salt", "糖": "sugar",
    # IDE 增强 386：基地词（营地/前哨/据点/总部/避难所/定居点/哨站/基地）
    "营地": "camp", "前哨": "outpost", "据点": "stronghold", "总部": "hq",
    # IDE 增强 387：状态词（中毒/流血/烧伤/感染/辐射/眩晕/冰冻/麻痹）
    "中毒": "poison", "流血": "bleed", "烧伤": "burn", "感染": "infection",
    # IDE 增强 388：机械零件词（齿轮/轴承/弹簧/螺丝/皮带/链条/活塞/飞轮）
    "齿轮": "gear", "轴承": "bearing", "弹簧": "spring", "螺丝": "screw",
    # IDE 增强 389：加工机器词（破碎机/研磨机/过滤机/干燥机/冷却机/加热机）
    "破碎机": "crusher", "研磨机": "mill", "过滤机": "filter", "干燥机": "dryer",
    # IDE 增强 390：液体储存词（储罐/蓄水池/水塔/油罐/储气罐/冷却塔）
    "储罐": "tank", "蓄水池": "reservoir", "水塔": "watertower", "油罐": "oiltank",
    # IDE 增强 391：职业词（建筑师/工程师/科学家/医生/厨师/商人/矿工/木匠）
    "建筑师": "architect", "工程师": "engineer", "科学家": "scientist", "医生": "doctor",
    # IDE 增强 392：废料词（废料/废铁/废木/碎布/骨头/垃圾/残渣/碎屑已有碎屑）
    "废料": "scrap", "废铁": "iron", "废木": "wood", "碎布": "rag",
    # IDE 增强 393：角色成长词（经验/等级/技能点/天赋/属性/熟练度/专精/声望点）
    "经验": "exp", "等级": "level", "技能点": "skillpoint", "天赋": "talent",
    # IDE 增强 394：战斗机制词（连击/反击/格挡/闪避/招架/瞄准/蓄力/处决）
    "连击": "combo", "反击": "counter", "格挡": "block", "闪避": "dodge",
    # IDE 增强 395：资源点词（矿点/泉眼/遗迹/废墟/沉船/宝箱/营地已有营地/烽火台）
    "矿点": "ore", "泉眼": "spring", "遗迹": "ruins", "废墟": "wreck",
    # IDE 增强 396：气候词（降雨量/风速/气候/季节变化/能见度/气压差）
    "降雨量": "rainfall", "风速": "windspeed", "气候": "climate", "季节变化": "seasonal",
    # IDE 增强 397：群落词（湿地/草原/苔原/热带/温带/海岸/礁石/冻土）
    "湿地": "wetland", "草原": "grassland", "苔原": "tundra", "热带": "tropical",
    # IDE 增强 398：贸易品词（丝绸/瓷器/毛皮/珍珠/琥珀/香木/象牙/珊瑚）
    "丝绸": "silk", "瓷器": "porcelain", "毛皮": "fur", "珍珠": "pearl",
    # IDE 增强 399：动物行为词（迁徙/冬眠/觅食/繁殖/领地/巢穴/伏击/警戒）
    "迁徙": "migrate", "冬眠": "hibernate", "觅食": "forage", "繁殖": "breed",
    # IDE 增强 400：魔法词（咒语/法术/结界/召唤/驱散/变形/瞬移/预言）
    "咒语": "spell", "法术": "magic", "结界": "barrier", "召唤": "summon",
    # IDE 增强 401：法术系词（火系/冰系/雷系/暗影/神圣/自然/奥术/死灵）
    "火系": "fire", "冰系": "ice", "雷系": "lightning", "暗影": "shadow",
    # IDE 增强 402：敌人类型词（精英/头目/小兵/召唤物/变异体/傀儡/哨兵/追踪者）
    "精英": "elite", "头目": "boss", "小兵": "minion", "召唤物": "summon",
    # IDE 增强 403：工艺词（选矿/浮选/煅烧/电解/淬火/镀层/退火/烧结）
    "选矿": "beneficiate", "浮选": "flotation", "煅烧": "calcine", "电解": "electrolyze",
    # IDE 增强 404：通信词（频率/波长/带宽/干扰/中继/加密/解码/广播）
    "频率": "frequency", "波长": "wavelength", "带宽": "bandwidth", "干扰": "interference",
    # IDE 增强 405：建筑结构词（承重/框架/支撑/拱门/柱/梁已有横梁/穹顶/桁架）
    "承重": "loadbearing", "框架": "frame", "支撑": "support", "拱门": "arch",
    # IDE 增强 406：精炼工艺词（纯化/分离/提取/提纯/冷凝/结晶/沉淀/吸附）
    "纯化": "purify", "分离": "separate", "提取": "extract", "提纯": "refine",
    # IDE 增强 407：矿井设备词（绞车/矿车/通风机/排水泵/支柱/巷道灯）
    "绞车": "winch", "矿车": "cart", "通风机": "fan", "排水泵": "dewater",
    # IDE 增强 408：区域规划词（分区/地块/网格/区块/边界/边界墙/缓冲带/市中心）
    "分区": "zone", "地块": "plot", "网格": "grid", "区块": "chunk",
    # IDE 增强 409：生活品词（蜡烛/肥皂/牙刷/毛巾/毯子/灯油/水壶/餐具）
    "蜡烛": "candle", "肥皂": "soap", "牙刷": "toothbrush", "毛巾": "towel",
    # IDE 增强 410：自动化词（自动化/远程控制/监控/警报/传感器/联动/程序控制）
    "自动化": "automation", "远程控制": "remote", "监控": "monitor", "警报": "alarm",
    # IDE 增强 411：运输方式词（空运/海运/陆运/管道运输/快递/货运/客运/配送）
    "空运": "airlift", "海运": "shipping", "陆运": "ground", "管道运输": "pipeline",
    # IDE 增强 412：商业链词（生产链/供应链/分销/零售/批发/代理/直销/电商）
    "生产链": "production", "供应链": "supply", "分销": "distribute", "零售": "retail",
    # IDE 增强 413：城市设施词（路灯/长椅/花坛/喷泉/公告栏/邮箱/电话亭/报刊亭）
    "路灯": "streetlight", "长椅": "bench", "花坛": "flowerbed", "喷泉": "fountain",
    # IDE 增强 414：地形特征词（山峰/峡谷/悬崖/瀑布/湖泊/沙丘/火山/冰川）
    "山峰": "peak", "峡谷": "canyon", "悬崖": "cliff", "瀑布": "waterfall",
    # IDE 增强 415：植物词（灌木/藤蔓/苔藓/蕨类/花朵/仙人掌/芦苇/水草）
    "灌木": "bush", "藤蔓": "vine", "苔藓": "moss", "蕨类": "fern",
    # IDE 增强 416：勘探词（勘探/钻探/采样/测绘/勘察/标图/探矿/岩芯）
    "勘探": "prospect", "钻探": "drilling", "采样": "sample", "测绘": "survey",
    # IDE 增强 417：实验容器词（坩埚/烧瓶/试管/培养皿/蒸馏瓶/量杯/漏斗/研钵）
    "坩埚": "crucible", "烧瓶": "flask", "试管": "testtube", "培养皿": "petridish",
    # IDE 增强 418：组装线词（装配线/总装/质检/包装/封箱/贴标/上漆/干燥线）
    "装配线": "assemblyline", "总装": "finalassembly", "质检": "quality", "包装": "packaging",
    # IDE 增强 419：燃料词（柴油/汽油/煤油/木炭/燃料棒/电池组/乙醇/沼气）
    "柴油": "diesel", "汽油": "gasoline", "煤油": "kerosene", "木炭": "charcoal",
    # IDE 增强 420：装饰词（地毯/挂毯/画作/雕塑/花瓶/窗帘/灯饰/摆件）
    "地毯": "carpet", "挂毯": "tapestry", "画作": "painting", "雕塑": "statue",
    # IDE 增强 421：仓库设备词（托盘/吊车/起重机/堆垛机/分拣臂/传送机/货梯/升降台）
    "托盘": "pallet", "吊车": "crane", "起重机": "hoist", "堆垛机": "stacker",
    # IDE 增强 422：庭院词（栅栏/篱笆/门廊/露台/凉亭/花园/菜园/鸡舍）
    "栅栏": "fence", "篱笆": "hedge", "门廊": "porch", "露台": "terrace",
    # IDE 增强 423：宗教建筑词（祭坛/神殿/圣所/图腾/神龛/修道院/教堂/寺庙）
    "祭坛": "altar", "神殿": "temple", "圣所": "sanctum", "图腾": "totem",
    # IDE 增强 424：村落建筑词（磨坊/铁匠铺/酒馆/马厩/谷仓/面包房/木工坊/染坊）
    "磨坊": "mill", "铁匠铺": "smithy", "酒馆": "tavern", "马厩": "stable",
    # IDE 增强 425：渔猎词（渔网/鱼叉/钓竿/诱饵/渔获/猎网/捕兽夹/狩猎台）
    "渔网": "fishingnet", "鱼叉": "harpoon", "钓竿": "fishingrod", "诱饵": "bait",
    # IDE 增强 426：仪表词（分析仪/检测仪/计量表/指示器/显示器/探针已有探测器/仪表盘）
    "分析仪": "analyzer", "检测仪": "detector", "计量表": "gauge", "指示器": "indicator",
    # IDE 增强 427：气象词（气象站/云层/风向/人工降雨/雷暴/龙卷风/寒潮/热浪）
    "气象站": "weatherstation", "云层": "cloud", "风向": "winddir", "人工降雨": "rainmaking",
    # IDE 增强 428：生物研究词（基因/菌落/样本/培养/克隆/突变/血清/疫苗）
    "基因": "gene", "菌落": "colony", "样本": "specimen", "培养": "culture",
    # IDE 增强 429：军火词（导弹/鱼雷/火箭/炸弹/装甲板/穿甲/高爆/燃烧弹）
    "导弹": "missile", "鱼雷": "torpedo", "火箭": "rocket", "炸弹": "bomb",
    # IDE 增强 430：攻城词（攻城锤/投石机/云梯/攻城塔/弩炮/破城槌/油罐车/爆破组）
    "攻城锤": "batteringram", "投石机": "catapult", "云梯": "ladder", "攻城塔": "siegetower",
    # IDE 增强 431：地下建筑词（地牢/墓穴/地下室/下水道/避难洞/地下城/地窖/矿井通道）
    "地牢": "dungeon", "墓穴": "crypt", "地下室": "basement", "下水道": "sewer",
    # IDE 增强 432：交易市场词（交易所/拍卖行/收购站/矿价/期货/订单/竞价/挂牌）
    "交易所": "exchange", "拍卖行": "auction", "收购站": "buyer", "矿价": "oreprice",
    # IDE 增强 433：矿工装备词（矿灯/呼吸面罩/安全绳/护目镜/工作服/防爆服/手套/靴子）
    "矿灯": "mininglamp", "呼吸面罩": "respirator", "安全绳": "safetyline", "护目镜": "goggles",
    # IDE 增强 434：控制终端词（控制台/操作台/终端机/控制面板/操纵杆/按钮/开关/旋钮）
    "控制台": "console", "操作台": "workstation", "终端机": "terminal", "控制面板": "panel",
    # IDE 增强 435：发电设施词（风车/水轮机/太阳能板/地热/潮汐能/燃料电池/热电联产/储电塔）
    "风车": "windmill", "水轮机": "waterturbine", "太阳能板": "solarpanel", "地热": "geothermal",
    # IDE 增强 436：交通设施词（收费站/加油站/充电站/服务区/停车场/洗车站/修理厂/加油站2）
    "收费站": "toll", "加油站": "gasstation", "充电站": "chargingstation", "服务区": "restarea",
    # IDE 增强 437：贸易路线词（商路/航线/驼队/贸易站/驿站/通商口岸/货运站/补给站）
    "商路": "caravanroute", "航线": "shippingroute", "驼队": "caravan", "贸易站": "tradingpost",
    # IDE 增强 438：导航词（罗盘/星图/信标/航点/里程碑/路标/灯塔/界碑）
    "罗盘": "compass", "星图": "starmap", "信标": "beacon", "航点": "waypoint",
    # IDE 增强 439：处理辅助词（料斗/加料口/出料口/搅拌器/乳化器/均质机/离心分离/过滤槽）
    "料斗": "hopper", "加料口": "inlet", "出料口": "outlet", "搅拌器": "agitator",
    # IDE 增强 440：矿井结构词（竖井/平巷/主巷/通风井/安全通道/避难硐室/排水沟/支架区）
    "竖井": "shaft", "平巷": "drift", "主巷": "mainhaulage", "通风井": "ventilation",
    # IDE 增强 441：冶炼辅料词（助熔剂/催化剂/反应剂/淬火液/冷却液/电解液/研磨剂/润滑剂）
    "助熔剂": "flux", "催化剂": "catalyst", "反应剂": "reagent", "淬火液": "quenchfluid",
    # IDE 增强 442：贸易政策词（关税/配额/许可证/垄断/补贴/禁运/特惠/配额制）
    "关税": "tariff", "配额": "quota", "许可证": "license", "垄断": "monopoly",
    # IDE 增强 443：特种矿物词（硫磺/硝石/磷矿/盐岩/云母/石膏/石墨/石英）
    "硫磺": "sulfur", "硝石": "saltpeter", "磷矿": "phosphate", "盐岩": "rocksalt",
    # IDE 增强 444：矿井安全词（瓦斯检测/坍塌预警/紧急出口/救援设备/灭火器/警报器/通风监测/地压监测）
    "瓦斯检测": "gasdetect", "坍塌预警": "collapsewarn", "紧急出口": "escape", "救援设备": "rescue",
    # IDE 增强 445：矿体类型词（脉状矿/砂矿/露头矿/浸染矿/层状矿/斑岩矿/冲积矿/结核矿）
    "脉状矿": "veinore", "砂矿": "placer", "露头矿": "outcrop", "浸染矿": "disseminated",
    # IDE 增强 446：矿区设施词（采矿场/露天矿/尾矿库/废石场/选矿厂/冶炼厂/矿工宿舍/仓库区）
    "采矿场": "mineyard", "露天矿": "openpit", "尾矿库": "tailings", "废石场": "wastedump",
    # IDE 增强 447：冶炼炉词（高炉/电炉/转炉/反射炉/鼓风炉/坩埚炉/平炉/感应炉）
    "高炉": "blastfurnace", "电炉": "electricfurnace", "转炉": "converter", "反射炉": "reverberatory",
    # IDE 增强 448：炼钢设备词（轧机/连铸机/钢包/精炼炉/拉丝机/锻造机/冲压机/退火炉）
    "轧机": "rollingmill", "连铸机": "caster", "钢包": "ladle", "精炼炉": "refiner",
    # IDE 增强 449：科研设施词（研究所/试验场/化验室/标本室/图书馆/档案馆/观测台/演算室）
    "研究所": "institute", "试验场": "testrange", "化验室": "assaylab", "标本室": "specimenroom",
    # IDE 增强 450：金属锭词（铜锭/钢锭/铝锭/锌锭/锡锭/铅锭/镍锭/钛锭）
    "铜锭": "copperingot", "钢锭": "steelingot", "铝锭": "aluminum", "锌锭": "zinc",
    # IDE 增强 451：矿工技能词（挖掘/爆破/支护/通风技能/运输技能/勘探技能/精炼技能/冶炼技能）
    "挖掘": "digging", "爆破": "blasting", "支护": "supporting", "通风技能": "ventilation",
    # IDE 增强 452：矿区生活词（食堂/澡堂/医务室/娱乐室/值班室/洗衣房/工具房/警卫室）
    "食堂": "canteen", "澡堂": "bathhouse", "医务室": "infirmary", "娱乐室": "recreationroom",
    # IDE 增强 453：矿权开发词（开采权/矿权证/开发计划/产能/回收率/品位/储量/回采率）
    "开采权": "miningright", "矿权证": "miningclaim", "开发计划": "developmentplan", "产能": "capacity",
    # IDE 增强 454：矿工管理词（排班/工资/工时/考勤/奖惩/班次/轮换/津贴）
    "排班": "schedule", "工资": "wage", "工时": "worktime", "考勤": "attendance",
    # IDE 增强 455：矿业法规词（环保法规/安全法规/用地许可/环评/复垦/赔偿金/罚款/审计）
    "环保法规": "environmentalreg", "安全法规": "safetyreg", "用地许可": "landusepermit", "环评": "environmentalreview",
    # IDE 增强 456：选矿建筑词（破碎站/筛分楼/储矿仓/装车楼/皮带廊/浓缩池/精矿仓/尾矿管线）
    "破碎站": "crushingstation", "筛分楼": "screeningtower", "储矿仓": "orebin", "装车楼": "loadingtower",
    # IDE 增强 457：矿物运输词（精矿运输/矿石列车/矿用卡车/驳船/散货船/转运站/卸矿站/堆场）
    "精矿运输": "concentratetransport", "矿石列车": "oretrain", "矿用卡车": "minetruck", "驳船": "barge",
    # IDE 增强 458：矿石取样词（取样点/化验单/品位曲线/矿样袋/样槽/岩样/矿样/标样）
    "取样点": "samplingpoint", "化验单": "assayreport", "品位曲线": "gradecurve", "矿样袋": "samplebag",
    # IDE 增强 459：采矿设备词（掘进机/凿岩机/装载机/铲运机/台车/锚杆机/喷浆机/提升机）
    "掘进机": "roadheader", "凿岩机": "rockdrill", "装载机": "loader", "铲运机": "scraper",
    # IDE 增强 460：选矿药剂词（捕收剂/起泡剂/抑制剂/调整剂/絮凝剂/活化剂/分散剂/消泡剂）
    "捕收剂": "collector", "起泡剂": "frother", "抑制剂": "depressant", "调整剂": "regulator",
    # IDE 增强 461：选矿工艺词（破碎/磨矿/重选/磁选/电选/浮选槽/摇床/跳汰机）
    "破碎": "crushing", "磨矿": "grinding", "重选": "gravity", "磁选": "magnetic",
    "电选": "electrostatic", "浮选槽": "flotationcell", "摇床": "shakingtable", "跳汰机": "jig",
    "絮凝剂": "flocculant", "活化剂": "activator", "分散剂": "dispersant", "消泡剂": "defoamer",
    "台车": "drillrig", "锚杆机": "bolter", "喷浆机": "shotcreter", "提升机": "hoistmachine",
    "样槽": "sampletrench", "岩样": "rocksample", "矿样": "oresample", "标样": "standard",
    "散货船": "bulkcarrier", "转运站": "transferstation", "卸矿站": "unloadingstation", "堆场": "stockyard",
    "皮带廊": "beltgallery", "浓缩池": "thickener", "精矿仓": "concentratebin", "尾矿管线": "tailingspipeline",
    "复垦": "reclamation", "赔偿金": "compensation", "罚款": "fine", "审计": "audit",
    "奖惩": "reward", "班次": "shift", "轮换": "rotation", "津贴": "allowance",
    "回收率": "recoveryrate", "品位": "grade", "储量": "reserves", "回采率": "extractionrate",
    "值班室": "dutyroom", "洗衣房": "laundry", "工具房": "toolroom", "警卫室": "guardroom",
    "运输技能": "hauling", "勘探技能": "prospecting", "精炼技能": "refining", "冶炼技能": "smelting",
    "锡锭": "tin", "铅锭": "lead", "镍锭": "nickel", "钛锭": "titanium",
    "图书馆": "library", "档案馆": "archive", "观测台": "observatory", "演算室": "computationroom",
    "拉丝机": "drawingmachine", "锻造机": "forge", "冲压机": "stamping", "退火炉": "annealingfurnace",
    "鼓风炉": "cupola", "坩埚炉": "cruciblefurnace", "平炉": "openhearth", "感应炉": "inductionfurnace",
    "选矿厂": "oreplant", "冶炼厂": "smeltery", "矿工宿舍": "bunkhouse", "仓库区": "storagesite",
    "层状矿": "stratified", "斑岩矿": "porphyry", "冲积矿": "alluvial", "结核矿": "nodule",
    "灭火器": "extinguisher", "警报器": "siren", "通风监测": "ventmonitor", "地压监测": "pressuremonitor",
    "云母": "mica", "石膏": "gypsum", "石墨": "graphite", "石英": "quartz",
    "补贴": "subsidy", "禁运": "embargo", "特惠": "preference", "配额制": "quota2",
    "冷却液": "coolant", "电解液": "electrolyte", "研磨剂": "abrasive", "润滑剂": "lubricant",
    "安全通道": "escape", "避难硐室": "refuge", "排水沟": "drainage", "支架区": "supportzone",
    "乳化器": "emulsifier", "均质机": "homogenizer", "离心分离": "centrifuge", "过滤槽": "filterbed",
    "里程碑": "milestone", "路标": "signpost", "灯塔": "lighthouse", "界碑": "boundarymark",
    "驿站": "relaystation", "通商口岸": "port", "货运站": "freightstation", "补给站": "supplystation",
    "停车场": "parking", "洗车站": "carwash", "修理厂": "garage", "加油站2": "fuelstation",
    "潮汐能": "tidal", "燃料电池": "fuelcell", "热电联产": "cogeneration", "储电塔": "batterytower",
    "操纵杆": "lever", "按钮": "button", "开关": "switch", "旋钮": "knob",
    "工作服": "overall", "防爆服": "blastshield", "手套": "gloves", "靴子": "boots",
    "期货": "futures", "订单": "order", "竞价": "bidding", "挂牌": "listing",
    "避难洞": "bunker", "地下城": "undercity", "地窖": "cellar", "矿井通道": "minepassage",
    "弩炮": "ballista", "破城槌": "ram", "油罐车": "tanker", "爆破组": "demolition",
    "装甲板": "armorplate", "穿甲": "piercing", "高爆": "highyield", "燃烧弹": "incendiary",
    "克隆": "clone", "突变": "mutation", "血清": "serum", "疫苗": "vaccine",
    "雷暴": "thunderstorm", "龙卷风": "tornado", "寒潮": "coldwave", "热浪": "heatwave",
    "显示器": "display", "仪表盘": "dashboard", "读数": "reading", "校准": "calibrate",
    "渔获": "catch", "猎网": "huntingnet", "捕兽夹": "snare", "狩猎台": "blind",
    "谷仓": "granary", "面包房": "bakery", "木工坊": "carpentry", "染坊": "dyery",
    "神龛": "shrine", "修道院": "monastery", "教堂": "church", "寺庙": "monastery2",
    "凉亭": "gazebo", "花园": "garden", "菜园": "grove", "鸡舍": "coop",
    "分拣臂": "sorterarm", "传送机": "conveyor", "货梯": "freightlift", "升降台": "lift",
    "花瓶": "vase", "窗帘": "curtain", "灯饰": "lantern", "摆件": "decor",
    "燃料棒": "fuelrod", "电池组": "battery", "乙醇": "ethanol", "沼气": "biogas",
    "封箱": "sealing", "贴标": "labeling", "上漆": "painting", "干燥线": "dryingline",
    "蒸馏瓶": "still", "量杯": "beaker", "漏斗": "funnel", "研钵": "mortar",
    "勘察": "recon", "标图": "plotting", "探矿": "oreprospect", "岩芯": "core",
    "花朵": "flower", "仙人掌": "cactus", "芦苇": "reed", "水草": "seaweed",
    "湖泊": "lake", "沙丘": "dune", "火山": "volcano", "冰川": "glacier",
    "公告栏": "bulletin", "邮箱": "mailbox", "电话亭": "phonebooth", "报刊亭": "kiosk",
    "批发": "wholesale", "代理": "agency", "直销": "direct", "电商": "ecommerce",
    "快递": "express", "货运": "freight", "客运": "passenger", "配送": "delivery",
    "传感器": "sensor", "联动": "interlock", "程序控制": "logic", "配电": "distribution",
    "毯子": "blanket", "灯油": "lampoil", "水壶": "kettle", "餐具": "cutlery",
    "边界": "border", "缓冲带": "buffer", "市中心": "downtown", "郊区": "suburb",
    "支柱": "support", "巷道灯": "lamp", "轨道": "track", "安全帽": "helmet",
    "冷凝": "condense", "结晶": "crystallize", "沉淀": "precipitate", "吸附": "absorb",
    "柱": "column", "穹顶": "dome", "桁架": "truss", "加固": "reinforce",
    "中继": "relay", "加密": "encrypt", "解码": "decode", "广播": "broadcast",
    "淬火": "quench", "镀层": "plating", "退火": "anneal", "烧结": "sinter",
    "变异体": "mutant", "傀儡": "golem", "哨兵": "sentinel", "追踪者": "stalker",
    "神圣": "holy", "自然": "nature", "奥术": "arcane", "死灵": "necromancy",
    "驱散": "dispel", "变形": "transform", "瞬移": "teleport", "预言": "divine",
    "领地": "territory", "巢穴": "nest", "伏击": "ambush", "警戒": "alert",
    "琥珀": "amber", "香木": "sandalwood", "象牙": "ivory", "珊瑚": "coral",
    "温带": "temperate", "海岸": "coast", "礁石": "reef", "冻土": "permafrost",
    "能见度": "visibility", "气压差": "pressuregrad", "湿度变化": "humidity", "风暴强度": "stormintensity",
    "沉船": "shipwreck", "宝箱": "treasure", "烽火台": "beacon", "地标": "landmark",
    "招架": "parry", "瞄准": "aim", "蓄力": "charge", "处决": "execute",
    "属性": "stat", "熟练度": "mastery", "专精": "specialize", "声望点": "rep",
    "骨头": "bone", "垃圾": "trash", "残渣": "residue", "尘土": "dust",
    "厨师": "chef", "商人": "trader", "矿工": "miner", "木匠": "carpenter",
    "储气罐": "gastank", "冷却塔": "coolingtower", "水渠": "aqueduct", "水井": "well",
    "冷却机": "cooler", "加热机": "heater", "蒸馏器": "distiller", "反应釜": "reactor",
    "皮带": "belt", "链条": "chain", "活塞": "piston", "飞轮": "flywheel",
    "辐射": "radiation", "眩晕": "stun", "冰冻": "freeze", "麻痹": "paralyze",
    "避难所": "shelter", "定居点": "settlement", "哨站": "post", "基地": "base",
    "油": "oil", "酱汁": "sauce", "醋": "vinegar", "胡椒": "pepper",
    "零件": "part", "部件": "assembly", "电路": "circuit", "芯片": "chip",
    "宝石": "gem", "浓缩物": "concentrate", "块": "block", "碎屑": "shard",
    "番茄": "tomato", "胡萝卜": "carrot", "南瓜": "pumpkin", "棉花": "cotton",
    "茶": "tea", "烈酒": "liquor", "果汁": "juice", "牛奶": "milk",
    "蘑菇": "mushroom", "鱼干": "jerky", "蜂蜜": "honey", "奶酪": "cheese",
    "压缩机": "compressor", "磨矿机": "grinder", "筛矿机": "screen", "装填机": "loader",
    "窗": "window", "房间": "room", "天窗": "skylight", "壁炉": "fireplace",
    "地基": "foundation", "墙板": "wall", "横梁": "beam", "脚手架": "scaffold",
    "煎": "fry", "烤": "roast", "炖": "stew", "烘焙": "bake",
    "油漆": "paint", "涂料": "coating", "喷漆": "spray", "上色": "tint",
    "腰带": "belt", "徽章": "badge", "耳环": "earring", "头饰": "headgear",
    "信号灯": "signal", "高架": "elevated", "隧道": "tunnel", "路口": "junction",
    "冰箱": "fridge", "冷冻": "freezer", "桶": "barrel", "罐子": "jar",
    "哨戒": "sentry", "碉堡": "bunker", "路障": "barricade", "铁丝网": "barbed",
    "弓箭": "bow", "弩": "crossbow", "法杖": "staff", "锤": "blunt",
    "改造": "modify", "修理": "repair", "分解": "deconstruct", "组装": "assemble",
    "恒星": "star", "卫星": "satellite", "探测器": "probe", "陨石": "meteor",
    "翻耕机": "tiller", "喷灌机": "sprinkler", "粮仓": "granary", "蜂箱": "beehive",
    "铀矿": "uranium", "稀土": "rare", "硅矿": "silicon", "石油": "oil",
    "泵": "pump", "阀门": "valve", "蒸汽": "steam", "液压": "hydraulic",
    "电池": "battery", "充电": "charge", "电网": "grid", "断电": "outage",
    "仓库": "warehouse", "货架": "shelf", "叉车": "forklift", "装卸": "loading",
    "熔炉": "furnace", "钻机": "drill", "雷达": "radar", "天线": "antenna",
    "布料": "cloth", "皮革": "leather", "混凝土": "concrete", "塑料": "plastic",
    "燃料": "fuel", "方向盘": "steering", "传动": "transmission", "油箱": "tank",
    "加工": "process", "流水线": "pipeline", "发电机": "generator", "能源": "energy",
    "升空": "ascend", "俯冲": "dive", "急升": "climb", "悬停": "hovers",
    "占领": "occupy", "入侵": "invade", "围攻": "siege", "哨塔": "watchtower",
    "游泳": "swim", "浮力": "buoyancy", "潜水艇": "submarine", "水肺": "scuba",
    "灌溉": "irrigate", "施肥": "fertilize", "畜牧": "livestock", "温室": "greenhouse",
    "音调": "pitch", "节拍": "beat", "音符": "note", "音效": "sfx",
    "发明": "invention", "升级": "upgrade", "科技树": "techtree", "实验": "experiment",
    "页脚": "footer", "标签栏": "tabbar", "通知": "toast", "提示": "hint",
    "睡眠": "sleep", "精力": "stamina", "疲惫": "fatigue", "伤口": "wound",
    "船": "boat", "潜艇": "submarine", "飞行器": "aircraft", "机甲": "mech",
    "铲": "shovel", "绳索": "rope", "扳手": "wrench", "焊枪": "welder",
    "收获": "harvest", "种植": "plant", "木材": "wood", "水源": "water",
    "温度": "temperature", "湿度": "humidity", "气压": "pressure", "氧气": "oxygen",
    "框架": "framework", "运行时": "runtime", "驱动": "driver", "接口": "interface",
    "过场": "cutscene", "叙事": "narrative", "剧本": "script", "镜头": "camera",
    "敌对": "hostile", "联盟": "alliance", "关系": "relation", "招募": "recruit",
    "价格": "price", "商人": "merchant", "货币": "currency", "买卖": "buy",
    "奖励": "reward", "成就": "achievement", "目标": "objective", "条件": "condition",
    "桥梁": "bridge", "道路": "road", "塔": "tower", "城墙": "wall",
    "野兽": "beast", "宠物": "pet", "坐骑": "mount", "昆虫": "bug",
    "昼夜": "daynight", "光照": "lighting", "雾": "fog", "风": "wind",
    "沙漠": "desert", "洞穴": "cave", "高原": "plateau", "沼泽": "swamp",
    "技能": "skill", "伤害": "damage", "暴击": "crit", "护盾": "shield",
    "道具": "item", "材料": "material", "锻造": "craft", "合成": "recipe",
    "预加载": "preload", "延迟加载": "lazy", "清理": "cleanup", "释放": "release",
    "烟": "smoke", "碎片": "debris", "闪电": "lightning", "毒": "poison",
    "世界生成": "worldgen", "种子": "seed", "难度": "difficulty", "关卡": "level",
    "溢出": "overflow", "换行": "wrap", "居中": "center", "收缩": "shrink",
    "路径": "path", "视野": "fov", "区域": "region", "传送": "teleport",
    "按键": "key", "快捷键": "shortcut", "手势": "gesture", "拖拽": "drag",
    "分词": "token", "截取": "substring", "拼接": "concat", "替换": "replace",
    "事务": "transaction", "主键": "primary", "索引": "index", "查询": "query",
    "工具提示": "tooltip", "下拉框": "dropdown", "复选框": "checkbox", "滑块": "slider",
    "音频流": "audio", "字幕": "subtitle", "音量": "volume", "画面": "frame",
    "扭矩": "torque", "阻尼": "damping", "刚度": "stiffness", "惯性": "inertia",
    "遮挡": "occlusion", "阴影": "shadow", "贴图": "texture", "网格": "mesh",
    "通知": "notify", "回调": "callback", "信号": "signal", "指令": "command",
    "填充": "fill", "描边": "stroke", "渐变": "gradient", "投影": "shadow",
    "方差": "variance", "样本": "sample", "概率": "probability", "期望": "expectation",
    "默认值": "default", "环境": "env", "标志": "flag", "注册": "register",
    "下载": "download", "读写": "io", "存储": "storage", "备份": "backup",
    "压缩": "compress", "格式化": "format", "校验和": "checksum", "转换": "convert",
    "时区": "timezone", "延迟": "delay", "期限": "deadline", "超时": "timeout",
    "降级": "degrade", "熔断": "circuit", "限流": "ratelimit", "灰度": "canary",
    "重试": "retry", "取消": "cancel", "刷新": "refresh", "提示": "toast",
    "特效": "effect", "音效": "audio", "动画": "animation", "场景": "scene",
    "库存": "inventory", "价格": "price", "商品": "product", "交易": "trade",
    "查找": "find", "递归": "recursive", "遍历": "traverse", "匹配": "match",
    "并发": "concurrent", "缓存": "cache", "内存": "memory", "崩溃": "crash",
    "同步": "sync", "异步": "async", "超时": "timeout", "重试": "retry",
    "像素": "pixel", "触摸": "touch", "手势": "gesture", "滚动": "scroll",
    # IDE 增强 275：Flutter/UI 词（控件/界面/布局/导航/主题/状态）
    "控件": "widget", "界面": "ui", "布局": "layout", "导航": "navigate",
    "主题": "theme", "状态": "state", "动画": "animation", "页面": "page",
    # IDE 增强 163：游戏/交互常用词（背包/商店/对话/升级/瞄准/射击等）
    "背包": "inventory", "商店": "shop", "对话": "dialog", "升级": "upgrade",
    "瞄准": "aim", "射击": "shoot", "跳跃": "jump", "移动": "move",
    "动画": "animation", "音效": "sound", "背包栏": "inventory",
}


def normalize_goals(goal: str) -> list[str]:
    """目标词规范化：分词 + 中英同义映射 + 中文复合词子串映射（纯逻辑）。"""
    goals = [g for g in goal.lower().replace(",", " ").split() if len(g) > 1]
    for g in list(goals):
        if g in _SYN:
            goals.append(_SYN[g])
    for _k, _v in _SYN.items():
        if _k in goal and _v not in goals:
            goals.append(_v)
    return goals


# ══════════════ distill_pipeline（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""distill_pipeline.py — P3 蒸馏管线（抄 unsloth/onnxruntime 思路，TOP_TIER_PLAN ⑦）。

目标：蒸馏 3 个本地小模型（代码语义嵌入 / 错误分类 / 值函数），
教师=大模型 API 或本地大模型，学生=小模型（Qwen2.5-1.5B 级 / ModernBERT），
导出 ONNX + INT8 量化 → onnxruntime 推理（local_intel.py 消费）。

⚠️ 需要 GPU + 训练依赖（torch/transformers/unsloth）：
  pip install torch transformers unsloth onnx onnxruntime
数据自举：从 unified-rx 历史数据生成（scan-log / 教训库 / stats 调用历史）。

用法：
  python distill_pipeline.py --stage prepare   # 1. 生成训练数据（自举）
  python distill_pipeline.py --stage distill   # 2. 蒸馏训练（教师→学生）
  python distill_pipeline.py --stage export    # 3. 导出 ONNX + 量化
  python distill_pipeline.py --all             # 全流程
"""
import argparse
import json
import sys
from pathlib import Path

# 数据源（unified-rx 历史数据）
_SCAN_LOG = Path(os.path.expanduser("~/.unified-rx/scan-log.jsonl"))
_LSE_STATE = Path(os.path.expanduser("~/.unified-rx/lse-state.json"))
_OUT_DIR = Path(__file__).resolve().parent / "models"


# ── Stage 1: 数据准备（自举）──────────────────────────────
def prepare_data(out_dir: Path) -> dict:
    """从 scan-log / lse-state 生成蒸馏训练数据。

    三类数据：
      1. 代码语义嵌入：代码片段（file:line + 内容）→ 语义对（相似/不相似）
      2. 错误分类：scan-log 的错误摘要 → 类别标签
      3. 值函数：教训 utility + 上下文 → (state, value) 对
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {"scan_rows": 0, "lesson_rows": 0, "embed_pairs": 0,
             "error_rows": 0, "value_rows": 0}

    embed_pairs, error_rows, value_rows = [], [], []

    # 1. scan-log（扫描记录：工具/摘要/路径）
    if _SCAN_LOG.exists():
        with open(_SCAN_LOG, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stats["scan_rows"] += 1
                summary = str(rec.get("summary", ""))[:200]
                tool = rec.get("tool", "")
                if not summary:
                    continue
                # 错误分类样本：工具 + 摘要 → 是否含问题
                has_issue = any(k in summary for k in
                                ("issue", "错误", "warning", "bug", "问题"))
                error_rows.append({"text": f"[{tool}] {summary}",
                                   "label": 1 if has_issue else 0})
                # 值函数样本：教训 utility 关联
                if has_issue:
                    value_rows.append({"state": summary, "value": 0.2})
                else:
                    value_rows.append({"state": summary, "value": 0.8})

    # 2. lse-state（教训 utility）
    if _LSE_STATE.exists():
        try:
            with open(_LSE_STATE, encoding="utf-8") as fh:
                lse = json.load(fh)
            lessons = lse.get("lessons", {})
            if isinstance(lessons, dict):
                for lid, lesson in lessons.items():
                    stats["lesson_rows"] += 1
                    utility = float(lesson.get("utility", 0.5)) if isinstance(lesson, dict) else 0.5
                    value_rows.append({"state": str(lid)[:100], "value": utility})
        except (json.JSONDecodeError, OSError):  # 尽力而为（吞错可追溯）
            pass

    # 3. 嵌入对（从错误样本构造相似/不相似对）
    texts = [e["text"] for e in error_rows[:2000]]
    for i in range(0, len(texts) - 1, 2):
        if i + 1 < len(texts):
            embed_pairs.append({"anchor": texts[i], "positive": texts[i + 1],
                                "label": 1})  # 同源近似（简化；真实需语义标注）
    stats["embed_pairs"] = len(embed_pairs)
    stats["error_rows"] = len(error_rows)
    stats["value_rows"] = len(value_rows)

    # 落盘
    (out_dir / "embed_pairs.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in embed_pairs[:5000]),
        encoding="utf-8")
    (out_dir / "error_rows.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in error_rows[:5000]),
        encoding="utf-8")
    (out_dir / "value_rows.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in value_rows[:5000]),
        encoding="utf-8")
    return stats


# ── Stage 2/3: 蒸馏训练 + 导出（占位实现，需 GPU 环境）────
def distill(out_dir: Path) -> dict:
    """蒸馏训练（教师→学生）。

    完整实现需 torch/transformers/unsloth + GPU。此处提供结构：
      - 教师：大模型 API 或本地（生成软标签）
      - 学生：ModernBERT/Qwen2.5-1.5B（LoRA 微调）
      - 损失：KL 散度（蒸馏）+ CE（硬标签）
    环境未就绪时返回指导信息（不炸）。
    """
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return {"ok": False, "stage": "distill",
                "error": "训练依赖未装：pip install torch transformers unsloth",
                "guide": "GPU 环境就绪后运行；数据已由 prepare 生成在 models/"}
    # ---- 完整实现骨架（GPU 环境执行）----
    # from unsloth import FastLanguageModel
    # model, tokenizer = FastLanguageModel.from_pretrained("Qwen/Qwen2.5-1.5B")
    # ... LoRA 训练（数据集 = models/*.jsonl）
    # ... 蒸馏损失 = KL(student_logits, teacher_logits) + CE
    return {"ok": False, "stage": "distill",
            "error": "蒸馏训练需 GPU + 数据标注流程（骨架已就位，见代码注释）"}


def export_onnx(out_dir: Path) -> dict:
    """导出 ONNX + INT8 量化（onnxruntime 消费）。"""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return {"ok": False, "stage": "export",
                "error": "onnxruntime 未装：pip install onnxruntime"}
    return {"ok": False, "stage": "export",
            "error": "需先完成 distill 得到学生模型权重；导出用 transformers.onnx 或 optimum"}


def main():
    ap = argparse.ArgumentParser(description="蒸馏管线（P3 本地智能）")
    ap.add_argument("--stage", choices=["prepare", "distill", "export"],
                    default="prepare")
    ap.add_argument("--out", default=str(_OUT_DIR))
    args = ap.parse_args()
    out = Path(args.out)
    if args.stage == "prepare":
        stats = prepare_data(out)
        print(json.dumps({"ok": True, "stage": "prepare", "stats": stats},
                         ensure_ascii=False, indent=2))
    elif args.stage == "distill":
        print(json.dumps(distill(out), ensure_ascii=False, indent=2))
    elif args.stage == "export":
        print(json.dumps(export_onnx(out), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())


# ══════════════ quality_engine（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""quality_engine.py — P2a 质量引擎多后端（抄 ruff/semgrep/gitleaks/pyright）。

每个后端：可用就用（子进程 JSON），缺失自动降级（返回 unavailable，不炸）。
后端清单：
  - ruff     Python lint（AST 规则，Rust 实现，★49k）
  - semgrep  模式即规则跨语言静态分析（★16k）
  - gitleaks 密钥泄露检测（★28.6k）
  - pyright  静态类型检查（★15.6k）

用法：
  qe = QualityEngine()
  qe.scan(path)            # 跑全部可用后端
  qe.ruff_check(path)      # 单后端
"""
import shutil
import subprocess


class QualityEngine:
    """质量检查多后端（自动探测可用性，缺失降级）。"""

    def __init__(self):
        self._cache: dict[str, str | None] = {}

    # ── 后端探测 ──────────────────────────────────────────
    def _find(self, name: str) -> str | None:
        """找可执行文件（缓存结果）。"""
        if name not in self._cache:
            self._cache[name] = shutil.which(name)
        return self._cache[name]

    def available(self) -> dict[str, bool]:
        return {
            "ruff": self._find("ruff") is not None,
            "semgrep": self._find("semgrep") is not None,
            "gitleaks": self._find("gitleaks") is not None,
            "pyright": self._find("pyright") is not None,
            "codeql": self._find("codeql") is not None,
            "angr": self._find("python") is not None and self._angr_importable(),
        }

    def _angr_importable(self) -> bool:
        try:
            import angr  # noqa: F401
            return True
        except ImportError:
            return False

    # ── 单后端 ────────────────────────────────────────────
    def ruff_check(self, path: str, select: str = "E,F", timeout: int = 60) -> dict:
        """ruff lint（Python AST 规则）。返回 {ok, findings, summary}。"""
        exe = self._find("ruff")
        if exe is None:
            return {"backend": "ruff", "available": False,
                    "error": "ruff 未安装（pip install ruff）"}
        try:
            r = subprocess.run(
                [exe, "check", "--select", select, "--output-format", "json", path],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"backend": "ruff", "available": True, "error": f"{type(exc).__name__}: {exc}"}
        try:
            findings = json.loads(r.stdout) if r.stdout.strip() else []
        except json.JSONDecodeError:
            findings = []
        # 按规则聚合
        by_rule: dict[str, int] = {}
        for f in findings:
            code = f.get("code", "?")
            by_rule[code] = by_rule.get(code, 0) + 1
        return {"backend": "ruff", "available": True, "ok": r.returncode == 0,
                "count": len(findings),
                "by_rule": by_rule,
                "top": [{"code": f.get("code"), "file": f.get("filename", ""),
                         "line": (f.get("location") or {}).get("row"),
                         "message": f.get("message", "")[:120]}
                        for f in findings[:20]]}

    def semgrep_check(self, path: str, timeout: int = 120) -> dict:
        """semgrep 扫描（模式规则，跨语言）。"""
        exe = self._find("semgrep")
        if exe is None:
            return {"backend": "semgrep", "available": False,
                    "error": "semgrep 未安装"}
        try:
            r = subprocess.run(
                [exe, "scan", "--json", "--quiet", path],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"backend": "semgrep", "available": True, "error": f"{type(exc).__name__}: {exc}"}
        try:
            data = json.loads(r.stdout) if r.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        results = data.get("results", [])
        by_rule: dict[str, int] = {}
        for res in results:
            rid = (res.get("check_id") or "?").split(".")[-1]
            by_rule[rid] = by_rule.get(rid, 0) + 1
        return {"backend": "semgrep", "available": True,
                "count": len(results), "by_rule": by_rule,
                "top": [{"rule": (res.get("check_id") or "?"),
                         "file": (res.get("path") or ""),
                         "line": (res.get("start") or {}).get("line"),
                         "message": (res.get("extra") or {}).get("message", "")[:120]}
                        for res in results[:20]]}

    def gitleaks_check(self, path: str, timeout: int = 120) -> dict:
        """gitleaks 密钥扫描。"""
        exe = self._find("gitleaks")
        if exe is None:
            return {"backend": "gitleaks", "available": False,
                    "error": "gitleaks 未安装"}
        try:
            r = subprocess.run(
                [exe, "detect", "--source", path, "--no-banner", "--report-format", "json",
                 "--report-path", os.devnull],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"backend": "gitleaks", "available": True, "error": f"{type(exc).__name__}: {exc}"}
        # gitleaks stdout 是报告行；stderr 有统计
        leaks = []
        for line in (r.stdout or "").splitlines():
            if not line.strip():
                continue
            try:
                leaks.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return {"backend": "gitleaks", "available": True,
                "count": len(leaks),
                "top": [{"rule": l.get("RuleID", ""), "file": l.get("File", ""),
                         "line": l.get("StartLine"), "secret": str(l.get("Secret", ""))[:8] + "..."}
                        for l in leaks[:20]]}

    def pyright_check(self, path: str, timeout: int = 180) -> dict:
        """pyright 类型检查（需 node 环境）。"""
        exe = self._find("pyright")
        if exe is None:
            return {"backend": "pyright", "available": False,
                    "error": "pyright 未安装"}
        try:
            r = subprocess.run(
                [exe, "--outputjson", path],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"backend": "pyright", "available": True, "error": f"{type(exc).__name__}: {exc}"}
        try:
            data = json.loads(r.stdout) if r.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        diags = data.get("generalDiagnostics", [])
        by_sev: dict[str, int] = {}
        for d in diags:
            sev = d.get("severity", "?")
            by_sev[sev] = by_sev.get(sev, 0) + 1
        return {"backend": "pyright", "available": True,
                "count": len(diags), "by_severity": by_sev,
                "top": [{"severity": d.get("severity"), "file": d.get("file", ""),
                         "line": d.get("range", {}).get("start", {}).get("line"),
                         "message": d.get("message", "")[:120]}
                        for d in diags[:20]]}

    # ── 顶级后端（2026-08-12：codeql/angr——探测降级，装了才跑）──
    def codeql_check(self, path: str, timeout: int = 300) -> dict:
        """CodeQL 数据流/污点分析（GitHub 级，需 codeql CLI + 数据库）。"""
        exe = self._find("codeql")
        if exe is None:
            return {"backend": "codeql", "available": False,
                    "error": "codeql 未安装（https://github.com/github/codeql）"}
        try:
            # 数据库不存在时提示建库（完整跑需要 codeql database create）
            db_path = os.path.join(path, "codeql-db")
            r = subprocess.run(
                [exe, "database", "create", db_path, "--language", "python",
                 "--source-root", path, "--overwrite"],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"backend": "codeql", "available": True,
                    "error": f"{type(exc).__name__}: {exc}"}
        return {"backend": "codeql", "available": True,
                "ok": r.returncode == 0,
                "note": "数据库已建（可跑 codeql database analyze 查数据流）"}

    def angr_check(self, path: str, timeout: int = 120) -> dict:
        """angr 符号执行（深度路径 bug：除零/越界；需 pip install angr）。"""
        if not self._angr_importable():
            return {"backend": "angr", "available": False,
                    "error": "angr 未安装（pip install angr）"}
        try:
            import angr
            proj = angr.Project(path, auto_load_libs=False)
            state = proj.factory.entry_state()
            # 简化：跑 5 秒符号执行，收集发现
            sm = proj.factory.simulation_manager(state)
            sm.explore(find=lambda s: s.history.block_count > 100,
                       num_find=3, avoid_unsat=True)
            return {"backend": "angr", "available": True,
                    "explored": len(sm.active) + len(sm.deadended),
                    "found": len(sm.found),
                    "note": "符号执行探测（路径探索；完整分析需定制约束）"}
        except Exception as exc:
            return {"backend": "angr", "available": True,
                    "error": f"angr 执行失败: {type(exc).__name__}: {exc}"}

    # ── 聚合扫描 ──────────────────────────────────────────
    def scan(self, path: str, backends: list[str] | None = None) -> dict:
        """跑全部（或指定）可用后端，聚合结果。"""
        if not os.path.exists(path):
            return {"ok": False, "error": f"路径不存在: {path}"}
        wanted = backends or ["ruff", "semgrep", "gitleaks", "pyright"]
        results = {}
        for b in wanted:
            if b == "ruff":
                results[b] = self.ruff_check(path)
            elif b == "semgrep":
                results[b] = self.semgrep_check(path)
            elif b == "gitleaks":
                results[b] = self.gitleaks_check(path)
            elif b == "pyright":
                results[b] = self.pyright_check(path)
            elif b == "codeql":
                results[b] = self.codeql_check(path)
            elif b == "angr":
                results[b] = self.angr_check(path)
        return {"ok": True, "path": path, "backends": results,
                "available": self.available()}


# ══════════════ failure_analyze（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""failure_analyze —— 根因分析（RCA，阶段2，TraceStation/test_report_analyzer 式）。

输入 traceback/失败文本 → 输出根因链报告：
  ① 解析：异常类型/消息/文件:行 帧链（纯正则，坏输入宽容）
  ② 关联（证据收集）：
     - telemetry：最近工具错误（同错误消息/同时间段）
     - scan-log：该 root 最近扫描已知问题
     - git：最近提交（谁改了什么——文件命中则高置信）
     - alarms：最近告警（卡死/慢/错误率）
  ③ 候选根因：规则驱动打分排序（证据强度）
  ④ 建议：下一步动作（causal_trace/bug_bisect/定向测试）

全部只读——不自动改代码。
"""



def _parse_traceback(text: str) -> dict:
    """从 traceback 文本提取：异常消息 / 文件:行 帧链。坏输入宽容。"""
    lines = [l.rstrip() for l in str(text).splitlines()]
    exc = ""
    frames: list[dict] = []
    for line in lines:
        m = re.search(r'File "([^"]+)", line (\d+)', line)
        if m:
            frames.append({"file": m.group(1), "line": int(m.group(2))})
    for line in reversed(lines):
        if line.strip():
            exc = line.strip()[:200]
            break
    return {
        "exception": exc,
        "frames": frames[:10],
        "file": frames[0]["file"] if frames else "",
        "line": frames[0]["line"] if frames else None,
    }


def _git_recent(root: str, n: int = 10) -> list[dict]:
    """最近提交（git log，只读；非 git 项目返回空）。"""
    try:
        r = subprocess.run(
            ["git", "-C", root, "log", "--format=%H|%an|%ad|%s",
             "--date=iso", f"-{n}"],
            capture_output=True, text=True, timeout=15, encoding="utf-8",
            errors="replace")
        out = []
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = line.split("|", 3)
                if len(parts) == 4:
                    out.append({"hash": parts[0][:10], "author": parts[1],
                                "time": parts[2], "message": parts[3][:100]})
        return out
    except (OSError, subprocess.TimeoutExpired):
        return []


def _git_touched_file(root: str, file_path: str, n: int = 20) -> list[dict]:
    """最近改动过该文件的提交（git log -- <file>）。"""
    try:
        r = subprocess.run(
            ["git", "-C", root, "log", "--format=%H|%an|%ad|%s",
             "--date=iso", f"-{n}", "--", file_path],
            capture_output=True, text=True, timeout=15, encoding="utf-8",
            errors="replace")
        out = []
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = line.split("|", 3)
                if len(parts) == 4:
                    out.append({"hash": parts[0][:10], "author": parts[1],
                                "time": parts[2], "message": parts[3][:100]})
        return out
    except (OSError, subprocess.TimeoutExpired):
        return []


def _scan_log_for(root: str, n: int = 20) -> list[dict]:
    """scan-log 中该 root 最近扫描记录（已知问题）。"""
    try:
        import scan_log_core
        logs = scan_log_core.query_logs(limit=200)
        out = []
        for l in logs:
            if root and root not in str(l.get("root", "")):
                continue
            if l.get("ok") is False or l.get("summary"):
                out.append({"ts": l.get("ts", 0), "tool": l.get("tool", ""),
                            "ok": l.get("ok"), "summary": str(l.get("summary", ""))[:100]})
        return out[-n:]
    except Exception:  # noqa: BLE001 —— 尽力而为
        return []


def failure_analyze(text: str, root: str = "", limit: int = 200) -> dict:
    """根因分析主入口：traceback → 根因链报告。"""
    tb = _parse_traceback(text)
    evidence: list[dict] = []
    candidates: list[dict] = []

    # ── 证据 1：遥测最近错误（同错误消息匹配优先） ──────────────
    try:
        import telemetry_core
        errs = telemetry_core.recent_errors(10)
        if errs:
            matched = []
            for e in errs:
                if tb["exception"] and tb["exception"][:40] in str(e.get("err", "")):
                    matched.append(e)
            evidence.append({"source": "telemetry_recent_errors",
                             "matched": matched, "items": errs[:5]})
            if matched:
                candidates.append({
                    "rank": 1, "confidence": "high",
                    "hypothesis": "同一错误近期在工具调用中反复出现（遥测记录）",
                    "evidence": [f"tool={m.get('tool')} err={m.get('err', '')[:60]}"
                                 for m in matched[:3]]})
    except Exception:  # noqa: BLE001
        pass

    # ── 证据 2：git 最近提交 + 异常文件命中 ─────────────────────
    if root and os.path.isdir(root):
        commits = _git_recent(root, 10)
        if commits:
            evidence.append({"source": "git_recent", "items": commits[:5]})
        if tb["file"]:
            touched = _git_touched_file(root, tb["file"], 10)
            if touched:
                evidence.append({"source": "git_touched_file",
                                 "file": tb["file"], "items": touched[:3]})
                candidates.append({
                    "rank": 1, "confidence": "high",
                    "hypothesis": f"异常文件 {tb['file']} 最近被提交改动——"
                                  f"新变更引入回归的可能性最高",
                    "evidence": [f"{c['hash']} {c['message']}" for c in touched[:3]]})
            elif commits:
                candidates.append({
                    "rank": 2, "confidence": "medium",
                    "hypothesis": "异常文件近期无改动，根因可能在依赖/调用方/环境",
                    "evidence": [f"文件未出现在最近 {10} 条提交中"]})
    else:
        commits = []
        if tb["file"]:
            candidates.append({
                "rank": 2, "confidence": "medium",
                "hypothesis": "未提供 root——无法关联 git 提交，建议传入项目路径",
                "evidence": []})

    # ── 证据 3：scan-log 已知问题 ──────────────────────────────
    if root:
        scan_issues = _scan_log_for(root, 10)
        if scan_issues:
            evidence.append({"source": "scan_log", "items": scan_issues[:5]})
            candidates.append({
                "rank": 3, "confidence": "medium",
                "hypothesis": "scan-log 显示该项目近期扫描出问题",
                "evidence": [f"{s.get('tool')}: {s.get('summary')}" for s in scan_issues[:3]]})

    # ── 证据 4：告警 ───────────────────────────────────────────
    try:
        import telemetry_core
        alarms = telemetry_core.read_alarms(10)
        if alarms:
            evidence.append({"source": "alarms", "items": alarms[:5]})
            crit = [a for a in alarms if a.get("level") == "CRITICAL"]
            if crit:
                candidates.append({
                    "rank": 3, "confidence": "medium",
                    "hypothesis": "存在 CRITICAL 告警（daemon 卡死/系统异常）可能为间接根因",
                    "evidence": [f"{a.get('rule')}: {a.get('msg', '')[:60]}"
                                 for a in crit[:2]]})
    except Exception:  # noqa: BLE001
        pass

    # ── 文件存在性验证（防幻觉：引用前先验证） ─────────────────
    file_exists = False
    if tb["file"]:
        cand = tb["file"]
        if os.path.isabs(cand):
            file_exists = os.path.exists(cand)
        elif root:
            file_exists = os.path.exists(os.path.join(root, cand.lstrip("/\\")))
    if tb["file"] and not file_exists:
        candidates.append({
            "rank": 4, "confidence": "low",
            "hypothesis": "异常文件不存在于本地——可能已删除/重命名或路径来自其他机器",
            "evidence": [f"{tb['file']} 不存在"]})

    # ── 建议（规则） ──────────────────────────────────────────
    suggestions = []
    if root:
        suggestions.append(f"causal_trace(root={root}) 回溯行为因果链")
        suggestions.append("bug_bisect 二分定位引入提交（good_commit → bad_commit）")
    suggestions.append("telemetry_query(status=error) 查看同窗口其他失败")
    suggestions.append("修复后跑对应测试 + std_check 验证")
    if not tb["frames"]:
        suggestions.append("输入不是标准 traceback（无 File 行）——可粘贴原始报错文本")

    return {
        "ok": True,
        "analyzed_at": time.time(),
        "exception": tb["exception"] or "(未识别到异常消息)",
        "location": {"file": tb["file"], "line": tb["line"],
                     "exists": file_exists,
                     "frames": tb["frames"]},
        "evidence": evidence,
        "candidates": sorted(candidates, key=lambda c: c["rank"])[:5],
        "suggestions": suggestions,
        "hint": "根因链按证据强度排序；rank=1 优先验证",
    }


if __name__ == "__main__":  # CLI 调试入口
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "Traceback: File \"x.py\", line 3, in main\nValueError: boom"
    root = sys.argv[2] if len(sys.argv) > 2 else ""
    print(json.dumps(failure_analyze(text, root), ensure_ascii=False, indent=1))


# ══════════════ mini_bert_tokenizer（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""mini_bert_tokenizer.py — 轻量 BERT WordPiece tokenizer（bge-small-zh ONNX 推理用）。

读 HuggingFace tokenizer.json（Xenova/transformers.js 格式）：
  - BertNormalizer（clean_text + handle_chinese_chars + lowercase）
  - BertPreTokenizer（空白/标点切分；中文按单字）
  - WordPiece（## 子词）+ CLS/SEP 拼接 + max_len 截断
纯 Python 零依赖（re + json）。~200 行。
"""

import unicodedata

# BertNormalizer 的 clean_text 规则
_CLEAN_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# handle_chinese_chars：中文字符两侧加空格
_CJK_RE = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff])")
# BertPreTokenizer：空白分隔 + 标点剥离
_PUNCT_RE = re.compile(r"([\s.,!?;:()\[\]{}<>\"'`~@#$%^&*_+=\-/\\|]+)")


class MiniBertTokenizer:
    """简化 BERT tokenizer（推理够用：编码为 input_ids + attention_mask）。"""

    def __init__(self, tokenizer_json: str, max_len: int = 256):
        with open(tokenizer_json, encoding="utf-8") as f:
            data = json.load(f)
        model = data["model"]
        self.vocab: dict[str, int] = model["vocab"]
        self.unk_id = int(self.vocab.get(model.get("unk_token", "[UNK]"), 100))
        self.pad_id = int(self.vocab.get("[PAD]", 0))
        self.cls_id = int(self.vocab.get("[CLS]", 101))
        self.sep_id = int(self.vocab.get("[SEP]", 102))
        self.continuing_prefix = model.get("continuing_subword_prefix", "##")
        self.max_len = max_len
        # 中文单字加速集
        self._cjk_chars = set(
            re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", "".join(self.vocab.keys())))

    # ── normalizer + pre_tokenizer ──
    def _normalize(self, text: str) -> str:
        text = _CLEAN_RE.sub(" ", text)
        text = unicodedata.normalize("NFC", text)
        # handle_chinese_chars：CJK 前后加空格（让 pre_tokenizer 拆成单字）
        text = _CJK_RE.sub(r" \1 ", text)
        return text

    def _pretokenize(self, text: str) -> list[str]:
        """BertPreTokenizer：按空白/标点切分，去空。"""
        parts = [p for p in _PUNCT_RE.split(text) if p and not p.isspace()]
        return parts

    # ── wordpiece ──
    def _wordpiece(self, word: str) -> list[str]:
        if word in self.vocab:
            return [word]
        tokens = []
        start = 0
        wlen = len(word)
        while start < wlen:
            end = wlen
            cur = None
            while end > start:
                sub = word[start:end]
                if start > 0:
                    sub = self.continuing_prefix + sub
                if sub in self.vocab:
                    cur = sub
                    break
                end -= 1
            if cur is None:
                return ["[UNK]"]  # 符号占位（encode 时映射 id）
            tokens.append(cur)
            start = end
        return tokens

    # ── encode ──
    def encode(self, text: str, max_len: int | None = None) -> dict[str, list[int]]:
        """text → {input_ids, attention_mask}。bge 有 max_len 截断。"""
        # DoS 防护（security-review MEDIUM）：_wordpiece 对超长 word 逐字符
        # 回退为 O(n²)——输入截断 4096 字符，防 MCP 超长无分隔文本卡死本地服务
        if len(text) > 4096:
            text = text[:4096]
        max_len = max_len or self.max_len
        ids: list[int] = [self.cls_id]
        for word in self._pretokenize(self._normalize(text)):
            for tok in self._wordpiece(word):
                tid = self.vocab.get(tok, self.unk_id)
                ids.append(tid)
                if len(ids) >= max_len - 1:
                    break
            if len(ids) >= max_len - 1:
                break
        ids.append(self.sep_id)
        ids = ids[:max_len]
        mask = [1] * len(ids)
        return {"input_ids": ids, "attention_mask": mask}

    def vocab_size(self) -> int:
        return len(self.vocab)


def encode_batch(tokenizer: MiniBertTokenizer, texts: list[str],
                 max_len: int = 256) -> dict[str, list[list[int]]]:
    """批量编码（batch → padding 到 batch 内最长，pad_id 补位）。"""
    encs = [tokenizer.encode(t, max_len=max_len) for t in texts]
    max_t = max(len(e["input_ids"]) for e in encs)
    input_ids = [e["input_ids"] + [tokenizer.pad_id] * (max_t - len(e["input_ids"])) for e in encs]
    masks = [e["attention_mask"] + [0] * (max_t - len(e["attention_mask"])) for e in encs]
    return {"input_ids": input_ids, "attention_mask": masks}


# ══════════════ replay_core（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""replay_core —— 操作录制/重放（阶段3，BugCraft 式：偶现变必现）。

- replay_record(name, step)：追加一步操作（工具调用 or 命令）到
  `~/.unified-rx/replays/<name>.jsonl`
- replay_run(name)：逐条重放——工具调用走 server._call（权限层照常），
  命令步骤需显式 `__authorized: true`（L4 语义，默认跳过并标注）
- 用途：用户报"崩溃了/偶现 bug" → 回放操作序列 → 必现 → 定位第一步

安全：
  - name 白名单（字母数字-_，防路径穿越）
  - cmd 默认拒绝（需显式授权）；超时保护
  - 全部只读回放语义由调用方保证（工具本身已有权限层）
"""


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def replays_dir() -> str:
    d = os.environ.get("UNIFIED_RX_STATE_DIR", "").strip()
    if not d:
        d = os.path.join(os.environ.get("USERPROFILE") or
                         os.environ.get("HOME") or ".", ".unified-rx")
    return os.path.join(d, "replays")


def _path_for(name: str) -> str | None:
    if not _NAME_RE.match(name or ""):
        return None
    p = os.path.join(replays_dir(), f"{name}.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def replay_record(name: str, step: dict) -> dict:
    """追加一步。step: {type: "tool", tool, args} | {type: "cmd", cmd, cwd, timeout}"""
    p = _path_for(name)
    if p is None:
        return {"ok": False, "error": "录制名非法（仅字母数字-_，≤64）"}
    stype = step.get("type", "")
    if stype not in ("tool", "cmd"):
        return {"ok": False, "error": "type 必须是 tool 或 cmd"}
    rec = {"ts": time.time(), "type": stype}
    if stype == "tool":
        tool = str(step.get("tool", "")).strip()
        if not tool:
            return {"ok": False, "error": "tool 步骤缺少 tool 字段"}
        rec["tool"] = tool
        rec["args"] = step.get("args") or {}
    else:
        cmd = str(step.get("cmd", "")).strip()
        if not cmd:
            return {"ok": False, "error": "cmd 步骤缺少 cmd 字段"}
        rec["cmd"] = cmd
        rec["cwd"] = str(step.get("cwd", "") or "")
        rec["timeout"] = int(step.get("timeout", 60) or 60)
        if step.get("authorized") or (step.get("args") or {}).get("__authorized"):
            rec["authorized"] = True  # 显式授权透传（L4 语义）
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "replay": name, "recorded": rec,
            "total": sum(1 for _ in open(p, encoding="utf-8"))}


def _load_steps(name: str) -> tuple[list, str | None]:
    p = _path_for(name)
    if p is None:
        return [], "录制名非法"
    if not os.path.exists(p):
        return [], f"录制不存在: {name}（先 replay_record）"
    steps = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    steps.append(json.loads(line))
                except ValueError:
                    pass
    return steps, None


def replay_run(name: str, stop_on_fail: bool = True) -> dict:
    """逐条重放。工具步骤走 server._call；cmd 步骤需 __authorized。"""
    steps, err = _load_steps(name)
    if err:
        return {"ok": False, "error": err}
    if not steps:
        return {"ok": False, "error": f"录制为空: {name}"}
    results = []
    ok_all = True
    for i, step in enumerate(steps, 1):
        t0 = time.perf_counter()
        entry: dict = {"step": i, "type": step.get("type"),
                       "ts": step.get("ts", 0)}
        if step.get("type") == "tool":
            tool = step.get("tool", "")
            args = step.get("args") or {}
            entry["tool"] = tool
            try:
                import server
                out = server._call(tool, args)
                text = out[0].text if out else ""
                entry["ok"] = True
                entry["result"] = text[:200]
                # 结果含 Error → 视为复现失败点（偶现变必现）
                if text.startswith("Error:"):
                    entry["ok"] = False
                    entry["error"] = text[:200]
                    ok_all = False
            except Exception as e:  # noqa: BLE001
                entry["ok"] = False
                entry["error"] = str(e)[:200]
                ok_all = False
        elif step.get("type") == "cmd":
            if not (step.get("args") or {}).get("__authorized") and \
                    not str(step.get("authorized", "")).lower() in ("1", "true"):
                entry["ok"] = False
                entry["skipped"] = "cmd 步骤需显式授权（args.__authorized=true）"
                ok_all = False if stop_on_fail else ok_all
            else:
                try:
                    r = subprocess.run(
                        str(step.get("cmd", "")), shell=True,
                        cwd=step.get("cwd") or None,
                        capture_output=True, text=True, timeout=60,
                        encoding="utf-8", errors="replace")
                    entry["ok"] = r.returncode == 0
                    entry["returncode"] = r.returncode
                    entry["output"] = (r.stdout + r.stderr)[:200]
                    if r.returncode != 0:
                        ok_all = False
                except subprocess.TimeoutExpired:
                    entry["ok"] = False
                    entry["error"] = "超时 60s"
                    ok_all = False
        else:
            entry["ok"] = False
            entry["error"] = f"未知步骤类型: {step.get('type')}"
            ok_all = False
        entry["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        results.append(entry)
        if stop_on_fail and not entry["ok"]:
            break
    return {
        "ok": ok_all, "replay": name, "steps": len(steps),
        "results": results,
        "failed_at": next((r["step"] for r in results if not r["ok"]), None),
        "hint": ("failed_at 即复现点——偶现变必现；对失败步骤用 "
                 "failure_analyze/bug_bisect 定位根因"),
    }


def replay_list() -> dict:
    """列出所有录制（名称 + 步数 + 最近时间）。"""
    d = replays_dir()
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".jsonl"):
                p = os.path.join(d, fn)
                try:
                    n = sum(1 for _ in open(p, encoding="utf-8"))
                    mtime = os.path.getmtime(p)
                except OSError:
                    n, mtime = 0, 0
                out.append({"name": fn[:-6], "steps": n,
                            "updated": time.strftime("%Y-%m-%d %H:%M",
                                                     time.localtime(mtime))})
    return {"ok": True, "replays": out}


if __name__ == "__main__":  # CLI 调试入口
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        print(json.dumps(replay_list(), ensure_ascii=False, indent=1))
    elif len(sys.argv) > 2 and sys.argv[1] == "run":
        print(json.dumps(replay_run(sys.argv[2]), ensure_ascii=False, indent=1))
    else:
        print("用法: replay_core.py list | run <name>")


# ── 兼容：旧模块名 import 无缝映射到本引擎 ──
import sys as _sys
_sys.modules.setdefault('patch_learn', _sys.modules[__name__])
_sys.modules.setdefault('differentiable_code', _sys.modules[__name__])
_sys.modules.setdefault('explore_engine', _sys.modules[__name__])
_sys.modules.setdefault('distill_pipeline', _sys.modules[__name__])
_sys.modules.setdefault('quality_engine', _sys.modules[__name__])
_sys.modules.setdefault('failure_analyze', _sys.modules[__name__])
_sys.modules.setdefault('mini_bert_tokenizer', _sys.modules[__name__])
_sys.modules.setdefault('replay_core', _sys.modules[__name__])
