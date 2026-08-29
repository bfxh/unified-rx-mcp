# -*- coding: utf-8 -*-
"""S10 端到端探针：真实 stdio 协议下 local_run 被 notifications/cancelled 中断。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(r"D:\开发\unified-rx-mcp")
PY = r"C:\Users\lbx13\AppData\Local\Programs\Python\Python311\python.exe"
# 兜底脚本放 %TEMP%（ASCII 路径）——绝不往仓库根写临时文件（A2 教训）
sleep_py = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path(os.environ.get("TEMP", r"C:\Temp")) / "uRX_probe_sleep_default.py"
sleep_py.parent.mkdir(parents=True, exist_ok=True)
sleep_py.write_text("import time\ntime.sleep(30)\nprint('done')\n", encoding="utf-8")

proc = subprocess.Popen(
    [PY, "-X", "utf8", "server.py"],
    cwd=str(REPO), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=open(r"C:\Users\lbx13\AppData\Local\Temp\opencode\uRX_server_err.log",
                "w", encoding="utf-8"),
    text=True, encoding="utf-8")


def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2025-03-26"}})
# 吃掉 initialize 响应
line = proc.stdout.readline()
assert '"serverInfo"' in line, line[:200]
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

t0 = time.monotonic()
call_id = 42
send({"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
      "params": {"name": "local_run",
                 "arguments": {"domain": "python", "name": "script",
                               "args": {"script": str(sleep_py)},
                               "timeout": 40, "__authorized": True},
                 "_meta": {"progressToken": "pt-42"}}})
time.sleep(3.0)                      # 让进程跑起来并至少发一次 1.5s 心跳
send({"jsonrpc": "2.0", "method": "notifications/cancelled",
      "params": {"requestId": call_id}})

resp = None
notes = []
progress_notes = []
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    line = proc.stdout.readline()
    if not line:
        break
    try:
        m = json.loads(line)
    except json.JSONDecodeError:
        continue
    if m.get("id") == call_id:
        resp = m
        break
    if m.get("method") == "notifications/message":
        notes.append(m["params"]["data"][:80])
    if m.get("method") == "notifications/progress":
        progress_notes.append(m["params"].get("progress"))

wall = time.monotonic() - t0
proc.terminate()
txt = ""
if resp:
    txt = resp["result"]["content"][0]["text"]
print(json.dumps({
    "wall_seconds": round(wall, 2),
    "isError": resp["result"].get("isError") if resp else None,
    "cancelled_in_payload": ("cancelled" in txt) or ("取消" in txt),
    "progress_notes": progress_notes,
    "payload_preview": txt[:200],
    "log_notes": notes[:3],
}, ensure_ascii=False))
ok = bool(resp and resp["result"].get("isError") and wall < 10
          and (("cancelled" in txt) or ("取消" in txt)) and len(progress_notes) >= 1)
print("E2E-CANCEL+PROGRESS:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
