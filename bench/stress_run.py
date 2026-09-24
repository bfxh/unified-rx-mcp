"""高压语料跑机器（S162）：读 `spec/stress-corpus.json`，对**真实 stdio server** 逐条施压。

判据是**数据**（语料），本脚本只是跑它的机器——加场景=加一条 JSON，不改代码。
设计要点（每条都对应一次实测教训，见 docs/STRESS-AND-PR-GATES.md §1.2）：
  · **expect 必须先分类** success|error —— 首版把"本该成功"的用例当错误期望 ⇒ 假阴性；
  · **并发必须用互不相同的输入** —— 同参命中 S103 缓存，量到的是缓存不是并行；
  · **客户端按 id 配对** —— 服务端 4 worker、**响应可乱序**，"发一条读一行"会错配（p95 假数）；
  · 语料里只有占位符（`$fixture`），绝对路径由本脚本在临时目录物化 ⇒ 语料可移植、也过 path 门。

用法：
  python bench/stress_run.py --tier fast      # 抽样档（秒级，提交前用）
  python bench/stress_run.py --tier full      # 全量（不含并发档）
  python bench/stress_run.py --tier concurrency   # 并发档（**需机器级独占**：先 perf_lock.py acquire）
  python bench/stress_run.py --tier all
退出码：0 = 全过；1 = 有失败。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "spec" / "stress-corpus.json"
W = (Path(tempfile.gettempdir()) / "urx-stress-corpus").resolve()
W.mkdir(parents=True, exist_ok=True)
os.environ["UNIFIED_RX_SANDBOX"] = str(W)
os.environ.setdefault("UNIFIED_RX_SPILL_KB", "32")     # 让 spill 场景在小夹具上也能触发


class Server:
    """按 id 配对的客户端（响应可乱序，必须配对）。"""

    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(ROOT / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, shell=False, cwd=str(ROOT))
        self._wlock = threading.Lock()
        self._lock = threading.Lock()
        self._slots = {}
        self._alive = True
        self._n = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while self._alive:
            line = self.p.stdout.readline()
            if not line:
                break
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                slot = self._slots.pop(rec.get("id"), None)
            if slot is not None:
                slot[1] = rec
                slot[0].set()

    def call(self, name, args, timeout=60):
        with self._wlock:
            self._n += 1
            mid = self._n
        slot = [threading.Event(), None]
        with self._lock:
            self._slots[mid] = slot
        self.p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
             "params": {"name": name, "arguments": args}}, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        slot[0].wait(timeout=timeout)
        return (slot[1] or {}).get("result") or {}

    def close(self):
        self._alive = False
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:                                          # noqa: BLE001
            self.p.kill()


def materialize(fixtures):
    """把语料里的 fixture 物化成文件/字符串（语料只存占位符，不存绝对路径）。"""
    out = {}
    for name, spec in fixtures.items():
        if spec.get("kind") == "file":
            p = W / f"{name}.txt"
            if "text_len" in spec:
                p.write_text(spec.get("fill", "x") * spec["text_len"], encoding="utf-8")
            else:
                p.write_text(spec.get("text", ""), encoding="utf-8")
            out[name] = str(p)
        else:                                                      # arg：超大字符串
            out[name] = spec.get("fill", "x") * spec.get("repeat", 1000)
    return out


def resolve(args, fx):
    out = {}
    for k, v in (args or {}).items():
        out[k] = fx[v[1:]] if isinstance(v, str) and v.startswith("$") else v
    return out


def body_of(res):
    """回包正文（剥掉不可信前缀整行，再解析）。"""
    text = ((res.get("content") or [{}])[0].get("text") or "")
    if text.startswith("[untrusted-content"):
        _, _, text = text.partition("\n")
    try:
        return json.loads(text), text
    except ValueError:
        return None, text


def check_case(srv, case, fx):
    """返回 (ok, detail)。判据：expect 分类 + 形状（错误要可行动；成功要可解析）。"""
    res = srv.call(case["tool"], resolve(case.get("args"), fx))
    payload, text = body_of(res)
    is_err = res.get("isError") is True
    if case["expect"] == "error":
        if not is_err:
            return False, "期望失败但拿到了成功形状"
        if payload is None:
            return False, "错误回包不是 JSON（散文错误已废止）"
        e = payload.get("error") or {}
        if payload.get("ok") is not False or not e.get("message") or not e.get("next"):
            return False, "错误缺 ok=false / message / next"
        if (res.get("structuredContent") or {}).get("ok") is not False:
            return False, "structuredContent 未同形"
        return True, "错误结构化 + 可行动"
    # expect == success
    if is_err:
        return False, f"期望成功却失败：{text[:80]}"
    if payload is None:
        return False, "成功回包不是可解析 JSON"
    assert_spec = case.get("assert") or {}
    if assert_spec.get("spilled"):
        sp = (res.get("structuredContent") or {}).get("spilled") or {}
        if not sp.get("path") or not sp.get("fetch"):
            return False, "期望溢出落盘（带 path+fetch），未命中"
        if "truncated" in text:
            return False, "溢出与截断同时出现（应只溢出）"
    for want in assert_spec.get("contains", []):
        if want not in text:
            return False, f"回包里找不到 {want}"
    for key in assert_spec.get("keys", []):
        if key not in payload:
            return False, f"顶层缺键 {key}"
    return True, "成功形状符合"


def run_concurrency(srv, spec, fx):
    """并发档：**互不相同**的输入 + 串行对照（同一批输入的串行和 vs 并发 wall）。"""
    results = []
    for case in spec["cases"]:
        n = case["n"]
        paths = []
        for i in range(n * 2):                    # 2n 个不同文件：n 串行 + n 并发
            p = W / f"conc-{case['name']}-{i}.py"
            p.write_text(f"def h{i}():\n    return {i}\n", encoding="utf-8")
            paths.append(str(p))
        t0 = time.perf_counter()
        for p in paths[:n]:
            srv.call(case["tool"], {**(case.get("args") or {}), "path": p})
        serial = (time.perf_counter() - t0) * 1000
        out = []

        def work(path, sink):
            t = time.perf_counter()
            r = srv.call(case["tool"], {**(case.get("args") or {}), "path": path})
            sink.append(((time.perf_counter() - t) * 1000, r))

        ths = [threading.Thread(target=work, args=(paths[n + i], out)) for i in range(n)]
        t0 = time.perf_counter()
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        wall = (time.perf_counter() - t0) * 1000
        limit = (case.get("assert") or {}).get("wall_vs_serial_lt", 0.6)
        ratio = wall / serial if serial else 9.9
        ok = ratio < limit
        results.append((ok, f"{case['name']}：串行 {serial:.0f}ms vs 并发 {wall:.0f}ms"
                            f"（比值 {ratio:.2f} < {limit}）"))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="fast", choices=("fast", "full", "concurrency", "all"))
    a = ap.parse_args()
    spec = json.loads(CORPUS.read_text(encoding="utf-8"))
    fx = materialize(spec["fixtures"])
    tiers = {"fast": {"fast"},
             "full": {"fast", "full"},
             "concurrency": {"concurrency"},
             "all": {"fast", "full", "concurrency"}}[a.tier]
    fails, n = [], 0
    srv = Server()
    try:
        for case in spec["cases"]:
            if case["tier"] not in tiers:
                continue
            n += 1
            ok, why = check_case(srv, case, fx)
            if not ok:
                fails.append(f"{case['name']}：{why}")
        if "concurrency" in tiers:
            for ok, why in run_concurrency(srv, spec["concurrency"], fx):
                n += 1
                if not ok:
                    fails.append(why)
    finally:
        srv.close()
    for f in fails:
        print(f"  ❌ {f}")
    if fails:
        print(f"STRESS FAIL（{len(fails)}/{n} 条不符）")
        return 1
    print(f"STRESS OK（{n} 例：tier={a.tier}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
