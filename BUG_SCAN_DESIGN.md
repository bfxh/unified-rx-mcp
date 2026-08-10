# unified-rx 增加「扫 bug + 精准定位」能力 —— 设计与 RX 适配

> 目标：在极简单文件 MCP（53 → 55 工具）中内联两个工具：
> `bug_scan`（静态缺陷扫描）与 `bug_locate`（报错 → file:line 精准定位）。
> 约束：纯 Python 零第三方依赖、保持单文件、启动 <100ms、不破坏现有错误契约。

---

## 1. 代码缺陷扫描的极简实现思路（纯 Python 零依赖）

核心结论：**用标准库 `ast` 做一次语法树遍历，而不是自己写词法/正则分析。**
`ast` 天然给出：语法解析、作用域内名称的 Load/Store 语义、每个节点的
`lineno`/`col_offset`。这正好同时满足「扫 bug」和「定位到行」两个诉求。

### 总体架构

```
bug_scan(path, max_files=100)
  ├─ 路径校验（复用 _check_path 沙盒）
  ├─ 收集 .py 文件（目录递归，上限 max_files、单文件 1MB、总行数 200k）
  ├─ ast.parse 每个文件（SyntaxError → 记一条 issue 继续，不中断）
  ├─ 五类规则分析（每类独立、可单独关闭）
  └─ 输出 {"ok", "files", "issue_count", "issues[]"}
```

### 五类规则的极简实现

| 规则 | 检测方式 | 确定性 | 严重级 |
|------|----------|--------|--------|
| `undefined_name` | 作用域栈：每层收集 Store/参数/import/循环/异常目标 → 对 Name(Load) 查白名单(builtins+隐式名) | 近似(warning) | warning |
| `none_deref` | 线性跟踪：`x = None` 后、再次赋值前，x 出现在 `x.attr` / `x[i]` / `x(...)` | 近似(warning) | warning |
| `resource_leak` | `open(...)` 不在 with 中、且无对应 `.close()` 调用 | 近似(warning) | warning |
| `divide_by_zero` | BinOp/AugAssign 的 `/`、`//`、`%` 右操作数是字面量 0（含 `-0`） | 确定(error) | error |
| `index_out_of_range` | 字面量容器(List/Tuple/str) + 字面量索引越界；含变量跟踪（`s=[1,2]` 后 `s[5]`） | 确定(error) | error |

### 关键坑（本实现踩过并修复）

1. **`ast.parse` 不做常量折叠**：`[1, 2]` 是 `List` 节点不是 `Constant(value=[1,2])`。
   字面量容器识别必须分别处理 `ast.List` / `ast.Tuple` / `ast.Constant(str)`。
2. **`iter_child_nodes(FunctionDef)` 会遍历函数体**：模块级 walk 误把函数内引用当
   未定义变量。入口为函数/类时只查装饰器与参数默认值，函数体交给作用域递归。
3. **作用域必须分层**：`ast.walk` 会穿透嵌套函数。用「作用域递归」：
   模块级 → 函数体 → 嵌套函数/类，外层定义集（闭包可见）随 `outer` 参数传递；
   lambda 形参并入 `extra` 集合。
4. **误报控制**：builtins + 常见隐式名（`__file__`/`self`/`cls`）白名单；
   近似类规则一律 severity=warning，确定性规则才 error。

### 为什么零依赖可行

- `ast`/`re`/`builtins` 全是标准库，`import ast` 开销约 5-15ms，实测 selftest
  55 工具仍 **16.7ms**（含扫描抽样 56.7ms），未破坏「启动 <100ms」目标。
- 不引入 pylint/pyflakes/mypy：那会破坏「单文件、零依赖、启动极快」的定位。

---

## 2. 精准定位（报错 → file:line）的最佳实践

### 输入格式支持（两个正则，覆盖 90% 场景）

```python
_TRACEBACK_RE  = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([^\n]+))?')  # traceback
_SIMPLE_POS_RE = re.compile(r'([^\s:"]+\.py):(\d+)(?::(\d+))?')                 # x.py:42 或 x.py:42:5
```

- **优先匹配 traceback**（能带函数名、路径可含空格）；无匹配再回退简洁格式。
- 一个报错文本可能含多层栈帧 → **返回全部 locations 列表**，不只最后一个。
- traceback 正则注意 group 顺序：`(文件)(行号)(函数名)`，函数名是**字符串**，
  不能 int() 强转（本实现初版踩过 `int('worker')` 的坑）。

### 定位输出契约

```json
{
  "ok": true,
  "matched": true,
  "locations": [
    {
      "file": "C:\\repo\\src\\a.py", "line": 42, "col": 0, "func": "worker",
      "status": "ok", "context": ["40: ...", "41: ...", "42: boom()"]
    }
  ]
}
```

### 最佳实践清单

1. **上下文片段带行号前缀**（`"42: boom()"`），±3 行即可直接粘贴回编辑器/提示词。
2. **逐位置容错**：每个 location 独立 `status`（ok/missing/blocked/unreadable），
   一个位置失效不拖垮整条报错解析。
3. **无匹配不是错误**：返回 `matched: false` + 空列表 + 正常 ok:true，
   让调用方能区分「没有定位信息」和「工具失败」。
4. **路径必须过沙盒校验**（复用 `_check_path`），防报错文本里的任意路径被读取。
5. **文件读限复用 `_MAX_READ`（1MB）**，防超大文件把上下文撑爆。

---

## 3. RX（Reasonix / unified-rx 网关）适配契约

### 工具命名

```
bug_scan    —— 前缀分组 bug_*（与 fs_/math_/str_/... 同风格，见 docstring 分类表）
bug_locate
```

### 输出格式

- 与现有 53 工具一致：`list[types.TextContent]`，文本为 **JSON（ensure_ascii=False）**。
- 结构约定：顶层 `ok` 布尔 + 业务字段（`issues`/`locations`）+ 计数。
- 中文消息直接输出（不转义），与 `fs_*`/`json_*` 风格统一。

### 错误契约（关键：与网关完全一致）

| 场景 | 行为 |
|------|------|
| 工具内部异常（路径越界/参数非法/超限） | `raise ValueError` → 网关 `_call` 统一转 `Error: ...` 文本 |
| 单文件语法错误 | 不中断，记一条 `syntax_error` issue 继续扫其余文件 |
| 无匹配定位 | 正常返回 `matched:false`（非错误） |
| 未知工具 | 网关兜底 `unknown tool`（无需新逻辑） |

### 安全与性能护栏（对齐现有 security 修复风格）

- 路径：`_check_path` 沙盒校验（目录/文件都走）。
- 扫描上限：`max_files`（默认 100，1..500）、单文件 ≤1MB、总行数 ≤200k → 防 DoS。
- 注册表：静态 `_TOOLS` 表加两条，O(1) 分发不变；零反射。
- 懒加载哲学：`ast`/`re` 是轻量标准库，顶部直接 import（不破坏启动时间）。

### 测试与回归

- `test_unified_rx.py`：工具数断言 53 → 55；新增 6 个用例：
  五规则全命中+行号精确、干净文件零 issue、with/close 容忍、
  traceback 定位、简洁格式定位+无匹配、max_files 越界 Error 契约。
- `server.py --selftest`：抽样断言 `bug_scan` 扫描自身 + `bug_locate` 定位自身。

---

## 后续可扩展（保持极简，按需再开）

- 规则开关参数（`rules=["undefined_name", ...]`）。
- 更多语言（GDScript/TS）→ 需要换 tokenizer，违背零依赖，慎做。
- `bug_fix`：基于 issue 的 file/line 自动生成修复建议（纯文本提示，不自动改码）。
- 变量跟踪增强：跨分支 def-use（当前线性近似，warning 级已够用）。
