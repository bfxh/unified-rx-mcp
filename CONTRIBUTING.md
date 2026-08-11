# CONTRIBUTING — 变更流程（必须严格执行）

> 本文件定义本仓库所有变更的**强制流程**。任何改动（代码/文档/CI/workflow）都必须
> 走完以下 7 步，缺一不可。绕过流程的变更 = 违规（review 发现即打回）。

## 流程总览（7 步，顺序执行）

```
① 规划  → ② 分支  → ③ 实现  → ④ 验证  → ⑤ 审查  → ⑥ 漏洞扫描  → ⑦ PR/推送
```

### ① 规划（写进 PR 描述）
- 明确：改什么 / 为什么改 / 影响面 / 验收标准
- 涉及多文件/多工具的变更必须先在 issue 或 PR 描述里列清单

### ② 分支
- 代码改动：`feat/<name>` 或 `fix/<name>` 分支（禁止直接改 main）
- 文档/CI 改动：同样走分支（用户明确要求"整个东西都要严格"）
- 例外：紧急安全修复可热修，但必须事后补 PR 记录

### ③ 实现
- 单文件小改：`edit_file`（精确替换）
- 多文件/新增：先写设计（核心思路），再实现
- 新文件必须带 SPDX 头（`SPDX-FileCopyrightText: 2026 bfxh` + `SPDX-License-Identifier: MIT`）
- 新增/修改功能必须有对应测试（pytest 或 Rust test）

### ④ 验证（本地，必须全绿）
```bash
python -m pytest test_unified_rx.py -q      # Python 全量测试
cargo test --release                        # Rust（lse-engine）测试
python server.py --selftest                 # 工具自检
python scripts/tool_ratchet.py --check      # 工具清单棘轮（改工具必须过）
python scripts/mcp_smoke.py                 # 真实 stdio 协议冒烟
reuse lint                                  # REUSE 合规（新文件必须有头）
git diff --check                            # 无空白错误
```

### ⑤ 审查
- 用 `review`（常规）/ `security_review`（涉及输入/文件/网络/密钥）内置 playbook 审查 diff
- review 的 blocking 必须全部修复后才进下一步；should-fix 尽量修

### ⑥ 漏洞扫描（常态，不依赖提示词）
```bash
node C:\Users\lbx13\AppData\Roaming\reasonix\global-workspace\scripts\vuln-scan.mjs   # 本地多语言静态扫描
# 或仓库内工具扫描：python -m pytest 前的 bug_scan/std_check 等（结果自动落盘 scan-log）
```
- 扫描类工具（bug_scan/std_check/vuln_scan/ui_check/project_scan）调用结果自动落盘
  `~/.unified-rx/scan-log.jsonl`——收尾汇报必须包含"本轮漏洞扫描结果"小节
- 发现的问题必须修复或明确标注"确认为误报/低风险"并附证据

### ⑦ PR/推送
- 本仓库（bfxh/unified-rx-mcp 等自有仓库）：分支 → PR → 合并（不直接推 main）
- fork（bfxh/DeepSeek-Reasonix）：分支 → PR 到上游
- PR 描述包含：①规划内容 + ④验证结果 + ⑥扫描结果

## 强制约定

1. **不做无流程变更**：任何改动先过 ①规划，写明验收标准
2. **不绕过验证**：④验证有任一红，禁止进 ⑤⑥⑦
3. **不提交未审查代码**：⑤审查的 blocking 未清，禁止推送
4. **新工具必须过棘轮**：改 `_TOOLS`/`tools.json` 后 `tool_ratchet --check` 必须 OK
5. **密钥零硬编码**：GitHub push 保护会拦——token 一律走环境变量/gh keyring
6. **删除文件走流程**：删 workflow/文件前先确认影响（如引用它的 alerts/文档）
7. **迁移（如 Python→Rust）走专项流程**：先出迁移方案文档（规模/分期/风险）→
   评审通过 → 分期实施（每期独立 PR，保持可运行）

## 仓库现状速查

- Python 主实现：`server.py`（2594 行）+ core 文件（guard/std/locate/cb/ds/ui/scan_log/lse_client）
- Rust：`lse-engine/`（lib.rs 1041 行）——教训引擎已 Rust
- 测试：`test_unified_rx.py`（108 个 pytest）+ `lse-engine`（14 个 Rust test）
- CI：`.github/workflows/`（30 个有效 workflow，含 security-matrix 100+ 项）
