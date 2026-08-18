# DESIGN — unified-rx-mcp 设计文档总索引

> 本文件是项目"设计文档"入口（用户规则：任何项目至少两个文档——聊天记录 + 设计文档）。
> 设计细节按主题分布在各专项文档，本文件提供总览、定位与导航，避免重复造内容。
> 与 CHATLOG.md（决策/规则来源）配套阅读：DESIGN 讲"是什么"，CHATLOG 讲"为什么"。

## 一句话定位

unified-rx 是**工具集，不是智能体**：一个 MCP 入口（177 工具 = 101 核心 + 76 扩展），
产出证据与事实，不替代 LLM 推理。单文件 server.py + 引擎层 + vendor 扩展，
纯本地零费用，适配 Reasonix 及所有标准 MCP 客户端。

## 架构总览

- `server.py`：入口（工具注册/分发/权限/沙盒——唯一分发点 `_call`）
- `engine/`：引擎层（scan/ide/learn/locate/index/infra 六引擎，旧模块名注册进
  sys.modules 无缝兼容）
- `vendor/extensions/`：扩展（cae 13 / pr-oracle 3 / tautest 4 / stats 4 / ciopt 52）
- `rx-core` / `rx-search` / `rx-net` / `rx-telemetry` / `lse-engine`：Rust 加速族

详细架构：`docs/ARCHITECTURE.md`

## 核心机制

| 机制 | 文档 |
|---|---|
| MCP 接口小总（被调用状态：分发/落盘/耦合/反馈） | `docs/MCP_INTERFACE.md` |
| 扫描质量与信噪比（噪音淹没信号问题 + 演进记录） | `docs/SCAN_QUALITY_ISSUES.md` |
| 防幻觉三层（guard 三分级 / capability_manifest / 诚实标注） | README 防幻觉章节 + `spec/05_guard.md` |
| 工具契约（MUST/SHOULD） | `spec/00_overview.md` ~ `spec/07_net_chaos.md` |
| 多智能体兼容矩阵 | `docs/AGENT_COMPAT.md` |
| 用户设计偏好（长期默认规则） | `docs/DESIGN_PREFERENCES.md` |
| 工具梳理与调用统计 | `docs/TOOL_INVENTORY.md` |
| 价值矩阵（工具对 RX 的实际价值评级） | `docs/RX_EFFECTIVENESS.md` |

## 扫描工具设计（bug_scan / std_check / ui_check）

- bug_scan：Python AST 五类规则 + 多语言轻量文本规则；确定性规则一律 error 级：
  `x[len(x)]` 恒越界、负索引字面量越界（UnaryOp）、变量零分母（z=0 后 /z）、
  字面量除零/越界；近似规则（undefined_name/none_deref/resource_leak）warning。
- std_check：占位文字/命名冲突/UI 硬编码/魔法数字（全大写常量定义豁免）/
  密钥检测（Critical）。
- 结果带 `severity_counts` + `noise_ratio`——AI 一眼判断报告可信度。
- 设计细节：`BUG_SCAN_DESIGN.md`、`spec/01_bug_scan.md`、`spec/02_std_check.md`

## 质量保障（改完必跑）

```bash
python scripts/semantic_regression.py   # 语义回归 122+ 锚点（pre-push 第 1 步）
python -m pytest test_unified_rx.py -q  # 单元测试 163+
python server.py --selftest             # 工具自检
python scripts/mcp_smoke.py             # 协议层冒烟（177 tools）
scripts/pre-push.sh                     # 七步全链（语义回归→pytest→cargo→smoke→ratchet→async_guard→sync_check）
```

## 已知问题与决策记录（DR）

- 2026-08-19：ciopt_ 分发断链修复（能力清单幻觉）→ 语义回归 6b 锚点防复发。
- 2026-08-19：guard Windows 绝对路径误判修复（盘符提取）。
- 2026-08-19：文档数字漂移修复（157→177），确立"文档数字是契约"原则。
- 历史演进：README changelog（2026-08-11 起逐日条目）+ `docs/OVERALL_PLAN.md`。
