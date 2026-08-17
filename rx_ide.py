# SPDX-License-Identifier: MIT
# RX-IDE Lite 入口：默认拉起 pywebview 桌面窗口；--web 仅启动 HTTP 服务。
"""RX-IDE Lite 命令行入口。

用法：
    python rx_ide.py          # pywebview 桌面窗口（缺依赖时自动回退纯 Web）
    python rx_ide.py --web    # 仅 HTTP 服务 http://127.0.0.1:17310/
"""

import os
import sys

# 确保项目根在 sys.path 最前，保证 rxide 包可导入
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rxide import host  # noqa: E402


def main():
    web_only = "--web" in sys.argv[1:]
    host.start(web_only=web_only)


if __name__ == "__main__":
    main()
