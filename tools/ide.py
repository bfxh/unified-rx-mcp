"""tools/ide.py —— IDE 域门面（S48 职责拆分：实现移入子模块，本文件只再导出）。

职责：
- ide_edit.py  编辑（locate_edit/code_context/ide_edit_multi/ide_rename）
- ide_build.py 构建/lint（ide_build）
- ide_debug.py 调试/断点（ide_debug/ide_break）
- ide_diag.py  统一诊断（ide_diagnostics）
- ide_common.py 共享助手/解析器
- lsp.py       真实 LSP 客户端（独立模块）
"""
from tools.ide_build import ide_build  # noqa: F401,E402
from tools.ide_common import (  # noqa: F401,E402
    _RE_PY_FRAME,
    _SKIP_DIRS,
    MAX_CTX,
    _detect_eol,
    _iter_files,
    _lang_of,
    _parse_cargo_short,
    _parse_gcc,
    _parse_go_build,
    _read,
)
from tools.ide_debug import (  # noqa: F401,E402
    _parse_go_panic,
    _parse_java_trace,
    _parse_py_traceback,
    _parse_pytest,
    _parse_rust_panic,
    ide_break,
    ide_debug,
)
from tools.ide_diag import ide_diagnostics  # noqa: F401,E402
from tools.ide_edit import code_context, ide_edit_multi, ide_rename, locate_edit  # noqa: F401,E402
