# CALLGRAPH —— 真调用图（S125）

> 目标：把"谁调谁"从文本/骨架猜测升级为**语法级调用边**——符号级
> `调用点(file:line) → 定义`，不依赖 LSP、不需要构建、可目录级单次产出。
> 承接 NAMERES（S106-S108）的同一作用域引擎，是其上层的调用关系视图。
> 红线不变：Rust `[dependencies]` 恒空；每轮 pytest + cargo test 双绿；沙盒语义两侧等价。

## 一、为什么（缺口与现状）

| 现状 | 缺口 |
|---|---|
| `repo_map`（S102）是 PageRank 骨架（"该看哪些定义"）| 不是精确调用边，回答不了"谁调用了 X" |
| `ide_impact`（S58/S109）三级降级 | 解析级给出的是**引用**（谁 import/引用了这个定义），调用关系要人肉再筛 |
| `dep_graph`（S108 resolved） | 文件级 import 边，非符号级 |
| `ide_dead_code`（S123） | 零引用口径 ≠ 零调用；调用图是更精确的可达性下界 |
| 模块化架构（用户主诉） | 环检出/扇入扇出是"上帝对象拆分"的客观输入 |

## 二、事实模型

节点 = def/class 定义（**全限定名**：`模块.类.方法.嵌套`，模块名按 resolve_dir
口径含包前缀）；边 = 调用点：

```
edge = {file, line, caller, callee, to_file, to_line, kind}
kind ∈ {name, self_attr, module_attr, from_import, class}
caller = 定义全限定名；模块级代码块 caller = ""（伪节点）
```

`unresolved`（如实列出，不猜）：`{file, line, caller, expr, reason}`，
reason ∈ `external`（外部依赖，含 `os.path.join` 这类链式）· `attr_chain`
（内部模块链式调用/`a.b.c()`）· `receiver_var`（对普通变量取属性调）·
`var_call`（对变量或模块对象裸调）· `self_attr_missing`（实例属性静态不可见）·
`re_export`（只再导出不追踪）· `star_import` · `not_found` · `expr`（非常规 callee）。

**stats 自洽**：`calls = resolved + unresolved + builtin_calls`（Rust 测试锁死；
内建调用单列不进 unresolved，因为它是可解析的事实、只是没有内部节点）。

## 三、算法（两阶段 + stitch，与 resolve_dir 同构）

```
阶段 1 预扫描（新）：逐文件只扫 def/class/import/赋值目标（不下函数体）
  → 模块级绑定表 + 类作用域绑定表（label → 名字 → (行, kind)）+ import 事实
阶段 2 主遍历（复用 Resolver 走查）：模块/类作用域以预扫描结果**种子化**；
  进入调用点时按当前作用域链解析：
    Name 调用 → 本地/本模块 def/class（前向引用可解，种子保证）/ import → 待定
    Attribute 调用 → self.m()（基名是方法参数且父作用域是类 → 查类表）
                    C.m()（基名绑到 class → 查类表）
                    m.f()（基名绑到 import → 待定 + 记 base 供子模块回退）
                    其余 → receiver_var / attr_chain（链根是 import 时待定）
  stitch：待定项查模块索引——命中绑定即出边（含 from-import 别名、相对导入、
  子模块回退 `from pkg import sub; sub.f()`）；不命中按 external/re_export/
  name_not_found/attr_missing 如实分类
```

**预扫描种子**是相对 nameres 单文件解析的关键增量：模块级/类级的前向引用
（互递归、方法定义在调用之后……）都能解析——这是调用图的第一性需求。

## 四、不做（明确边界）

- **函数体内局部定义的互递归**（顺序绑定：`a` 里调尚未定义的 `b` → not_found）——
  函数体不做预扫描（代价/收益比不合格）；Rust 测试 `local_mutual_recursion_is_documented_boundary` 锁死；
- 类型推断（`x = get_obj(); x.method()` → receiver_var）；
- 属性链（`pkg.leaf.f()` 记 attr_chain；`f()()`、`super().m()` 同理）；
- 跨文件控制流、`__all__` re-export 追踪、运行时元编程（与 NAMERES §九 同边界）；
- 不承诺"解析不了 = 不存在"。

## 五、接口与集成

- **实现**：`rust/src/nameres.rs`（同一作用域引擎扩展）+ `rx-scan callgraph <root> [max_files]`
  （默认 300，与 resolvedir 同口径）；
- **工具面**：`ide_callgraph`（ide 域 22nd）——薄壳转调 + 查询层（本仓惯例：
  抽取在 Rust、形状在 Python）：
  - 汇总模式（不传 symbol）：stats + top fan-in/out + 环检出（迭代 DFS，规范形去重，
    ≤10）+ resolution_rate；
  - 查询模式：短名消歧（多命中给候选不猜）→ callers/callees 有界 BFS（depth 1-8、
    边 400 上限置 truncated）；
- **未接线（下轮候选）**：`ide_impact` 调用面补充（引用 vs 调用的语义分层不混）、
  `ide_dead_code` 加"零调用"辅助证据。

## 六、验收（S125 实测）

- Rust `rust/tests/callgraph_test.rs` **16/16**：前向引用、互递归双向、self 方法
  （定义在后）、类名调用、self_attr 缺失如实、跨文件 from-import/相对导入、
  子模块回退 vs 链式边界、external/builtin 分类、var_call/receiver_var、
  re_export、嵌套局部顺序绑定、局部互递归边界、star_import、语法错误跳过、
  逐字节确定性、stats 自洽；
- Python `tests/test_s125_callgraph.py` **10/10**：stats 自洽、callers/callees 深度
  语义、歧义候选、not_found 包络、环 + self 方法 + reason 面、沙盒拒绝、
  直连包装确定性、exe 缺失清晰错误、真仓冒烟（`tools.fs._resolve` 调用者 ≥5）；
- 未解析率参考（本仓 tools/ 30 文件）：resolved 421 / unresolved 1653 /
  builtin 608——receiver_var 与 external 是主要项（无类型推断 + 外部依赖），
  **数字如实入档，不美化**。
