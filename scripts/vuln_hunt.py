"""scripts/vuln_hunt.py —— 一键漏洞挖掘（S171 队列 ⑥）：克隆 → 三合一 → 封印报告。

三面（全部落在**克隆体**或**本工具面**上，绝不碰原件）：
  ① 自攻 + 模糊  `attack_cruise`（授权门自审 / 路径探针 / 输入模糊 / 大输入）——打本工具面；
  ② 污点         `rust_taint_scan`（形参即来源 → 汇点，跨文件链）——打在克隆体上；
  ③ 静见         `app_audit`（JS 危险面 / 秘密掩码采集 / URL / 二进制 / asar）+ `secrets_hunt`。

隔离纪律（本仓红线）：`UNIFIED_RX_AUDIT_SANDBOX` 与 `UNIFIED_RX_SANDBOX` 收到**同一个**
`--sandbox` 根下（默认 `<TEMP>/unified-rx-hunt`）——这样克隆体既在审计沙箱内（`app_audit`
拒绝原件路径），也在 fs 沙箱内（`rust_taint_scan`/`secrets_hunt` 能解析）。结束默认
`app_clean` 清理（`--keep` 保留克隆体）。

封印：`seal = sha256(规范化 JSON(发现清单))`——同输入同结论即同封印，报告可复核。
退出码：0 = 三面都跑完（verdict 看报告）；1 = 有面没跑成（工具缺失/越界，如实报）；2 = 用法错。
"""
import argparse
import hashlib
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 面 → 哪些键算"发现"（形状各异的回包统一看这几个）
ISSUE_KEYS = ("issues", "findings", "hits", "failures", "secrets", "danger",
              "dangerous", "definite", "results", "risky", "urls")
MAX_ITEMS = 8


def _bound(obj, depth=0):
    """有界摘要：标量直出；容器只报规模 + 前若干条（避免把整份报告塞进上下文）。"""
    if isinstance(obj, dict):
        if depth >= 3:
            return {"<dict>": len(obj)}
        return {k: _bound(v, depth + 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, (list, tuple)):
        head = [_bound(v, depth + 1) for v in list(obj)[:MAX_ITEMS]]
        return {"count": len(obj), "head": head}
    if isinstance(obj, str):
        return obj[:200]
    return obj


def _face_findings(name, res):
    """从一面回包里抽"发现"：ISSUE_KEYS 里非空的列表/计数。"""
    out = []
    if not isinstance(res, dict):
        return out
    for k, v in res.items():
        if k not in ISSUE_KEYS and k not in ("summary", "stats", "counts"):
            continue
        if isinstance(v, list) and v:
            out.append(f"{name}.{k}: {len(v)} 条 → {_bound(v[:2])}")
        elif isinstance(v, int) and v and k.endswith(("s", "total")):
            out.append(f"{name}.{k}: {v}")
    return out[:MAX_ITEMS]


def _seal(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _normalize(obj, snapshot: str, sandbox: str):
    """路径归一化：绝对路径换成占位符 ⇒ 封印跨目录/跨机可比（"同输入同封印"才成立）。"""
    if isinstance(obj, str):
        s = obj.replace(snapshot, "<clone>") if snapshot else obj
        return s.replace(sandbox, "<sandbox>") if sandbox else s
    if isinstance(obj, dict):
        return {k: _normalize(v, snapshot, sandbox) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v, snapshot, sandbox) for v in obj]
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="待审目录（原件只读；审计发生在克隆体）")
    ap.add_argument("--sandbox", help="隔离根（默认 %%TEMP%%/unified-rx-hunt）")
    ap.add_argument("--report", help="报告落盘路径（默认 <sandbox>/reports/<name>-<ts>.json）")
    ap.add_argument("--keep", action="store_true", help="保留克隆体（默认审完清理）")
    ap.add_argument("--json", action="store_true", help="只打 JSON（给脚本消费）")
    a = ap.parse_args()

    target = pathlib.Path(a.target).resolve()
    if not target.is_dir():
        print(f"用法错：--target 不是目录 {target}", file=sys.stderr)
        return 2
    sandbox = pathlib.Path(a.sandbox or os.path.join(
        os.environ.get("TEMP", "/tmp"), "unified-rx-hunt")).resolve()
    audit_root = sandbox / "audit"
    audit_root.mkdir(parents=True, exist_ok=True)
    # 同一个根：克隆体既在审计沙箱内（app_audit 口径），也在 fs 沙箱内（taint/secrets 口径）
    os.environ["UNIFIED_RX_AUDIT_SANDBOX"] = str(audit_root)
    os.environ["UNIFIED_RX_SANDBOX"] = str(sandbox)

    import registry  # 环境定好之后再导入
    import tools  # noqa: F401  触发注册

    t0 = time.time()
    faces: dict[str, dict] = {}
    findings: list[str] = []
    failed_faces: list[str] = []
    clone_info: dict = {}

    def run(name, tool, args, **kw):
        r = registry.call(tool, {**args, **kw})
        if r.get("ok"):
            raw = r.get("result") or {}
            issues = _face_findings(name, raw)
            faces[name] = {"ok": True, "result": _bound(raw), "issues": issues}
            findings.extend(issues)
        else:
            faces[name] = {"ok": False, "error": str(r.get("error"))[:200]}
            failed_faces.append(f"{name}: {str(r.get('error'))[:120]}")
        return faces[name]

    # ① 克隆（唯一落点：审计沙箱根；原件只读）
    cr = registry.call("app_clone", {"source_dir": str(target), "__authorized": True})
    if not cr.get("ok"):
        print(f"FAIL 克隆失败：{str(cr.get('error'))[:200]}", file=sys.stderr)
        return 1
    clone = cr["result"]
    snapshot = clone.get("snapshot") or ""
    clone_info = {k: clone.get(k) for k in ("snapshot", "files", "bytes", "verified",
                                            "fingerprint", "truncated_by")}
    faces["clone"] = {"ok": True, "result": clone_info}

    # ② 自攻 + 模糊（打本工具面，与 target 无关——如实标注）
    run("self_attack", "attack_cruise", {})
    # ③ 污点（克隆体）
    run("taint", "rust_taint_scan", {"root": snapshot})
    # ④ 静见（克隆体）
    run("audit", "app_audit", {"snapshot_dir": snapshot})
    run("secrets", "secrets_hunt", {"path": snapshot})

    verdict = "issues" if (findings or failed_faces) else "clean"
    # 封印只覆盖**与判定有关**的部分：目标名 + 三面结果 + 发现 + verdict。
    # 不覆盖路径/时间/克隆体指纹（每次跑都不同）——否则"同输入同封印"这条复核判据不成立。
    seal_payload = _normalize({
        "target_name": target.name, "verdict": verdict, "findings": findings,
        "failed_faces": failed_faces,
        # 只盖"判定相关"的部分：各面的 issue 抽取 + clone 面的清单标量。
        # **不盖各面原始回包**——那里有耗时/顺序等挥发字段（实测：盖了它封印不可复核）。
        "face_issues": {name: f.get("issues") for name, f in faces.items() if name != "clone"},
    }, snapshot, str(sandbox))
    report = {
        "target": str(target), "sandbox": str(sandbox), "snapshot": snapshot,
        "kept": bool(a.keep), "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
        "faces": faces, "findings": findings, "failed_faces": failed_faces,
        "verdict": verdict,
        "seal": _seal(seal_payload),
        "note": "自攻面打的是**本工具面**（与 target 无关）；污点/静见面打的是克隆体；"
                "封印只覆盖目标名 + 三面结果 + 发现（不含路径/时间）⇒ 同输入同封印",
    }

    rp = pathlib.Path(a.report) if a.report else (
        sandbox / "reports" / f"{target.name}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    if not a.keep and snapshot:
        registry.call("app_clean", {"target": snapshot, "__authorized": True})

    if a.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"VULN-HUNT target={target.name} verdict={verdict} "
              f"faces={len(faces)} 发现={len(findings)} 没跑成={len(failed_faces)} "
              f"{report['elapsed_s']}s")
        for line in findings:
            print(f"  ! {line}")
        for line in failed_faces:
            print(f"  ✗ {line}")
        print(f"  报告 {rp}")
        print(f"  {report['seal']}")
    return 1 if failed_faces else 0


if __name__ == "__main__":
    raise SystemExit(main())
