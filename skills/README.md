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
| fs | [fs.md](fs.md) | 4 | 沙盒 _fs_resolve，空 roots=全拒 |
| search | [search.md](search.md) | 2 | BM25 文件级 + tf-idf 符号级，均非嵌入 |
| scan | [scan.md](scan.md) | 6 | 正则+AST-lite，非编译器语义 |
| ide | [ide.md](ide.md) | 8 | LSP 仅 2 语言；build/debug/break 走真实工具链 |
| guard | [guard.md](guard.md) | 2 | 路径真值校验，非语义理解 |
| meta | [meta.md](meta.md) | 2 | local_run 需 __authorized |
| ops | [ops.md](ops.md) | 5 | stats.jsonl 打点 |
| attack | [attack.md](attack.md) | 3 | 自写对抗，非 hypothesis |
| appaudit | [appaudit.md](appaudit.md) | 3 | Electron asar 解包审计 |
| engine | [engine.md](engine.md) | 2 | 引擎桥接 |
| game | [game.md](game.md) | 2 | Blender/游戏资产校验 |
| learn | [learn.md](learn.md) | 1 | lesson 关键词检索，非向量 |

## 语言级索引（ide 域）

[python](lang/python.md) · [rust](lang/rust.md) · [java](lang/java.md) ·
[go](lang/go.md) · [c](lang/c.md) · [cpp](lang/cpp.md)

每个语言文档必含：构建命令、诊断强制项（本地化/编码）、调试后端、已知坑。
