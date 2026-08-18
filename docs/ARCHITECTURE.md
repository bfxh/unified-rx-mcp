# unified-rx-mcp 架构（2026-08-18 合并版）

> 原则：**大部分特性合并，新技术只需增量添加**。

## 分层结构

```
unified-rx-mcp/
├── server.py            # 入口（工具注册/分发/权限/沙盒——唯一"上帝文件"角色）
├── engine/              # 引擎层（合并后的大模块——技术能力都在这）
│   ├── scan_engine.py   # 扫描引擎（合并 7 个：bug_scan/std_check/ui_check/
│   │                    #   cov_scan/cross_taint/rust_scan/sage_scan）
│   ├── ide_engine.py    # IDE 引擎（合并 7 个：tools/ui/session/commands/
│   │                    #   cache/fusion/rx_ide）
│   ├── learn_engine.py  # 学习引擎（合并 8 个：patch_learn/differentiable/
│   │                    #   explore/distill/quality/failure/tokenizer/replay）
│   ├── locate_engine.py # 定位引擎（合并 3 个：locate_core/causal_debug/lse_client）
│   ├── index_engine.py  # 索引引擎（合并 4 个：cb_index/graph_index/search_core/search_index）
│   ├── infra_engine.py  # 基础设施引擎（合并 5 个：daemon/dashboard/telemetry/storage/backup）
│   └── __init__.py      # 兼容层（旧模块名 → 引擎 re-export；加载顺序 scan→infra→ide→learn）
├── vuln_rules.json      # 模板规则（bug_scan 加载）
├── train_data/          # 训练数据（samples/feedback/learned_rules——gitignore）
├── docs/                # 设计文档
└── test_unified_rx.py   # 测试（161+）
```

## 引擎加载机制（为什么旧代码不用改）

1. `server.py` 顶部预加载三个引擎 → 每个引擎**把自己的旧模块名注册进 `sys.modules`**
   （`sys.modules['bug_scan_core'] = 引擎`）
2. 任何旧 import（`from bug_scan_core import X`）自动命中注册表——**零改动无缝工作**
3. `engine/__init__.py` re-export 公开符号——`import engine` 也可用

## 新增技术规则（用户要求）

- **新技术 → 往对应引擎增量加函数**，不新建零散 `.py` 文件
- 新能力域（非扫描/IDE/学习）→ 新建 `engine/<域>_engine.py`（大模块）
- 每引擎 >2000 行才考虑拆子模块（当前 scan 2592/ide 1908/learn 1869）

## 合并踩坑记录（教训）

| 坑 | 解法 |
|---|---|
| `from __future__` 必须在文件最前 | 拼接时提取置顶 |
| 引擎内交叉 import 自引用 | 引擎顶部提前注册自己到 sys.modules |
| 同名函数覆盖（_iter_py_files/main） | 冲突者后缀改名（_cov/_rx）+ 段内引用同步 |
| 合并后 `__file__` 变化 → 数据文件路径失效 | `_ENGINE_ROOT = dirname(dirname(__file__))` 统一基准 |
| engine/__init__ re-export 顺序 | 先预加载引擎，再 re-export |

## 文件数统计

- 合并前：115 个 .py（34 个散乱小模块）
- 合并后：81 个 .py（34 个并入 6 个引擎 + 兼容层）——**后续每新技术只 +1 个引擎内函数**
- 引擎加载顺序：scan → infra → ide → learn（跨引擎依赖方向——infra 提供 dashboard 符号给 ide）
