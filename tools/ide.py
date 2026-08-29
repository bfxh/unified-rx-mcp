# -*- coding: utf-8 -*-
"""tools/ide.py —— IDE 域门面（S48 职责拆分：实现移入子模块，本文件只再导出）。

职责：
- ide_edit.py  编辑（locate_edit/code_context/ide_edit_multi/ide_rename）
- ide_build.py 构建/lint（ide_build）
- ide_debug.py 调试/断点（ide_debug/ide_break）
- ide_diag.py  统一诊断（ide_diagnostics）
- ide_common.py 共享助手/解析器
- lsp.py       真实 LSP 客户端（独立模块）
"""
from tools.ide_edit import locate_edit, code_context, ide_edit_multi, ide_rename  # noqa: F401,E402
from tools.ide_build import ide_build  # noqa: F401,E402
from tools.ide_debug import (  # noqa: F401,E402
    ide_debug, ide_break, _parse_py_traceback, _parse_rust_panic,
    _parse_java_trace, _parse_go_panic, _parse_pytest)
from tools.ide_diag import ide_diagnostics  # noqa: F401,E402
from tools.ide_common import (  # noqa: F401,E402
    _read, _lang_of, _iter_files, _detect_eol, MAX_CTX, _SKIP_DIRS,
    _parse_gcc, _parse_cargo_short, _parse_go_build, _RE_PY_FRAME,
)
