# 工作流规范：维稳版 / 开发版分离

## 原则（用户要求）

1. **D:\rj\MCP = 维稳版**：其他 AI 对话从这里调工具，只放已测试的 main 分支
2. **D:\开发\unified-rx-mcp = 开发版**：新功能/新实验在 feat/* 分支开发
3. **稳定版优先**：出问题时从稳定版入手操控新版本，而不是反过来
4. **大部分东西本地可跑**：不依赖网络服务（沙盒/构建/测试全本地）

## 流程

```
feat/* 分支开发
  → pytest 全绿（235 passed）
  → PR → main（gh pr merge）
  → git tag vX.Y.Z
  → git push origin main --tags
  → D:\rj\MCP: git pull origin main
```

## 版本规则

- main 分支 = **只放已测试的代码**，不允许直接 push 功能提交
- feat/* 分支 = 开发区，可以随意提交
- 每次合入 main 前：pytest 全绿 + 至少一个新测试覆盖新功能
- tag 格式：v主.次.修

## MCP 宿主接入

其他 AI 对话接入工具箱：
```json
{
  "mcpServers": {
    "unified-rx": {
      "command": "py",
      "args": ["-3.11", "D:\\rj\\MCP\\server.py"],
      "env": {
        "UNIFIED_RX_SANDBOX": "D:\\开发;D:\\rj\\MCP"
      }
    }
  }
}
```

UNIFIED_RX_SANDBOX 值说明：
- `;` 分隔的目录白名单（工具可读写的范围）
- `"*"` = 全开（仅限自检）
- 未设置 = fail-closed 全拒（S43b 修复后缺省安全）

## 维稳版更新操作

```bash
# 在 D:\开发\unified-rx-mcp（开发版）：
git checkout main && git merge feat/xxx && py -3.11 -m pytest -q
git push origin main --tags

# 在 D:\rj\MCP（维稳版）：
git pull origin main
py -3.11 -m pytest tests -q   # 确认
```
