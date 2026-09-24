"""scripts/tool_sweep.py —— CI 工具/依赖体检（S171，队列 ⑤）：列出"当前 vs 最新"。

盘两面：
1. workflow 里的 `uses: owner/repo@<sha> # vX.Y.Z` —— 取注释里的 tag 作"当前"，问 GitHub
   最新 release；
2. `.github/ci-requirements.txt` 里 `pkg==ver` 的钉版（未钉的如实标"浮动=CI 取最新"），
   问 PyPI 最新版本。

**不是门**：要联网、上游抖动会让门假红。它是"升级前先量"的那把尺子（`--check-updates`
用退出码 1 表示"有落后项"，方便将来接进定时任务，但不进 local_gate/CI 门链）。
网络不可用时如实报错并按 `--offline` 语义只打本地清单（不假装"都是最新"）。

取数只走**白名单主机 + https + 不跟重定向**，并在连接前解析并拒私网/环回/链路本地地址
（脚本只查 pypi/github 两处公开元数据，不该有通用抓取能力）。本机若把公开域名解析到
本地代理（实测 `api.github.com` → 127.0.0.1），守卫会拦下——要跑就显式设
`TOOL_SWEEP_ALLOW_LOOPBACK=1`（脚本会打印"已放行环回"，不会静默）。
"""
import argparse
import ipaddress
import json
import os
import pathlib
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

_USES = re.compile(r"uses:\s*([A-Za-z0-9._/-]+)@([0-9a-f]{40})\s*#\s*(v?[\w.\-]+)")
_PIN = re.compile(r"^\s*([A-Za-z0-9._\-]+)\s*==\s*([\w.\-]+)\s*$")
_LOOSE = re.compile(r"^\s*([A-Za-z0-9._\-]+)\s*$")
_ALLOWED_HOSTS = {"pypi.org", "api.github.com"}
_ALLOW_LOOPBACK = os.environ.get("TOOL_SWEEP_ALLOW_LOOPBACK") == "1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟重定向：跳转一律拒绝（防被引到内网/元数据地址）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(f"拒绝重定向 → {newurl}")


def _guard(url: str) -> None:
    """协议 + 主机白名单 + 解析后地址边界（私网/环回/链路本地一律拒）。"""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https":
        raise ValueError(f"仅允许 https：{url}")
    if u.hostname not in _ALLOWED_HOSTS:
        raise ValueError(f"主机不在白名单：{u.hostname}")
    for info in socket.getaddrinfo(u.hostname, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback and _ALLOW_LOOPBACK:
            print(f"[tool-sweep] 注意：{u.hostname} 解析到环回 {ip}（本地代理），"
                  f"已按 TOOL_SWEEP_ALLOW_LOOPBACK=1 放行", file=sys.stderr)
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"解析到非公网地址：{u.hostname} → {ip}")


def _get_json(url: str, timeout: int = 20) -> dict:
    _guard(url)
    req = urllib.request.Request(url, headers={"User-Agent": "unified-rx-tool-sweep"})
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=timeout) as r:
        return json.load(r)


def _latest_pypi(pkg: str) -> str:
    try:
        return _get_json(f"https://pypi.org/pypi/{pkg}/json")["info"]["version"]
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        return f"(查询失败 {type(e).__name__})"


def _latest_gh(repo: str) -> str:
    """按**版本 tag 列表**取最新（`releases/latest` 对某些仓返回的不是 action 版本，
    如 github/codeql-action 发的是 `codeql-bundle-v*` ⇒ 会假报落后）。"""
    repo = "/".join(repo.split("/")[:2])
    try:
        tags = _get_json(f"https://api.github.com/repos/{repo}/tags?per_page=100")
        vers = [t["name"] for t in tags if re.fullmatch(r"v\d+(\.\d+)*", t.get("name", ""))]

        def key(v: str) -> tuple:
            return tuple(int(x) for x in v.lstrip("v").split("."))

        return max(vers, key=key) if vers else "(无版本 tag)"
    except (urllib.error.URLError, OSError, KeyError, ValueError, TypeError) as e:
        return f"(查询失败 {type(e).__name__})"


def _norm(v: str) -> str:
    return v.lstrip("v").strip()


def collect(root: pathlib.Path) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """(actions[(名字, 当前 tag, 钉的 SHA)], deps[(包, 钉版 或 ''=浮动)])。"""
    actions, seen = [], set()
    for wf in sorted((root / ".github" / "workflows").glob("*.yml")):
        for m in _USES.finditer(wf.read_text(encoding="utf-8")):
            name, sha, tag = m.group(1), m.group(2), m.group(3)
            if (name, tag) in seen:
                continue
            seen.add((name, tag))
            actions.append((name, tag, sha))
    deps = []
    req = root / ".github" / "ci-requirements.txt"
    if req.is_file():
        for line in req.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            pin = _PIN.match(line)
            if pin:
                deps.append((pin.group(1), pin.group(2)))
                continue
            loose = _LOOSE.match(line)
            if loose:
                deps.append((loose.group(1), ""))
    return actions, deps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--offline", action="store_true", help="只打本地清单，不查上游")
    ap.add_argument("--check-updates", action="store_true", help="有落后项则退出码 1")
    a = ap.parse_args()
    root = pathlib.Path(a.root).resolve()
    actions, deps = collect(root)
    stale = 0

    print(f"TOOL-SWEEP root={root}  action={len(actions)} 依赖={len(deps)}"
          f"{'（offline）' if a.offline else ''}")
    print("  [actions]")
    for name, tag, sha in actions:
        latest = "(offline)" if a.offline else _latest_gh(name)
        behind = not a.offline and not latest.startswith("(") and _norm(latest) != _norm(tag)
        stale += int(behind)
        flag = "✗ 落后" if behind else ("·" if a.offline else "· 最新")
        print(f"    {flag} {name:28s} {tag:10s} → {latest:12s} {sha[:12]}")
    print("  [deps]")
    for pkg, ver in deps:
        latest = "(offline)" if a.offline else _latest_pypi(pkg)
        if not ver:
            print(f"    · 浮动  {pkg:22s} (CI 取最新) → {latest}")
            continue
        behind = not a.offline and not latest.startswith("(") and _norm(latest) != _norm(ver)
        stale += int(behind)
        flag = "✗ 落后" if behind else ("·" if a.offline else "· 最新")
        print(f"    {flag} {pkg:22s} {ver:10s} → {latest}")

    if a.offline:
        print("[tool-sweep] offline：只打了本地清单，未查上游（不假装最新）")
        return 0
    print(f"[tool-sweep] 落后项 {stale} 个（升级后要跑门链复核；ruff 之类的升级会动静态门基线）")
    return 1 if (a.check_updates and stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
