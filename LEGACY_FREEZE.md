# 旧版 unified-rx-mcp 停用说明（2026-08-24）

## 现状

| 版本 | 路径 | 状态 |
|---|---|---|
| **v2（新）** | `D:\开发\unified-rx-v2` | ✅ 生效（config.yaml 已指向） |
| v1（旧） | `D:\开发\unified-rx-mcp` | 🔒 冻结归档 |
| E 盘旧副本 | `E:\共享\51\unified-rx` | 🔒 冻结（不再同步） |

## 为什么停用旧版

1. **工具面爆炸**：183 工具（实际注入 200+）→ AI 上下文污染
2. **上帝文件**：server.py 7462 行，改一个工具动主文件
3. **环境自伤**：fs_write 授权剥离写不了文件、ide_edit_multi 0 应用、python 卡死
4. **功能重复**：5 套检索并存、文档 40+ 份

## 停用方式（已做）

- `config.yaml` 的 `mcp_servers.unified-rx` args 已指向 `D:\开发\unified-rx-v2\server.py`
- 旧库已备份：`D:\开发\backups\unified-rx-mcp-20260824-040352.zip`（4836 文件/109MB）
- config 备份：`config.yaml.bak-v2-20260824-045453` / `config.yaml.bak-cg-20260824-051423`

## 重启 Hermes 后的预期

```
mcp_servers:
  unified-rx:   → v2（34 组合工具）
  codegraph:    → codegraph_explore（语义引擎）
```

## 回滚（如 v2 有问题）

1. 用备份恢复 config.yaml：`config.yaml.bak-v2-20260824-045453` → config.yaml
2. 重启 Hermes

## 旧版还有价值的东西（已提炼进 v2）

- 防幻觉闭环（hallucination_guard 三分级）→ v2 guard 域
- 多语言扫描（bug_scan/std_check/ui_check）→ v2 scan 域（增强）
- 教训引擎（lesson_recall）→ v2 learn 域
- 写完即验（dev_check 四连）→ v2 测试门槛
- codegraph（Yan Agent 内核）→ v2 engine 域 + 原生 MCP 接入
