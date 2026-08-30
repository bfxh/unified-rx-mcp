# -*- coding: utf-8 -*-
"""tools/__init__.py —— 工具包汇总：导入全部域模块触发注册。"""
import os
import sys

# 确保项目根在 sys.path（脚本直跑时 tools 才能作为包被相对导入）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from . import fs        # noqa: F401,E402
from . import scan      # noqa: F401,E402
from . import ide       # noqa: F401,E402
from . import guard     # noqa: F401,E402
from . import learn     # noqa: F401,E402
from . import ops       # noqa: F401,E402
from . import search    # noqa: F401,E402
from . import game      # noqa: F401,E402
from . import meta      # noqa: F401,E402
from . import lsp       # noqa: F401,E402  S17：真 LSP 客户端（rust-analyzer/pylsp）
from . import engine    # noqa: F401,E402
from . import bevy      # noqa: F401,E402
from . import attack    # noqa: F401,E402  S7 默认化：攻击面工具随包常驻
from . import appaudit  # noqa: F401,E402  S8：智能体/桌面应用自查域（克隆→隔离审计→清理）
from . import astscan   # noqa: F401,E402  S9：结构化扫描域（Python AST / JS 词法-括号管线）
from . import metrics   # noqa: F401,E402  S52：代码质量度量域（coverage/dep_graph/stability）
from . import ide_test  # noqa: F401,E402  R2：统一测试入口（pytest/cargo/go → per-test 结果）
from . import ide_doctor  # noqa: F401,E402  R4：一键项目体检（六项聚合 → 基线报告）
from . import ide_read    # noqa: F401,E402  S66：结构化读取（ide_outline/ide_read_symbol）
from . import ide_vscode  # noqa: F401,E402  S68：VS Code 后手入口（open/diff）
from . import ide_autopilot  # noqa: F401,E402  S69：开发目录自动驾驶（启动自动体检+打开）

__all__ = ["fs", "pure", "scan", "ide", "guard", "learn", "ops",
           "search", "game", "collab", "meta", "engine", "bevy", "attack",
           "appaudit", "astscan", "metrics", "ide_test", "ide_doctor",
           "ide_read", "ide_vscode", "ide_autopilot"]
