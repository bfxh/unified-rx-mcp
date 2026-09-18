# skills/ — 对外 skill 文档（MCP 域级 + 语言级）

原则（用户要求固化的规矩）：

1. **MCP 对外的每个域必须有 skill 文档**（CI 门禁 `tests/test_manifest_gate.py`
   强制：新域没文档 = CI 红）。
2. **每一个语言必须有语言级 skill**（ide 域声明支持的语言 → `skills/lang/*.md`）。
3. 文档写**里子**不写宣传：每个工具的真实机制、已知坑、强制项。
4. **会话记录规范**：每轮工作的推导/决策/证据记入 `spec/ROUNDLOG.md`
   （`bench/log_round.py` 追加），跨项目通用。

## 域级索引

| 域 | 文档 | 工具 | 里子要点 |
|---|---|---|---|
| fs | [fs.md](fs.md) | 4 | 沙盒 _fs_resolve，空 roots=全拒；读面纯 Python（S95） |
| search | [search.md](search.md) | 3 | BM25 文件级 + tf-idf 符号级 + repo_map 个人化 PageRank，均非嵌入 |
| scan | [scan.md](scan.md) | 13 | 正则+AST-lite，非编译器语义；覆盖矩阵见 VULN-HUNTING 附录 B；含知识库与可选引擎 |
| ide | [ide.md](ide.md) | 23 | LSP 仅 2 语言（缺失如实报 + impact 文本降级，S99）；build/debug/break 走真实工具链；S125 调用图（nameres 同引擎）；S129 impact 调用面 + 风险榜；lsp 客户端/动作/影响面三分 |
| guard | [guard.md](guard.md) | 4 | 路径真值校验，非语义理解；读取过沙盒（S97） |
| meta | [meta.md](meta.md) | 3 |
| metrics | [metrics.md](metrics.md) | 3 | S136 组轴归位（模块 metrics.py 与组对齐）：覆盖率/依赖图/稳定性 | local_run 需 __authorized；S122 熔断状态/复位 |
| ops | [ops.md](ops.md) | 7 | stats.jsonl 打点 + S141 会话烧量 + S149 渐进披露（profile_status/profile_enable） |
| attack | [attack.md](attack.md) | 6 | 自写对抗，非 hypothesis |
| appaudit | [appaudit.md](appaudit.md) | 3 | Electron asar 解包审计 |
| engine | [engine.md](engine.md) | 2 | 引擎桥接 |
| game | [game.md](game.md) | 2 | Blender/游戏资产校验 |
| learn | [learn.md](learn.md) | 1 | lesson 关键词检索，非向量 |
| sys | [sys.md](sys.md) | 6 | S148 混合架构：EfficiencyClass 双 API 交叉判 P/E（非混合如实报 uniform）；steer=CPU Set 软定向 + EcoQoS（`hard` 才是硬亲和，Intel 劝阻） |

## 语言级索引（ide 域）

[python](lang/python.md) · [rust](lang/rust.md) · [java](lang/java.md) ·
[go](lang/go.md) · [c](lang/c.md) · [cpp](lang/cpp.md)

每个语言文档必含：构建命令、诊断强制项（本地化/编码）、调试后端、已知坑。
