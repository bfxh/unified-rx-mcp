# -*- coding: utf-8 -*-
"""tools/__init__.py —— 工具包汇总：导入全部域模块触发注册。"""
import os
import sys

# 确保项目根在 sys.path（脚本直跑时 tools 才能作为包被相对导入）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from . import fs        # noqa: F401,E402
from . import pure      # noqa: F401,E402
from . import scan      # noqa: F401,E402
from . import ide       # noqa: F401,E402
from . import guard     # noqa: F401,E402
from . import learn     # noqa: F401,E402
from . import ops       # noqa: F401,E402
from . import search    # noqa: F401,E402
from . import game      # noqa: F401,E402
from . import collab    # noqa: F401,E402
from . import meta      # noqa: F401,E402
from . import engine    # noqa: F401,E402
from . import bevy      # noqa: F401,E402
from . import attack    # noqa: F401,E402  S7 默认化：攻击面工具随包常驻
from . import appaudit  # noqa: F401,E402  S8：智能体/桌面应用自查域（克隆→隔离审计→清理）
from . import astscan   # noqa: F401,E402  S9：结构化扫描域（Python AST / JS 词法-括号管线）

__all__ = ["fs", "pure", "scan", "ide", "guard", "learn", "ops",
           "search", "game", "collab", "meta", "engine", "bevy", "attack",
           "appaudit", "astscan"]
