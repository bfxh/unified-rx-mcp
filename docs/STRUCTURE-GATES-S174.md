# 结构三连门 + 「门是真门」逐门注入验证（S174）

**TL;DR**：补了三道此前没人管的维度——**stdout 协议面**（MCP stdio 上的 `print`）、
**嵌套深度**（与 C901 复杂度互补）、**形参个数**（`ruff.toml` 有意不收 PLR0913，独立补上），
全是棘轮（只准减）。另加 `scripts/gate_probe.py`：**给每道门注入一次最小违规，它必须转红**——
静态自检查不出「门是不是空的」。逐门验证跑下来，可验的门**全是真门**，没有空门。

## 1. 三道结构门

| 门 | 判据 | 存量 |
|---|---|---|
| `stdout_gate.py` | MCP 服务面（根 `*.py` + `tools/`）出现 `print(` | 2 |
| `nest_gate.py` | 函数控制流嵌套 > 4 / > 8（`ast` 遍历） | 18 / 1 |
| `args_gate.py` | 函数形参 > 7（posonly + 普通 + kwonly） | 2 |

三道共用 `scripts/gate_common.py`（扫描面 / 基线读写 / 棘轮 / 报告只留一份实现 ⇒ 口径不漂移，
也不会被 dupe 门判成三份雷同文件）。

### 1.1 stdout 门：为什么在本仓特别要紧

MCP 走 **JSON-RPC over stdio**——stdout 就是协议通道。工具实现里任何一句 `print` 都会把非协议
字节写进这条流。这类 bug 的诡异之处是**手动跑永远正常**（那时没人监听 stdout），只有接进真
client 才炸 ⇒ 测试抓不到、人也想不起来。

存量 2 处，都是真的：

- `registry.py:62` `print(f"[ctx] SET …")` —— 请求上下文设置路径上的调试残留；
- `tools/ide_build.py:176` `print(json.dumps({...}))` —— 工具实现里直接往 stdout 打 JSON。

豁免（都写理由，不是"报多了就关掉"）：

- `scripts/`、`bench/`、`tests/`、`spec/`、`docs/` —— 命令行工具打印到 stdout 是本分；
- `server.py` 的 `selftest()` —— 那是 `python server.py --selftest` 子命令，CLI 模式不是 stdio
  会话，9 处打印都是给人看的诊断行。

### 1.2 嵌套深度门：与 C901 是两维

`ruff.toml` 已收 `C901`（mccabe 复杂度），它数的是「分支多少」；本门数「分支套了几层」。
同样 10 个分支，写成卫语句平铺与写成五层 `if` 套 `with` 套 `try`，**复杂度可能一模一样**，
但后者改一处要同时记住五层上下文。本仓 `ruff.toml` 明确不收 PL 子集（"复杂度另由 god 门管"），
而 god 门只管**函数长度** ⇒ 嵌套这一维此前确实没人管。

用 `ast` 遍历而不是正则 ⇒ 不受注释/字符串干扰。最深的一处是 `tools/scip.py`（9 层）。

### 1.3 形参门：补 ruff 有意关掉的那一条

`ruff.toml` 明确不收 PL 子集，`PLR0913`（参数过多）就在里面，理由是"PL 全是本仓的自觉选择"。
**有意关掉不等于这条维度没有价值** ⇒ 用独立一门补上，按棘轮走，不进 ruff 规则集（免得和
既有取舍打架）。

## 2. `gate_probe.py`：门是真门吗

27 道门，门越多越可能有失效的那几道，而失效的门**不会让任何测试变红**、CI 还挂着绿勾。
已有的 `ci_gate.py`（selftest）查的是门**在不在**，`UNIFIED_RX_GATE_FORCE_FAIL` 只验指定的一步；
都查不出门**是不是空的**。

判据：注入前门是绿的、注入后红了 ⇒ 真门。注入前就红的标「验证不充分」，**不冒充通过**；
没写探针的明确列进 `CANNOT_VERIFY`，同样不冒充通过。

### 2.1 结果：可验的门全是真门

god / lint / typos / secrets / gitleaks / path / dupe / deps-lock / audit-freshness 九道，
注入前后 `0 → 1` 全部成立。本机 `mypy` 缺 ⇒ type-gate 自动标「注入前已红」（CI 上装了 mypy
就会真正验证）。

### 2.2 但注入本身会骗人——三次假阴性

写这道验证时踩的坑，比结论更值得留档（**门没红不一定说明门坏了，也可能是探针不像**）：

1. `path-gate` 的 P3 阈值是 >1MB，而探针 `y = 2\n` × 120000 只有 **0.72MB** ⇒ 假阴性。
   改用 P4（同一行里 `..` 与写文件原语同现）更轻也更稳。
2. `gitleaks` 的 GitHub token 规则要 `ghp_` + **36** 字符，我一开始只给了 32；而
   AWS 官方**文档示例**里那组 AKIA 开头的示例 key 是 gitleaks 内置豁免 ⇒ 拿它当探针必假阴性。
3. `deps_lock` 的探针用 `str.replace("[dependencies]", …)`，改到的是 **Cargo.toml 注释里**
   那三个字（注释行被判据跳过）⇒ 必须只替换行首的 `[dependencies]`。

### 2.3 探针自己也要过门

`gate_probe.py` 里含凭据形状与错拼词，会被 secrets / gitleaks / typos / lint 扫到 ⇒
这些值一律**运行时生成**（`_aws_creds` / `_gh_token` / `_typo_text`），源码里不留完整字面量。
第一次跑就现形了：typos 门抓到了我写在探针里的那个错拼单词（"rec…ieve"）。
同理，本文件与提交信息里都不能写出该词的完整拼写——写了就被同一道门拦。

## 3. 接入

- `scripts/local_gate.py`：三道门进 `fast` 档；`gate-probe` 归 `full`（每道门跑两遍约 1–2 分钟，
  不进 pre-commit）。
- `.github/workflows/core.yml`：四步显式调用（同源漂移由 `tests/test_s145_gates.py` 锁）。
- `tests/test_s145_gates.py` 的 `STEPS` 元组补上新步骤名 ⇒ `--list` 覆盖检查跟着变严。

## 4. 待办

`gate_probe.py` 的 `CANNOT_VERIFY` 里还有 15 道没写探针（mcp-surface / tool-evals /
quality-pact / cli-golden / self-attack / data-flow …），造它们要协议违约回包、新提交、改金样，
代价大 ⇒ 明确列出而不是假装验证过。
