# 工作流规范：维稳版 / 开发版分离

## 原则（用户要求）

1. **D:\rj\MCP = 维稳版**：其他 AI 对话从这里调工具，只放已测试的 main 分支
2. **D:\开发\unified-rx-mcp = 开发版**：新功能/新实验在 feat/* 分支开发
3. **稳定版优先**：出问题时从稳定版入手操控新版本，而不是反过来；
   涉及 unified-rx-mcp 本身的检查一律以稳定版 D:\rj\MCP 为准
4. **大部分东西本地可跑**：不依赖网络服务（沙盒/构建/测试全本地）
5. **VS Code = 最后的后手（S68）**：工具链（build/lint/LSP/doctor）查不出或
   需要人工/AI 深查时，`ide_vscode` 把项目/文件/path:line:col 直接打开
   （Code.exe：D:\rj\KF\IDE\Microsoft VS Code\Code.exe）；**多项目联动检查**
   用 `ide_multi_check`（逐项目 doctor 全量汇总，issues 优先，
   `vscode: true` 直接把坏项目丢进编辑器）
6. **默认严苛（S62 固化为规范）**：新代码默认按最严格语义写，不靠事后补——
   - 写/执行类工具一律 `requires_auth=True`（跑编译/跑测试/跑程序=任意代码执行）
   - 工具返回 dict 含 `"error"` 键 ⇒ registry 恒转 ok:false（调用方只看 ok 一个字段）
   - 输入输出双侧尺寸上限（registry 入口 2MB 字符串/1 万项列表；server 协议行
     64MB；LSP 入站帧 64MB）
   - 落盘一律原子写（tmp+os.replace），不留半截文件
   - 路径过 `_fs_resolve`（realpath 沙盒包含性，symlink/junction 逃逸默认死）
   - 新增静默 except 前先问：吞掉的 NameError/契约错会不会让信号全空？
     （S55/S60 四次同源事故的教训——宁可报错，不许静默降级）

## 流程

```
feat/* 分支开发
  → pytest 全绿（v2.4 起基线 330+ passed）
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
