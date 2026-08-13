#!/usr/bin/env python3
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
