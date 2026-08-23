# unified-rx-v2 ROADMAP

> 定位：本地工具代替智能体体力活的平台。方向已由用户确认（2026-08-24）：
> ①修环境 ②按"每类事 1~2 工具"收敛 ③单点接开源最强 ④保留独有（游戏扫描/防幻觉/Blender/全流程）。

## ✅ 第一批完成（2026-08-24）

| 项 | 状态 |
|---|---|
| 旧库备份 | `D:\开发\backups\unified-rx-mcp-20260824-040352.zip`（4836 文件/109MB） |
| fs_write 授权直传 | ✅（__authorized 直传，MCP 层实测） |
| 工具收敛 183→34 | ✅ 12 域，注入面精简 5× |
| 协议薄层 | ✅ server.py 纯 stdlib 零依赖（<300 行） |
| ide_edit_multi 0 应用 | ✅ 内容匹配替代行号匹配 |
| 全量测试 | ✅ 24/24 通过 |
| MCP 协议联通 | ✅ initialize/list/call/授权/沙盒/未知工具 |
| config.yaml 切换 | ✅ unified-rx → v2 + 沙盒 env（备份 .bak-v2-20260824-045453） |

## ✅ 第二批完成（P2 + P3，2026-08-24）

### P2 codegraph 引擎接入（单点接开源最强）
| 项 | 状态 |
|---|---|
| codegraph 探测 | ✅ `@colbymchenry/codegraph` v1.5.0（MIT），`D:\rj\AI\Yan Agent\resources\codegraph-runtime` |
| VoxelForge init | ✅ 3 秒 / 69 文件 → 1571 节点 / 4412 边 |
| engine_query 真接入 | ✅ 优先 codegraph CLI（语义），无命中自动降级 BM25 |
| BM25 vs codegraph 对比 | ✅ codegraph 语义碾压（符号级定位+签名）；概念类查询降级 BM25 互补 |
| **codegraph MCP 原生接入 Hermes** | ✅ config.yaml 新增 `codegraph` server（`node.exe codegraph.js serve --mcp`），重启生效 |
| codegraph_explore 实测 | ✅ 一条调用返回源码+调用链+爆炸半径+测试覆盖警告（顶级设计） |

### P3 工具增强
| 项 | 状态 |
|---|---|
| bug_scan 补 Rust 生产规则 | ✅ unwrap/expect/panic/unreachable/todo/as_cast/indexing，分级 high/medium/low，tests/ 目录自动降级 |
| ui_check 补 Godot/Unity | ✅ 三引擎（Bevy/Godot/Unity）死按钮模式 |
| blender_verify 补 Umi-OCR | ✅ HTTP API + CLI 双通道读界面文字 |
| local_run git 中文 workdir | ✅ v2 的 local_run 参数校验只限命令字符串，workdir 传中文路径原生支持（旧版坑已消） |

## 🔜 下一步（P4 远期 + 收尾）

- [ ] **重启 Hermes 验证双 MCP 注入**（unified-rx v2 34 工具 + codegraph_explore）——用户重启后确认
- [ ] codegraph 对其他项目 init（VoxelForge-V3 / unified-rx-v2 自己）
- [ ] 本地嵌入模型（bge-small-zh → code_search 向量化，≤2GB 显存约束）
- [ ] Qwen3-VL 2B 视觉（可选，~1.5G）

## 设计原则（不改）

1. **少而准**：工具是能力不是噪声；新工具必须替代旧工具
2. **纯 stdlib**：零依赖，任何 Python 环境可跑
3. **写文件通道必须可靠**：fs_write 授权直传
4. **单点接开源最强**：语义引擎不自研（codegraph 已接入）
5. **写完即验**：24/24 是门槛，不许倒退
