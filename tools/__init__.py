"""tools/__init__.py —— 工具包汇总：导入全部域模块触发注册。"""
import os
import sys

# 确保项目根在 sys.path（脚本直跑时 tools 才能作为包被相对导入）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from . import (
    appaudit,  # noqa: F401,E402  # S8：智能体/桌面应用自查域（克隆→隔离审计→清理）
    astgrep,  # noqa: F401,E402  # S111：可选外部引擎 ast-grep 结构搜索（薄壳）
    astscan,  # noqa: F401,E402  # S9：结构化扫描域（Python AST / JS 词法-括号管线）
    attack,  # noqa: F401,E402  # S7 默认化：攻击面工具随包常驻
    bevy,  # noqa: F401,E402
    breaker,  # noqa: F401,E402  # S122：工具熔断（同工具+参数重复超限即断）
    cargoaudit,  # Rust 依赖安全审计（RustSec/cargo-audit 薄壳；__all__ 已导出）
    code_review,  # noqa: F401,E402  # S127：评审域自 scan.py 拆出（上帝对象 P0）
    engine,  # noqa: F401,E402
    filescan,  # noqa: F401,E402  # S115：文件扫描（签名/熵/哈希，GPU 加速熵）
    fs,  # noqa: F401,E402
    game,  # noqa: F401,E402
    gpu,  # noqa: F401,E402  # S114：GPU 计算支持（OpenCL/ctypes，零 pip 依赖）
    guard,  # noqa: F401,E402
    ide,  # noqa: F401,E402
    ide_autopilot,  # noqa: F401,E402  # S69：开发目录自动驾驶（启动自动体检+打开）
    ide_callgraph,  # noqa: F401,E402  # S125：真调用图（同 nameres 作用域引擎 + stitch）
    ide_deadcode,  # noqa: F401,E402  # S123：死符号可达性（ast 保守口径）
    ide_doctor,  # noqa: F401,E402  # R4：一键项目体检（六项聚合 → 基线报告）
    ide_read,  # noqa: F401,E402  # S66：结构化读取（ide_outline/ide_read_symbol）
    ide_riskrank,  # noqa: F401,E402  # S129：风险榜（高扇入×无测试 → 拆分/补测排序）
    ide_test,  # noqa: F401,E402  # R2：统一测试入口（pytest/cargo/go → per-test 结果）
    ide_vscode,  # noqa: F401,E402  # S68：VS Code 后手入口（open/diff）
    impact,  # noqa: F401,E402  # S129：ide_impact 影响面（自 lsp.py 拆出 + 调用面档）
    learn,  # noqa: F401,E402
    lsp,  # noqa: F401,E402  # S17：真 LSP 客户端（rust-analyzer/pylsp）
    lsp_actions,  # noqa: F401,E402  # S129：ide_lsp 动作分发（自 lsp.py 拆出）
    meta,  # noqa: F401,E402
    metrics,  # noqa: F401,E402  # S52：代码质量度量域（coverage/dep_graph/stability）
    neardupes,  # noqa: F401,E402  # S117：近似重复/同族聚类（n-gram + 余弦，GPU）
    ops,  # noqa: F401,E402
    scan,  # noqa: F401,E402
    scip,  # noqa: F401,E402  # S112：SCIP 索引消费（外部索引器产物，只读）
    search,  # noqa: F401,E402
    secrets,  # noqa: F401,E402  # S123：凭据/密钥泄漏扫描（模式+高熵，掩码输出）
    sysinfo,  # noqa: F401,E402  # S148：混合架构调度（P/E 核拓扑/线程/steer/设备，rx-sys.exe）
    vulnkb,  # noqa: F401,E402  # S110：漏洞知识库（规则号/关键词 → 成因/修法/先例）
)

# 注：模块名不得叫 sys.py——与 stdlib sys 撞名时 `from . import sys` 会静默跳过（S148 实锤）

__all__ = [
    "appaudit",
    "astgrep",
    "astscan",
    "attack",
    "bevy",
    "breaker",
    "cargoaudit",
    "code_review",
    "engine",
    "filescan",
    "fs",
    "game",
    "gpu",
    "guard",
    "ide",
    "ide_autopilot",
    "ide_callgraph",
    "ide_deadcode",
    "ide_doctor",
    "ide_read",
    "ide_test",
    "ide_vscode",
    "impact",
    "learn",
    "lsp",
    "lsp_actions",
    "meta",
    "metrics",
    "neardupes",
    "ops",
    "scan",
    "scip",
    "search",
    "secrets",
    "vulnkb",
]
