# NAMERES —— 名字解析（栈图式）设计轮（S106）

> 目标：把"引用 → 定义"从**文本匹配**升级为**语法级绑定解析**——不依赖 LSP、
> 不需要构建、可文件级增量。这是 ADVANCES 雷达 P2 第 7 项（最高杠杆）的设计轮，
> 本轮**只出设计**，实现拆到 S107/S108。
> 红线不变：Rust `[dependencies]` 恒空；每轮 pytest + cargo test 双绿；沙盒语义两侧等价。

## 一、为什么（现状与缺口）

| 现状 | 缺口 |
|---|---|
| `dep_graph`（tools/metrics.py）只解析 **.py 的 import 关系**，且是正则/文本级 | 不知道 import 绑定的**目标文件/行**；`from .m import x as y` 只记名字 |
| `locate_edit` / `ide_impact` 文本降级（S99） | 大小写敏感全文计数——**注释、字符串、同名不同定义全混进来**（skills/search.md 已如实标注） |
| `rust_reach`（astscan.rs，S16）只服务 Rust 裸标识符可达性 | Python 侧无等价物 |
| `ide_lsp` 语义级精确 | 需装 rust-analyzer/pylsp；本机 pylsp 未装（S99 起如实报 `detected=false` 并降级） |
| VULN-HUNTING 附录 B | "路径/数据流查不了"的根因之一就是**没有绑定解析** |

参考：[Stack Graphs（GitHub, EVCS 2023）](https://arxiv.org/abs/2211.01224) 证明
"纯语法 + 文件级增量"的名字解析可以支撑 GitHub 全站导航。本设计取其**思想**
（每文件独立子图 + 查询时拼接），不移植其图算法（作用域图引擎过重）。

## 二、事实模型

每条解析结果是一条**边**（+ 无法解析的清单）：

```
edge = {file, line, name, kind, to_file, to_line}
kind ∈ {local, import, module, builtin}
```

- `local`：函数/lambda/推导式作用域内的绑定（参数、赋值、for/with/except 目标）
- `module`：同文件模块级绑定（def/class/赋值/导入别名）
- `import`：跨文件——`import a.b` / `from m import x` 绑定到目标文件的行
- `builtin`：落到内建（终点无文件；用 `to_file=""` 标记，避免假边）

`unresolved`：无法静态确定的引用（star import、动态特性、属性链）——**如实列出，
不猜**。属性访问 `a.b` 只解析 `a`；`.b` 归入 unresolved 的 `attr_chain` 统计。

## 三、Python 作用域规则清单（逐条必须处理）

基础（LEGB）之外，本仓 pyast.rs 已建模全部相关节点，但下列怪癖必须逐条对：

1. **class body 不构成闭包**：方法内看不到类体变量（`self.x` 除外）。
2. **推导式独立作用域**（3.x）：`[x for x in ...]` 的 `x` 不外泄；但**首个 iterable
   在外层作用域求值**（`[y for y in y_list]` 的 `y_list` 按外层解析）。
3. **lambda 作用域**：参数与推导式同规则。
4. **global / nonlocal**：`names` 字段已有；`nonlocal` 找不到绑定 = SyntaxError（编译期）。
5. **except as 目标在 handler 结束后删除**：`except E as e:` 之后的 `e` 是未绑定
   （3.x 语义）——报 unresolved 还是 local 需定契约（见 §七 决策 3）。
6. **参数默认值 / 装饰器 / 注解在外层求值**：`def f(a=outer)` 的 `outer` 按**定义处
   外层**解析，不是函数内。
7. **del**：`del x` 之后的 `x` 仍按绑定存在（静态解析不做控制流）——如实标注
   `deleted=true` 可选字段？**决策：不加**（保持边简单；控制流不在此层）。
8. **star import**：`from m import *` → 该文件的模块级 unresolved 标记为 `star_import`。
9. **动态特性**：`importlib.import_module`、`getattr`、`exec`、monkeypatch、
   条件定义（`if TYPE_CHECKING:`）→ 静态不可解，如实入 unresolved。
10. **同名遮蔽**：按"最后一次模块级赋值/定义"取绑定（简化：不做控制流顺序），
    并在 stats 里记 `shadowed` 计数。

## 四、算法（简化栈图）

```
阶段 A（单文件，可增量）
  遍历 pyast 树 → 建作用域栈（Module/FunctionDef/AsyncFunctionDef/Lambda/ClassDef/
  ListComp/SetComp/DictComp/GeneratorExp）
  → 收集每个作用域的绑定表 {name: (line, kind)}
  → 每个 Name(Load) 按作用域链上溯解析；未命中且非内建 → unresolved
阶段 B（跨文件，查询时拼接）
  模块级绑定里的 import 别名 → 按 import 形态解析到目标文件：
    import a.b          → a → a/__init__.py 或 a.py（按存在性）
    from m import x     → x → m 文件里的 def/class/赋值行
    from .m import x    → 相对包解析（需 __init__.py 存在性判定）
    from m import x as y → 绑定名 y，目标仍是 m:x
  解析不到（命名空间包/条件导入）→ unresolved
阶段 C（输出）
  边按 (file, line, name) 稳定排序；stats 统计各 kind 计数与 unresolved 分类
```

**不做**（明确边界）：类型推断、属性链解析、跨文件控制流、`__all__` re-export 追踪、
运行时元编程。定位是"比文本级准、比 LSP 轻"，文档必须写清。

## 五、接口与集成

- **实现位置**：`rust/src/nameres.rs` + 子命令 `rx-scan resolve <root> [max_files]`
  （pyast 在 Rust；scan 域已承载分析类工具）。
- **工具面**：**不新增第 59 个工具**——升级 `dep_graph(resolved=true)`：
  返回既有 import 图 + `edges` + `unresolved` + `stats`；默认 false 保持旧形状。
- **第二阶段**（S108 之后）：`ide_impact` 的文本降级升级为"解析级中档"
  （LSP 语义级 → 解析级 → 文本级三级降级，`engine` 字段如实标注）。
- **缓存**：文件级子图按内容哈希缓存（复用 `tools/cache.py` 的 snapshot 思路），
  查询时只拼接受影响的 import 邻域。

## 六、Oracle 与验收（先定判据，再写代码）

1. **symtable 对照**（stdlib，Python 侧测试用）：对单文件，`symtable` 给出每个
   作用域的绑定/自由/全局分类——校验"local vs module vs free"判定。凡分歧逐例
   人工判定并记录（CPython 的 class 体怪癖与我们一致与否）。
2. **手标 fixture**：≥20 个 Python 文件覆盖 §三 全部规则与怪癖（含推导式首 iterable、
   默认值外层求值、except-as 删除、global/nonlocal、star import），逐边断言。
3. **与文本级对比报告**：对本仓跑一遍 `locate_edit` 与 `resolve_refs`，人工抽查
   ≥30 例差异——预期：文本级假命中（注释/字符串）被排除、同名误配被消解。
4. **确定性与增量**：两次运行逐字节相同；改一个文件后仅该文件子图重算（用
   `cache.snapshot` 计数验证）。
5. **性能预算**：本仓（~600 文件）单次解析 ≤300ms（Rust 单进程内）。

## 七、待拍板的三个决策（设计轮给出建议，实现轮执行）

1. **语言范围**：先 **Python only**（pyast 现成；Rust/JS 后续按需）。建议：是。
2. **工具面**：升级 `dep_graph(resolved=true)` vs 新工具。建议：**升级**（工具面
   不再膨胀；旧形状默认不变，零破坏）。
3. **except-as 契约**：`except E as e:` 之后引用 `e` → `unresolved` 还是 `local`？
   建议：**local**（Python 里该名字在作用域内确实被绑定过；报 unresolved 会与
   symtable 的 "local" 判定冲突，反而失真）——在 fixture 里锁死该选择。

## 八、实施拆分

- **S107**：pyast 作用域 pass + 单文件解析 + oracle（symtable + fixture ≥20 例）+
  `rx-scan resolve` 子命令骨架。验收：fixture 全绿、symtable 分歧清单入档。
- **S108**：跨文件拼接（import 形态四种）+ `dep_graph(resolved=true)` 接线 +
  本仓对比报告（≥30 例人工抽查）+ 文档（skills/scan.md 契约、ADVANCES 标已兑）。

## 九、风险与不做

- **风险**：控制流敏感的同名遮蔽（`if/else` 双定义）静态不可判——取"最后一次
  模块级定义"并如实标注；namespace package 与 `sys.path` 魔法解析不到 → unresolved。
- **不做**：类型推断、属性链、跨文件控制流、`__all__` re-export 追踪、运行时元编程；
  不承诺"解析不了 = 不存在"（与 VULN-HUNTING 的诚实口径一致）。
