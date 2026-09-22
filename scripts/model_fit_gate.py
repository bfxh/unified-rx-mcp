# -*- coding: utf-8 -*-
"""模型适配门（S161）：把"能被**弱模型**安全使用"变成可判定的门。

为什么单列一道门：`mcp_surface_gate` 验的是**协议契约**（字段在不在、上不上线路），
验不了"弱模型用错时会发生什么"。而错误放大链的起点正是那里——一次模糊错误 ⇒ 盲重试 ⇒
循环 ⇒ 结论污染。本门对**真实 stdio server** 握手，用弱模型的典型犯错方式施压。

三组判据：
  G1 弱模型模拟器：缺参 / 空值 / 沙盒外路径 / 未授权写 / 未知工具 / 超长参数——
     每次都必须给出**结构化、可行动**的错误，且**绝不**出现成功形状。
  G2 回包预算：代表工具的正常回包要么 ≤ 预算，要么**溢出落盘**（给路径 + 取用命令）；
     不得是"截断后的半截 JSON"（静默丢信息 = 弱模型照着残片编）。
  G3 一种形态：所有回包文本都是可解析 JSON（散文 `ERROR:` 已废止）。

退出码：0 = 全过；1 = 任一条违约（逐条打印）。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

W = (Path(tempfile.gettempdir()) / "urx-model-fit-fx").resolve()   # 夹具根即沙盒
W.mkdir(parents=True, exist_ok=True)
os.environ["UNIFIED_RX_SANDBOX"] = str(W)
(W / "sample.py").write_text("def f(p):\n    return open(p).read()\n", encoding="utf-8")

BUDGET = 64 * 1024          # G2：单次回包文本预算（字节）
NOTICE = "[untrusted-content"


class Server:
    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(ROOT / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            shell=False, cwd=str(ROOT))

    def call(self, name, args):
        self.p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": args}}, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        return (json.loads(self.p.stdout.readline()).get("result") or {})

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:                                        # noqa: BLE001
            self.p.kill()


def parse(text):
    """剥掉不可信前缀后解析。

    前缀是**一整行**（`[untrusted-content …]\\n`，S144 契约），故按首个换行整行剥掉——
    首版只剥了标记本身、把后半句留在 JSON 前面，自家门当场判红（留着当教训）。
    """
    if text.startswith(NOTICE):
        _, _, text = text.partition("\n")
    return json.loads(text)


def main() -> int:
    fails = []
    checked = {"G1": 0, "G2": 0, "G3": 0}

    def bad(msg):
        fails.append(msg)

    s = Server()
    try:
        # ---- G1：弱模型模拟器（每个都必须"结构化 + 可行动 + 非成功形状"）----
        in_sandbox = str(W / "sample.py")
        matrix = [
            ("缺参",        "fs_read", {}),
            ("空值",        "fs_read", {"path": ""}),
            ("沙盒外路径",  "fs_read", {"path": "C:/Windows/win.ini"}),
            ("未授权写",    "fs_write", {"path": in_sandbox, "content": "x"}),
            ("未知工具",    "no_such_tool_xyz", {}),
            ("超长参数",    "fs_read", {"path": "x" * 3_000_000}),
        ]
        for label, name, args in matrix:
            res = s.call(name, args)
            checked["G1"] += 1
            if res.get("isError") is not True:
                bad(f"G1[{label}] 未被判错（isError={res.get('isError')!r}）")
                continue
            try:
                p = parse(((res.get("content") or [{}])[0].get("text") or ""))
            except ValueError:
                bad(f"G1[{label}] 错误回包不是 JSON（弱模型无法解析）")
                continue
            if p.get("ok") is not False:
                bad(f"G1[{label}] 错误却 ok={p.get('ok')!r}（成功形状里藏错误）")
            err = p.get("error") or {}
            if not err.get("message"):
                bad(f"G1[{label}] 缺 error.message")
            if not err.get("next"):
                bad(f"G1[{label}] 缺 error.next（弱模型只能盲重试）")
            if (res.get("structuredContent") or {}).get("ok") is not False:
                bad(f"G1[{label}] structuredContent 未同形")

        # ---- G2：回包预算（≤预算，或溢出落盘给了路径与取用命令）----
        for name, args in (("fs_list", {"path": str(W)}),
                           ("bug_scan", {"path": in_sandbox}),
                           ("fs_read", {"path": in_sandbox})):
            res = s.call(name, args)
            checked["G2"] += 1
            text = ((res.get("content") or [{}])[0].get("text") or "")
            sc = res.get("structuredContent") or {}
            spilled = sc.get("spilled") or {}
            if len(text.encode("utf-8")) > BUDGET and not spilled:
                bad(f"G2[{name}] 回包 {len(text)} 字节超预算且未溢出落盘（截断=静默丢信息）")
            if spilled:
                if not spilled.get("path") or not spilled.get("fetch"):
                    bad(f"G2[{name}] 溢出但缺 path/fetch（模型取不回来）")
                if "truncated" in text:
                    bad(f"G2[{name}] 溢出与截断同时出现（应只溢出）")

        # ---- G3：一种形态（所有文本块都可解析 JSON）----
        for name, args in (("sys_topology", {}), ("fs_read", {"path": in_sandbox}),
                           ("fs_read", {})):
            res = s.call(name, args)
            checked["G3"] += 1
            text = ((res.get("content") or [{}])[0].get("text") or "")
            if text.startswith("ERROR:"):
                bad(f"G3[{name}] 散文错误回包（S161 已废止）")
            try:
                parse(text)
            except ValueError:
                bad(f"G3[{name}] 回包文本不是可解析 JSON")
    finally:
        s.close()

    if fails:
        for f in fails:
            print(f"  ❌ {f}")
        print(f"MODEL-FIT-GATE FAIL: {len(fails)} 条违约 "
              f"(G1={checked['G1']} G2={checked['G2']} G3={checked['G3']})")
        return 1
    print(f"MODEL-FIT-GATE OK（G1 弱模型模拟 {checked['G1']} 例 / "
          f"G2 回包预算 {checked['G2']} 例 / G3 一种形态 {checked['G3']} 例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
