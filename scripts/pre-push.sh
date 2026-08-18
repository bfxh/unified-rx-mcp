#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
# pre-push — 提交前本地五连（REGRESSION_GUARD：Pre-push CI 模拟）
#   语义回归 → pytest → cargo test → mcp_smoke → ratchet/async_guard/sync_check
# 用法：scripts/pre-push.sh [--skip-sync]（--skip-sync 跳过 51 副本对比）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/7] semantic regression（语义回归——改完代码先跑这个） ==="
timeout 180 python scripts/semantic_regression.py

echo "=== [2/7] pytest ==="
python -m pytest test_unified_rx.py -q

echo "=== [3/7] cargo test (lse-engine) ==="
(cd lse-engine && cargo test 2>&1 | grep -E "test result" | head -1)

echo "=== [4/7] MCP stdio smoke ==="
timeout 90 python scripts/mcp_smoke.py

echo "=== [5/7] tool_ratchet ==="
python scripts/tool_ratchet.py --check

echo "=== [6/7] async_guard ==="
python scripts/async_guard.py server.py

echo "=== [7/7] sync_check ==="
if [[ "${1:-}" == "--skip-sync" ]]; then
  echo "(跳过 51 副本对比)"
else
  python scripts/sync_check.py
fi

echo "=== pre-push 全部通过 ==="
