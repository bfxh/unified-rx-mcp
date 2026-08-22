# AGENTS.md — unified-rx-mcp 项目规则（项目级钩子）

> 本文件是**项目级钩子**：任何 AI 工具（Reasonix/Claude Code/Cursor/Qoder 等）
> 打开本项目默认读取。规则先查这里，不靠对话记忆。
> 配套文档：`docs/CHATLOG.md`（聊天记录：决策与规则来源）、`docs/DESIGN.md`（设计文档总索引）。

## 项目规则（按优先级）

1. **写完即验（2026-08-20 升级，用户批评"写代码没检查→大量 bug"）**：每个代码
   单元（函数/模块）**写完立刻**运行 `python scripts/dev_check.py <文件>`——
   语法 + bug_scan + 相关测试 + 语义回归四连；**不许攒一堆再验**（此前"改完
   必跑"是事后检查，bug 早积累）。dev_check 通过才继续下一个单元。
2. **改完必跑语义回归**：整批改动完成后仍跑
   `python scripts/semantic_regression.py`（退出码 0=全过）——pre-push 会拦截，本地先跑更快。
3. **每件事先读文档**：动手前先查 `docs/` 与 `spec/` 相关设计（架构/契约/已知问题），
   不凭记忆写代码——SCAN_QUALITY_ISSUES.md 记录过"噪音淹没信号"教训。
4. **确定性规则必须 error 级**：bug_scan 新增静态 100% 确定的规则用 error，
   不与其他 warning 混级；每条新规则必须配"正例+反例"测试（安全模式不误报）。
5. **能力清单与实现必须一致**：capability_manifest 列出的每个工具必须能被
   `_call` 路由——语义回归 6b 锚点守护（ciopt_ 断链教训）。
6. **文档数字是契约**：工具数/测试数改动同步更新 README/spec/tools.json，
   不一致即视为 bug（157→177 漂移教训）。
7. **零依赖/懒加载哲学**：新功能优先往 `engine/` 引擎加函数，不新建散乱 .py；
   重型依赖按需 import，启动 <100ms。
8. **收尾维护双文档**：本轮重要决策追加 `docs/CHATLOG.md`；设计变更同步 `docs/DESIGN.md`。
9. **隔离验收 + 高压常态化（2026-08-23，用户：自测有盲区，测试必须隔离；检测要加强度常态化）**：
   - MCP 自测必须隔离：`python scripts/isolated_test.py`（spawn 独立进程 +
     JSON-RPC 直连黑盒，不经 AI/网关/stats 打点）——工具变更后必跑，
     契约失败/表实调失败/临界慢工具(>2s) 即拦截
   - 高压基线：`python -m stress_scan <path> <mode> <scale>` 强度拉满（并发
     丢行/大文件/大索引），任一场景 ok=False 必须修复或显式 skip（环境缺失
     不算缺陷，但必须标注）
   - 独立工具性：`python cli.py scan|stats|track|schedule|denoise` 不经模型
     可干活——新工具必须同时提供 CLI 入口，不许只活在 AI 对话里
   - 代码/依赖跟踪：schedule 常驻（索引→扫描→自动开/关 issue）是默认跟踪
     机制；改动代码后依赖方用 predict_impact 校验

## 验证命令（改完必跑，按此顺序）

```bash
python scripts/semantic_regression.py   # 语义回归（快，秒级）
python -m pytest test_unified_rx.py -q  # 单元测试
python server.py --selftest             # 工具自检
python scripts/mcp_smoke.py             # 协议层冒烟
scripts/pre-push.sh                     # 提交前七步全链
```

## 仓库结构速览

- `server.py`：唯一分发入口（`_call`），103 核心工具注册表
- `engine/`：六引擎（scan/ide/learn/locate/index/infra）
- `vendor/extensions/`：76 扩展（cae/pr-oracle/tautest/stats/ciopt）
- `scripts/`：语义回归 / mcp_smoke / pre-push 等
- `docs/`：架构/契约/质量/设计文档 + CHATLOG + DESIGN
- `spec/`：工具契约（MUST/SHOULD）
- `probes/`：契约验证探针

## 提交约定

- 提交信息中文，前缀 `feat:`/`fix:`/`docs:`/`chore:`，说明"改了什么+为什么+验证结果"。
- 推送前本地全链验证（pre-push.sh）；直接 push main（本仓库惯例，无 PR 流程）。
