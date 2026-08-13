"""运行全部探针（probes/run_all.py）。

用法：
    python probes/run_all.py            # 全跑，退出码 0=全过
    python probes/run_all.py --json     # JSON 输出（供 reports 生成）
"""
import os
import sys

# 逐个 import probe 模块（每个模块向 _common.RESULTS 注册）
import _common
import probe_01_sandbox_bugscan  # noqa: F401
import probe_02_std_cbscan       # noqa: F401
import probe_03_locate_guard_pure  # noqa: F401

if __name__ == "__main__":
    # 确保以 probes/ 为 cwd 或把 probes/ 加入 path
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    _common.main()
