#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
# pre-push — 提交前本地四连（REGRESSION_GUARD：Pre-push CI 模拟）
#   pytest → cargo test → mcp_smoke → ratchet/async_guard/sync_check
# 用法：scripts/pre-push.sh [--skip-sync]（--skip-sync 跳过 51 副本对比）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/6] pytest ==="
python -m pytest test_unified_rx.py -q

echo "=== [2/6] cargo test (lse-engine) ==="
(cd lse-engine && cargo test 2>&1 | grep -E "test result" | head -1)

echo "=== [3/6] MCP stdio smoke ==="
timeout 90 python scripts/mcp_smoke.py

echo "=== [4/6] tool_ratchet ==="
python scripts/tool_ratchet.py --check

echo "=== [5/6] async_guard ==="
python scripts/async_guard.py server.py

echo "=== [6/6] sync_check ==="
if [[ "${1:-}" == "--skip-sync" ]]; then
  echo "(跳过 51 副本对比)"
else
  python scripts/sync_check.py
fi

echo "=== pre-push 全部通过 ==="
