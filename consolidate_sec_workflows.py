#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
# -*- coding: utf-8 -*-
"""
Security Workflows 分发 / 运行 / 信息合并 / 清理 脚本
=====================================================
流程：
  1. 读取 sec-workflows 目录下的全部安全扫描 workflow（*.yml/*.yaml）
  2. 列出 bfxh 名下所有仓库（含私有），逐个上传到 .github/workflows/
  3. 触发每个 workflow 的 workflow_dispatch 并轮询运行结果
  4. 信息合并：同一 workflow 在全部仓库均通过 -> 归入 GOOD（合并成功记录）
               任一仓库失败/异常              -> 归入 BAD（保留不删，合并失败记录）
  5. 清理：删除 GOOD 列表中的 workflow 文件；BAD 列表全部保留
  6. 生成 sec-workflows-report.md 合并报告

环境变量：
  GH_OWNER   仓库归属账号，默认 bfxh
  GH_TOKEN   GitHub PAT（需 repo + workflow 权限），默认取 gh CLI 登录态
  SEC_DIR    workflow 源目录，默认 sec-workflows
  TARGET_REPO 仅处理指定仓库名（逗号分隔，留空=全部）
  DRY_RUN    true=只生成报告不真正删除
  SKIP_RUN   true=只部署不触发运行
  MAX_WAIT   等待单个仓库全部运行完成的上限秒数，默认 1800
"""
import os
import sys
import json
import time
import glob
import base64
import urllib.request
import urllib.error

OWNER = os.environ.get("GH_OWNER", "bfxh")
TOKEN = os.environ.get("GH_TOKEN", "")
SEC_DIR = os.environ.get("SEC_DIR", "sec-workflows")
TARGET_REPO = os.environ.get("TARGET_REPO", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("true", "1")
SKIP_RUN = os.environ.get("SKIP_RUN", "false").lower() in ("true", "1")
MAX_WAIT = int(os.environ.get("MAX_WAIT", "1800"))
POLL_INTERVAL = 15
WORKFLOW_DIR = ".github/workflows"


def api(path, method="GET", data=None):
    """GitHub REST API 调用，返回解析后的 JSON；失败时返回含 __error__ 的 dict。"""
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    body = None
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(req, body, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"__error__": e.code, "__body__": e.read().decode("utf-8")[:500]}
    except Exception as e:  # noqa: BLE001
        return {"__error__": -1, "__body__": str(e)}


def gh(*args):
    """调用 gh CLI（兜底：脚本也可在本地直接跑）。"""
    cmd = ["gh"] + list(args)
    env = dict(os.environ)
    if TOKEN:
        env["GH_TOKEN"] = TOKEN
    r = subprocess_run(cmd, env)
    if r["code"] != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {r['err'][:500]}")
    return r["out"]


def subprocess_run(cmd, env=None):
    import subprocess
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return {"code": p.returncode, "out": p.stdout, "err": p.stderr}


def list_repos():
    """列出 OWNER 名下所有仓库（含私有），返回 [{name, branch}]。"""
    out = gh("repo", "list", OWNER, "--limit", "200",
             "--json", "name,visibility,defaultBranchRef")
    repos = json.loads(out)
    result = []
    for r in repos:
        name = r["name"]
        if TARGET_REPO:
            targets = [t.strip() for t in TARGET_REPO.split(",") if t.strip()]
            if name not in targets:
                continue
        branch = (r.get("defaultBranchRef") or {}).get("name") or "main"
        result.append({"name": name, "branch": branch})
    return result


def list_local_workflows():
    """返回 sec-workflows 目录下的全部 workflow 文件名列表。"""
    pats = glob.glob(os.path.join(SEC_DIR, "*.yml")) + \
           glob.glob(os.path.join(SEC_DIR, "*.yaml"))
    return sorted(os.path.basename(p) for p in pats)


def list_remote_workflows(repo, branch):
    """列出仓库 .github/workflows 下已有文件，返回 {文件名: sha}。"""
    r = api(f"/repos/{OWNER}/{repo}/contents/{WORKFLOW_DIR}?ref={branch}")
    if not isinstance(r, list):
        return {}
    return {f["name"]: f.get("sha") for f in r if f.get("type") == "file"}


def read_local(name):
    with open(os.path.join(SEC_DIR, name), "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def upload_workflow(repo, branch, name, content):
    """创建或更新 workflow 文件，返回 (ok, msg)。"""
    path = f"{WORKFLOW_DIR}/{name}"
    r = api(f"/repos/{OWNER}/{repo}/contents/{path}", method="PUT", data={
        "message": f"chore(sec): add security workflow {name}",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    })
    if "__error__" in r:
        return False, f"HTTP {r['__error__']}: {r['__body__'][:200]}"
    return True, ""


def delete_workflow(repo, branch, name, sha):
    """删除 workflow 文件，返回 (ok, msg)。"""
    path = f"{WORKFLOW_DIR}/{name}"
    r = api(f"/repos/{OWNER}/{repo}/contents/{path}", method="DELETE", data={
        "message": f"chore(sec): remove consolidated passing workflow {name}",
        "sha": sha,
        "branch": branch,
    })
    if "__error__" in r:
        return False, f"HTTP {r['__error__']}: {r['__body__'][:200]}"
    return True, ""


def trigger_workflow(repo, branch, wf_name):
    """触发 workflow_dispatch。wf_name 为文件路径，如 .github/workflows/gitleaks.yml。"""
    r = api(f"/repos/{OWNER}/{repo}/actions/workflows/{wf_name}/dispatches",
            method="POST", data={"ref": branch})
    return not ("__error__" in r)


def workflow_run_status(repo, wf_name):
    """获取某 workflow 最近一次运行状态，返回 (status, conclusion)。"""
    r = api(f"/repos/{OWNER}/{repo}/actions/workflows/{wf_name}/runs?per_page=1")
    if "__error__" in r or not r.get("workflow_runs"):
        return "no-run", "no-run"
    run = r["workflow_runs"][0]
    return run.get("status", "unknown"), run.get("conclusion", "unknown")


def wait_repo_runs(repo, wf_names, branch):
    """触发并等待一个仓库所有 workflow 完成，返回 {wf_name: (status, conclusion)}。"""
    results = {}
    if SKIP_RUN:
        return {n: ("skipped", "skipped") for n in wf_names}

    # 先全部触发
    for n in wf_names:
        wf_path = f"{WORKFLOW_DIR}/{n}"
        if not trigger_workflow(repo, branch, wf_path):
            results[n] = ("trigger-fail", "trigger-fail")
        else:
            results[n] = ("queued", "queued")
        time.sleep(1)

    deadline = time.time() + MAX_WAIT
    pending = [n for n, v in results.items() if v == ("queued", "queued")]
    while pending and time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        still = []
        for n in pending:
            status, conclusion = workflow_run_status(repo, f"{WORKFLOW_DIR}/{n}")
            if status == "completed":
                results[n] = (status, conclusion)
            else:
                still.append(n)
        pending = still
    for n in pending:
        results[n] = ("timeout", "timeout")
    return results


def main():
    if not TOKEN:
        print("未提供 GH_TOKEN，将尝试复用 gh CLI 登录态。")
    if not os.path.isdir(SEC_DIR):
        print(f"源目录不存在: {SEC_DIR}", file=sys.stderr)
        sys.exit(1)

    workflows = list_local_workflows()
    if not workflows:
        print("sec-workflows 目录下没有找到任何 workflow 文件", file=sys.stderr)
        sys.exit(1)
    print(f"[1/5] 本地 workflow 数量: {len(workflows)}")

    repos = list_repos()
    print(f"[2/5] 目标仓库: {len(repos)} 个 -> {[r['name'] for r in repos]}")

    # 部署
    deploy_report = []
    for repo in repos:
        remote = list_remote_workflows(repo["name"], repo["branch"])
        added, updated, failed = [], [], []
        for name in workflows:
            content = read_local(name)
            ok, msg = upload_workflow(repo["name"], repo["branch"], name, content)
            if ok:
                (updated if name in remote else added).append(name)
            else:
                failed.append(f"{name}: {msg}")
        deploy_report.append({
            "repo": repo["name"], "branch": repo["branch"],
            "added": added, "updated": updated, "failed": failed,
        })
        print(f"    {repo['name']}: +{len(added)} 更新{len(updated)} 失败{len(failed)}")

    # 运行并收集
    print(f"[3/5] 触发运行并收集结果 (SKIP_RUN={SKIP_RUN})")
    run_matrix = {}  # {wf_name: {repo: conclusion}}
    for repo in repos:
        results = wait_repo_runs(repo["name"], workflows, repo["branch"])
        for n, (status, conclusion) in results.items():
            run_matrix.setdefault(n, {})[repo["name"]] = conclusion
        print(f"    {repo['name']} 完成: {sum(1 for v in results.values() if v[0]=='completed')}/{len(results)}")

    # 信息合并
    print("[4/5] 信息合并：全通过 -> GOOD(删)，有失败 -> BAD(留)")
    good = {}  # {wf_name: {repo: conclusion}}  全部通过
    bad = {}   # {wf_name: {repo: conclusion}}  存在失败
    for n, per_repo in run_matrix.items():
        if not per_repo:
            bad[n] = {}
            continue
        conclusions = set(per_repo.values())
        if conclusions == {"success"}:
            good[n] = per_repo
        else:
            bad[n] = per_repo

    # 清理
    print("[5/5] 清理 GOOD 列表")
    cleanup = {"deleted": [], "keep": [], "errors": []}
    if not DRY_RUN:
        for n in good:
            for repo in repos:
                remote = list_remote_workflows(repo["name"], repo["branch"])
                sha = remote.get(n)
                if sha is None:
                    continue
                ok, msg = delete_workflow(repo["name"], repo["branch"], n, sha)
                if ok:
                    cleanup["deleted"].append(f"{repo['name']}/{n}")
                else:
                    cleanup["errors"].append(f"{repo['name']}/{n}: {msg}")
        for n in bad:
            cleanup["keep"].append(n)
    else:
        cleanup["deleted"] = [f"DRY-RUN(未实际删除) {n}" for n in good]
        cleanup["keep"] = list(bad.keys())

    # 报告
    report = build_report(workflows, repos, deploy_report, run_matrix,
                          good, bad, cleanup)
    out_path = os.path.join(os.getcwd(), "sec-workflows-report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n报告已生成: {out_path}")
    print(f"GOOD(通过/已删): {len(good)}  BAD(保留): {len(bad)}")


def build_report(workflows, repos, deploy_report, run_matrix, good, bad, cleanup):
    lines = []
    lines.append("# Security Workflows 合并报告")
    lines.append("")
    lines.append(f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 账号: {OWNER}")
    lines.append(f"- workflow 总数: {len(workflows)}")
    lines.append(f"- 仓库数: {len(repos)}")
    lines.append(f"- DRY_RUN: {DRY_RUN}")
    lines.append(f"- SKIP_RUN: {SKIP_RUN}")
    lines.append("")
    lines.append("## 一、部署情况")
    lines.append("")
    lines.append("| 仓库 | 新增 | 更新 | 失败 |")
    lines.append("|------|------|------|------|")
    for d in deploy_report:
        lines.append(f"| {d['repo']} | {len(d['added'])} | {len(d['updated'])} | {len(d['failed'])} |")
    lines.append("")
    lines.append("## 二、信息合并结果")
    lines.append("")
    lines.append(f"### GOOD（全部仓库通过，已删除）: {len(good)}")
    lines.append("")
    lines.append("| workflow | 仓库结论 |")
    lines.append("|----------|----------|")
    for n, per_repo in sorted(good.items()):
        detail = ", ".join(f"{k}={v}" for k, v in sorted(per_repo.items()))
        lines.append(f"| {n} | {detail} |")
    lines.append("")
    lines.append(f"### BAD（存在失败，保留不删）: {len(bad)}")
    lines.append("")
    lines.append("| workflow | 仓库结论 |")
    lines.append("|----------|----------|")
    for n, per_repo in sorted(bad.items()):
        detail = ", ".join(f"{k}={v}" for k, v in sorted(per_repo.items())) or "(未运行)"
        lines.append(f"| {n} | {detail} |")
    lines.append("")
    lines.append("## 三、清理情况")
    lines.append("")
    lines.append(f"- 已删除: {len(cleanup['deleted'])}")
    for item in cleanup["deleted"]:
        lines.append(f"  - {item}")
    lines.append(f"- 保留(BAD): {len(cleanup['keep'])}")
    for item in cleanup["keep"]:
        lines.append(f"  - {item}")
    if cleanup["errors"]:
        lines.append(f"- 错误: {len(cleanup['errors'])}")
        for item in cleanup["errors"]:
            lines.append(f"  - {item}")
    lines.append("")
    lines.append("## 四、规则说明")
    lines.append("")
    lines.append("- 同一个 workflow 在所有仓库运行结论均为 success -> GOOD，合并成功记录并从各仓库删除")
    lines.append("- 任一仓库失败/超时/未运行 -> BAD，保留文件，合并失败记录供排查")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
