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

## 十、实现注记（S107 单文件解析已落）

- **代码**：`rust/src/nameres.rs`（作用域栈遍历，~470 行）+ `rx-scan resolve <file>`
  子命令；`bug.rs::BUILTINS` 提为 pub(crate) 复用（160 个内建名）。
- **输出契约**：`{file, edges[{file,line,name,kind,to_line}], unresolved[{line,name,reason}],
  bindings[{scope,parent,name,line,kind}], stats{local,module,builtin,unresolved,
  attr_accesses,star_import}}`；edges/unresolved 按 (line,name) 稳定排序。
  `bindings` 带 `parent`——oracle 用它精确扣除"symtable 把推导式目标算进外层"的差异。
- **oracle 结果**：rust 21 测（十条规则逐条）全绿；python 4 测——含**对本仓
  30+ 真实文件**（registry.py/server.py/urx_tia_plugin.py + 全部 tools/*.py）与
  `symtable` 的逐作用域绑定对照，归一化后**零差异**。
- **两个实锤发现**：
  1. **推导式可见性以运行时为准**：3.14 实测 `[n for n in ...]` 之后 `n` 是
     NameError → 独立作用域判定正确；`symtable` 把推导式目标算进外层局部只是
     静态简化（oracle 按 parent 扣除）。
  2. **pyast 缺口**：不支持"推导式元素内的 walrus"（`[(y := i) for i in ...]`，
     CPython 需括号）——本轮不扩解析器（S83/S84 oracle 锁定），记入边界；
     已支持的 walrus 形态（if/语句级）绑定正确。
- **S108 待办**：跨文件 import 拼接（`import a.b` / `from m import x` / 相对导入 /
  别名）+ `dep_graph(resolved=true)` 接线 + 本仓文本级对比报告（≥30 例人工抽查）。
  注：pyast 目前**丢弃相对导入的前导点**（`from .m import x` 的 level 未记录）——
  S108 需先补 level 记录（存 aux，不改 dump 以保 oracle）。

## 十一、实现注记（S108 跨文件拼接已落）

- **pyast**：`from_stmt` 记录相对导入层级到 `aux`（dump/ast_scan 不消费 aux，
  S83/S84 oracle 不受影响）。
- **跨文件解析**：`nameres::resolve_dir(root, max_files)` + `rx-scan resolvedir`
  子命令——逐文件解析取模块级绑定 → 模块索引（`a/b/__init__.py` 与 `a/b.py` 都
  记 "a.b"）→ 解析四种 import 形态：绝对 `import a.b`、绝对 from、相对 from
  （level 上溯包）、`from pkg import submodule`（子模块回退）。root 自身是包
  （有 `__init__.py`）时模块名带包前缀。外部依赖与内部未找到**分开如实报**
  （external / unresolved，reason=name_not_found/star_import）。
- **工具面**：`dep_graph(path, resolved=true)` 附 `resolved.{imports,external,
  unresolved,stats}`；默认 false 时输出与旧版逐字段同形（零破坏）；exe 缺失 →
  `resolved.error` 显式报，基础图不受影响。
- **验收③ 对比报告**（bench/s108_resolve_compare.py，留档
  bench/results/s108_resolve_compare.json）：本仓 tools/ 包 **30 个符号**逐一对比
  ——解析级引用 2165 条、文本级命中 35283 条，其中 **33118 条（93.9%）是文本级
  假阳性**（注释/字符串/子串，样例已人工抽查：`os` 命中 "os.path.relpath" 的
  中文注释、`path` 命中注释里的"path 指向"等）；**resolved_only = 0**——解析集合
  是文本集合的精确子集，未发现"解析有而文本无"的遗漏。
- **剩余边界**：属性链（`a.b` 只解析 `a`）、类型推断、跨文件控制流、re-export
  追踪不做；star import/动态特性如实 unresolved。S108 后 ide_impact 的"解析级
  中档"可另起一轮（三级降级：LSP → 解析 → 文本）。
