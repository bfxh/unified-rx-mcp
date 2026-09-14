# -*- coding: utf-8 -*-
"""tools/ide_deadcode.py —— 死符号可达性（S123；S135 原生化薄壳）：ide_dead_code。

动机（SCAN-POLICY：上帝对象拆分大于测试）：拆分前先知道**谁没人用**。全库
可达性近似——把"定义了但整个代码库零引用"的顶层函数/类/私有方法找出来，给
拆分候选清单一个客观下界。

口径（保守优先，误报比漏报伤人；唯一实现在 rust/src/deadcode.rs——
`rx-ide deadcode` 子命令，复用 pyast.rs 迷你解析器）：
- 引用 = 所有 `.py` 里出现的 ast.Name（任意 ctx）/ ast.Attribute 名字（跨模块算活）；
  `def foo():` 定义处本身不产生 Name 节点，所以"零 Name/Attribute 出现"是可靠死信号；
- **字符串引用升级**：名字出现在任何字符串字面量里（getattr 分发、注册表、
  __all__）→ 不判死，单独列 suspect_dynamic——动态分发静态分析看不见；
- **带装饰器的定义默认豁免**：@tool/@app.route 之类是框架注册点（S135 为 pyast
  补 `deco` 计数承载此判定）；要查就传 include_decorated=true；
- 方法只查**私有**（单下划线、非双下划线）；嵌套函数/闭包不查；dunder 一律不查；
- 测试文件引用与生产代码同权；pytest 命名约定豁免（test_*/pytest_*/setup_*/
  teardown_* 前缀与 conftest.py 整文件）。

S135 原生化（HARDENING §六 候选二，纪律=先测基线再做）：
- 对照实验①夹具：八字段 + dead/suspect 列表与 Python 版**逐字节一致**
  （dead 7 / suspect 1 / defs 12）；②**真仓全量**：192 文件 / 1496 defs /
  dead 6 / suspect 3 / exempted 137·730 逐项一致；性能 **0.51s → 0.23s（2.2×）**；
- 本文件收敛为薄壳：沙盒解析（S88 纪律补全——原实现直付 abspath）+ 子进程
  转调 + root/note/elapsed 补齐；exe 缺失报清晰错误不静默降级。
- 已知边界（如实）：parse_errors 的 error 报文为 pyast 文本（Python 版是
  SyntaxError 文本）——形状一致、字数不同；多坏文件顺序按各自遍历序。

诚实边界：全库静态可达性近似，不是精确调用图——不解 import 语义（名字重用跨
模块会误活）、不追 exec/eval/globals() 字符串拼接、别名引用（as 重命名后算不到
原名）。零引用 ≠ 可安全删除，删前人工确认。
"""
import os
import time

from registry import tool
from tools.fs import _resolve as _fs_resolve
from tools.ide_read import _rx_ide_call  # 与 ide 域同一 exe 桥（单一实现）

_NOTE = ("口径：ast.Name/Attribute 零引用 + 字符串引用降级为嫌疑 + "
         "装饰器定义默认豁免。别名/import as、exec/eval、globals() "
         "拼接追不到——零引用≠可安全删除，删前人工确认")


@tool("ide_dead_code",
      "死符号可达性（全库）：报零引用的顶层函数/类/私有方法（拆分候选下界）；带装饰器默认豁免，名字出现在字符串 → suspect_dynamic 不判死；删前人工确认", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目根目录（默认当前目录）"},
           "max_files": {"type": "integer", "description": "最多扫描的 .py 文件数（默认 2000）"},
           "include_decorated": {"type": "boolean",
                                 "description": "把带装饰器的定义也纳入死代码判定（默认 false=豁免）"},
           "max_results": {"type": "integer", "description": "dead 列表上限（默认 200）"}},
       "required": []})
def ide_dead_code(path=None, max_files=2000, include_decorated=False,
                  max_results=200):
    t0 = time.time()
    try:
        root = _fs_resolve(path or os.getcwd())   # S135：读路径过沙盒（S88 纪律补全）
    except ValueError as e:
        return {"error": str(e)}
    out = _rx_ide_call(["deadcode", root, str(int(max_files)), str(int(max_results)),
                        "1" if include_decorated else "0"])
    out["root"] = root
    out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    out["note"] = _NOTE
    return out
