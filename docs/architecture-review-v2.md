# unified-rx 架构审查 v2（真实诊断基线）

> 2026-08-15 · arch-optimize v3.1 脚本实测（非纸面审查）
> 目标：unified-rx（server.py 6252 行 / 358KB——上帝文件）

---

## 一、诊断基线（真实数字）

| 指标 | 值 | 判定 |
|---|---|---|
| arch_scan | 677 文件 / 91 目录 / 17 模块 | — |
| risk_diagnose | **334 findings · health 0/危险** | 148 Critical + 186 Warning |
| server.py 单文件 | **81 findings（51 Critical）** | 重灾区实锤 |
| detect_code_ai | **1148 findings · gate_passed false** | 46 Blocker + 716 Critical + 386 Warning |

### 圈复杂度重灾区（R1/R4 Critical——真实）

| 文件:函数 | CC | 阈值 |
|---|---|---|
| ui_check_core.py:scan_ui_source:49 | **33** | >20 |
| ui_check_core.py:scan_ui_dir:204 | **26** | >20 |
| server.py 全文件 | 51 条 Critical | 含 R1/R4 群 |

---

## 二、AI 味检测真伪判定（detect_code_ai 工具级缺陷记录）

| 模式 | 数量 | 判定 | 说明 |
|---|---|---|---|
| DEAD-001 未使用函数 | 560 | **100% 工具误报** | 只做文件内分析——repo_status/scan_repo/add_note 均被 server 跨文件调用（抽查 15/15 全误报） |
| DEAD-008 except pass 吞错 | 95 | **真实——待修** | 静默吞异常（部分已加"尽力而为"注释仍命中） |
| DEAD-003 假实现 | 46 | 部分真实 | _vector_search 是文档化接口扩展点（return [] 降级）→ 误报；"假数据"是检测词表注释词 → 误报；lse-engine 空 impl 是 Rust trait 默认 → 合法；test call_fn 空体是测试辅助 → 合理 |
| DEAD-002 TODO | 61 | 正常标记 | TODO 是工程常态（游戏检查还主动统计） |
| DEAD-005/010/006 | 470 | 风格噪音 | 'data' 命名/JSON schema camelCase/静态类——非 AI 味 |

**结论**：detect_code_ai 默认模式对 unified-rx 误报率极高（≥85%）——
工具需改进（跨文件引用分析/词表上下文），**不修代码迎合误报**。
**真实待修：DEAD-008（except pass ×95）**。

---

## 三、整改执行（本次交付）

### R2-R3：拆上帝文件（server.py 6252 行 → 薄协议层 + tools 域包）
- 按域拆：bug/scan/locate/guard/cb/ds/ui/fs/lse/game/ide
- 注册表模式：注册=声明，schema 自动生成
- 渐进式：每拆一域 → pytest + mcp_smoke + tool_ratchet 验证
- 验收：server.py < 1000 行、全量测试绿、工具数零变化

### 反 AI 味真实修复
- DEAD-008（except pass ×95）→ 逐处补"尽力而为"注释或记录

### 回归防护
- regression_guard record 基线 → 重构后 compare（零退化率 100%）

---

## 四、遗留（工具级缺陷——待 arch-optimize 自身整改）

1. detect_code_ai DEAD-001：需跨文件引用分析（现文件内）
2. detect_code_ai DEAD-003："假数据"等词需上下文判断（现裸词匹配）
3. risk_diagnose health_score=0：需确认评分公式（148 Critical 时 0 分合理但应输出明细）
