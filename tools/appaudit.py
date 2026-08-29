# -*- coding: utf-8 -*-
"""tools/appaudit.py —— S8 智能体/桌面应用自查域（3 工具）：app_clone / app_audit / app_clean

用户规则固化（2026-08-27）：遇到任何已安装智能体或桌面应用 → 先克隆到隔离沙箱，
审计只碰副本。原件零接触。

隔离三保证：
1. 克隆唯一落点 %TEMP%\\unified-rx-appaudit\\<时间戳>-<sha256前12位>-<净化名>\\，
   目录名由哈希派生，不吃原始路径的注入（对比 learn 过的 normpath 空白放大漏洞）
2. app_audit 强制只接受沙箱内路径（normcase + 严格子判定）——想直接审原件 = 结构性拒绝
3. app_clean 同样强制在沙箱内 + requires_auth（声明式授权，registry 层统一把关）

纯 stdlib：含 best-effort 的 asar 提取器（Electron 应用主逻辑都在 .asar 里，
不提取等于没审）。asar 内容基址不做拍脑袋假设——用叶节点自带的 SHA256 integrity
做自标定，候选基址全部试出真值。
"""
import hashlib
import json
import os
import re
import shutil
import struct
import time
from collections import Counter
from pathlib import Path

from registry import tool

_SANDBOX_ENV = "UNIFIED_RX_AUDIT_SANDBOX"
_MAX_FINDINGS = 400
_MAX_ASARS = 6
_MAX_ASAR_EXTRACT_MB = 48          # 单个 asar 提取总量上限
_MAX_ASAR_ENTRY_BYTES = 4 * 1024 * 1024  # 单条目提取上限

_TEXT_EXTS = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".html",
              ".htm", ".css", ".md", ".txt", ".yml", ".yaml", ".env", ".ini",
              ".cfg", ".toml", ".xml", ".ron",
              # 凭据/密钥载体也是文本——漏掉它们 = 秘密规则对真实文件全盲
              ".pem", ".key", ".crt", ".pub"}

_BINARY_INVENTORY_EXTS = {".exe", ".dll", ".node", ".asar"}

# 高置信秘密特征（kind=definite 只有私钥块；其余为 clue 待人工确认——H3 教训：分数不能骗人）
_SECRET_RULES = [
    ("private_key_block",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "definite"),
    ("api_key_sk", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "clue"),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "clue"),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "clue"),
    # 键名指认型（实测教训：df110257….5fn4 这类无前缀凭据靠值形状抓不到；
    # config.json 走查曾因只按值匹配漏掩码 → 键名与值同行的情形必须独立成规则）
    ("secret_by_key",
     re.compile(r"""["'](?:api_?key|apikey|secret|access_?token|refresh_?token|password|private_?key)["']\s*[:=]\s*["']([^"']{8,200})["']""", re.I),
     "clue"),
]

# JS 危险面线索（Electron 场景 child_process/openExternal 合法常见 → 全部 clue 级给证据行）
_SURFACE_RULES = [
    ("eval_call", re.compile(r"\beval\s*\(")),
    ("new_function", re.compile(r"new\s+Function\s*\(")),
    ("child_process", re.compile(r"""require\s*\(\s*["'](child_process|node:child_process)["']\s*\)""")),
    ("open_external", re.compile(r"openExternal\s*\(")),
    ("auto_updater", re.compile(r"autoUpdater|electron-updater")),
    ("protocol_register", re.compile(r"setAsDefaultProtocolClient|registerFileProtocol|registerSchemesAsPrivileged")),
]

_URL_RE = re.compile(r"https?://[a-zA-Z0-9.\-]+(?::\d+)?(?:/[^\s\"'<>)\\\]]*)?")
_AI_HOST_HINTS = ("anthropic", "openai", "deepseek", "baichuan", "hunyuan",
                  "moonshot", "zhipuai", "mistral", "dashscope", "volces")


def _sandbox_root():
    base = os.environ.get(_SANDBOX_ENV) or os.path.join(
        os.environ.get("TEMP", r"C:\Temp"), "unified-rx-appaudit")
    root = Path(os.path.abspath(base))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _strictly_under(p, root=None):
    """normcase 严格子判定：拒绝空白/等于根本身/大小写欺骗。"""
    root = root or _sandbox_root()
    if not isinstance(p, str) or not p.strip():
        return False
    try:
        rp = Path(os.path.abspath(p)).resolve(strict=False)
    except OSError:
        return False
    pc = os.path.normcase(str(rp))
    rc = os.path.normcase(str(Path(root).resolve(strict=False)))
    return pc != rc and pc.startswith(rc.rstrip("\\") + "\\")


@tool("app_clone", "克隆已安装应用到隔离审计沙箱（唯一落点，防名注入；返回清单指纹）。审原件请先克隆。", "appaudit",
      {"type": "object",
       "properties": {
           "source_dir": {"type": "string", "description": "待审计应用的安装目录（绝对路径）"},
           "max_files": {"type": "integer", "description": "文件数上限（默认 20000）"},
           "max_bytes": {"type": "integer", "description": "总字节数上限（默认 3GB）"},
       },
       "required": ["source_dir"]})
def app_clone(source_dir, max_files=20000, max_bytes=3 * 1024 * 1024 * 1024):
    if not isinstance(source_dir, str) or not source_dir.strip():
        return {"error": "source_dir 必须是非空字符串"}
    src = Path(source_dir.strip())
    if not src.is_absolute():
        return {"error": "必须绝对路径（防相对路径歧义）；例：D:\\rj\\AI\\Yan Agent"}
    try:
        src_real = src.resolve(strict=True)
    except OSError:
        return {"error": f"源不存在或不可达: {src}"}
    if not src_real.is_dir():
        return {"error": f"不是目录: {src_real}"}
    max_files = int(max_files)
    max_bytes = int(max_bytes)

    tag = hashlib.sha256(os.path.normcase(str(src_real)).encode()).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9_\-]+", "_", src_real.name)[:40] or "app"
    root = _sandbox_root()
    stem = time.strftime("%Y%m%d-%H%M%S") + "-" + tag
    dest = root / (stem + "-" + safe_name)
    k = 1
    while dest.exists():
        k += 1
        dest = root / (stem + f"-{k}-{safe_name}")
    dest.mkdir()

    manifest = hashlib.sha256()
    copied = copied_bytes = skipped_links = meta_warns = read_fails = 0
    truncation = None
    errors = []
    plan = []            # (rel, size) 内容写入成功项，验证阶段对照
    stopped = False
    for dirpath, dirnames, filenames in os.walk(src_real):
        if stopped:
            break
        rel_dir = os.path.relpath(dirpath, src_real)
        out_dir = dest if rel_dir == "." else dest / rel_dir
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            errors.append(f"{rel_dir}: mkdir {e.__class__.__name__}")
            dirnames[:] = []
            continue
        for fn in filenames:
            fpath = os.path.join(dirpath, fn)
            if os.path.islink(fpath):
                skipped_links += 1
                continue
            try:
                st = os.stat(fpath)
                if copied >= max_files:
                    truncation = "max_files"
                    stopped = True
                    break
                if copied_bytes + st.st_size > max_bytes:
                    truncation = "max_bytes"
                    stopped = True
                    break
                rel = "" if rel_dir == "." else os.path.join(rel_dir, fn)
                dst_path = os.path.join(out_dir, fn)
                # 分级：内容复制失败才算丢文件；元数据（mtime 等）失败只计警告。
                # 实测教训：锁定中的 cache 文件必炸；且必须【先开源句柄】——
                # shutil.copyfile 先建目标，源打不开会留下 0 字节残桩污染克隆。
                try:
                    with open(fpath, "rb") as fin:
                        with open(dst_path, "wb") as fout:
                            shutil.copyfileobj(fin, fout, 8 * 1024 * 1024)
                except OSError:
                    try:
                        if os.path.exists(dst_path):
                            os.remove(dst_path)
                    except OSError:
                        pass
                    raise
                try:
                    shutil.copystat(fpath, dst_path, follow_symlinks=False)
                except OSError:
                    meta_warns += 1
                copied += 1
                copied_bytes += st.st_size
                plan.append((rel.replace("\\", "/"), st.st_size))
                manifest.update(f"{rel.replace(chr(92), '/')}\t{st.st_size}\n".encode())
            except OSError as e:
                read_fails += 1
                if len(errors) < 30:
                    errors.append(f"{os.path.relpath(fpath, src_real)}: {e.__class__.__name__}")

    # 验证阶段：副本实盘复核（计数不一致必须显式暴露，不静默）
    v_files = v_bytes = 0
    for dp, dns, fns in os.walk(dest):
        for fn in fns:
            try:
                v_files += 1
                v_bytes += os.stat(os.path.join(dp, fn)).st_size
            except OSError:
                pass

    return {
        "snapshot": str(dest),
        "source": str(src_real),
        "files": copied,
        "bytes": copied_bytes,
        "verify_files": v_files,
        "verify_bytes": v_bytes,
        "verified": bool(v_files == len(plan) and v_bytes == copied_bytes),
        "inventory_digest": manifest.hexdigest(),
        "skipped_links": skipped_links,
        "meta_warns": meta_warns,
        "read_fails": read_fails,     # 内容级失败总数（errors 仅前 30 条样本）
        "errors": errors,
        "truncated_by": truncation,
    }


class _AsarError(Exception):
    pass


def _extract_asar(asar_path, out_dir):
    """best-effort asar 提取：文本类小文件落盘到 asar 同级 .audit-ext 目录。

    基址自标定：候选内容基址逐个用首个带 integrity 的叶节点验 SHA256。
    返回 {"extracted": n, "bytes": n, "error": str|None, "entries": n}
    """
    # S13 内存修复（两轮）：
    # ① 不再 read_bytes() 整个文件——头窗口流式读，句柄复用做标定与提取；
    # ② 不再对二进制窗口 decode('utf-8','replace')——替换符会把 8MB 二进制
    #    涨成 16-32MB 的宽字符串（实测 30MB 文件峰值 32MB 的真凶）。
    # 现在按"前导 u32 给出的候选长度"逐个 json.loads(bytes)：解析成功即定长，
    # 文件名保持原始 UTF-8 语义；全失败再扩窗重试（ pathology 头 >8MB 兜底）。
    fh = open(asar_path, "rb")          # noqa: SIM115 生命周期函数内闭合
    buf = fh.read(8 * 1024 * 1024)
    jstart = buf.find(b'{"files"')
    obj = None

    def _preamble_of(js):
        if js >= 16:
            return list(struct.unpack("<4I", buf[js - 16:js]))
        return [0, 0, 0]

    for round_no in range(3):
        if jstart >= 0:
            preamble = _preamble_of(jstart)
            # 候选头长度：前导三个 u32 与常量组合；升序试
            # （截断 JSON 在结尾解析失败极快，小候选先试省拷贝）
            lens = set()
            for v in preamble[-3:] + [4]:
                for dv in (0, -4, -8):
                    if v + dv > 0:
                        lens.add(v + dv)
            lens.add(len(buf) - jstart)
            for L in sorted(lens):
                if jstart + L > len(buf):
                    continue
                try:
                    obj = json.loads(buf[jstart:jstart + L])
                    break
                except Exception:
                    continue
        if obj is not None:
            break
        grow = fh.read(8 * 1024 * 1024)
        if not grow:
            break
        jstart_new = grow.find(b'{"files"')
        if jstart >= 0 or jstart_new < 0:
            buf += grow                  # 同窗续扫（跨窗边界截断的头）
        else:
            jstart = len(buf) + jstart_new
            buf += grow
    if jstart < 0 or obj is None:
        fh.close()
        raise _AsarError("未找到或未解析出 files 头")
    preamble = _preamble_of(jstart)
    cands = set()
    # 候选基址：JSON 起点前三个 u32 与常量 4 的组合偏移
    # （真实 asar 是三层 pickle 封装，手工算差值易错——所以全列出来让 SHA256 挑真值）
    vals = preamble[-3:] + [4]
    for v in vals:
        for dv in (0, 4, 8, -4):
            cand = jstart + v + dv
            if cand > jstart:          # 内容区必然在 JSON 之后；真值交由 SHA256 裁决
                cands.add(cand)

    leaves = []  # (rel, offset:int, size:int, hash:str|None)
    def walk(node, prefix):
        for name, ent in (node.get("files") or {}).items():
            rel = f"{prefix}/{name}" if prefix else name
            if "files" in ent:
                walk(ent, rel)
            elif isinstance(ent.get("size"), int):
                off = int(ent.get("offset") or 0)
                ih = (ent.get("integrity") or {}).get("hash")
                leaves.append((rel, off, ent["size"], ih))

    walk(obj.get("root", obj.get("top", obj)), "")

    # fh 已在函数头打开（流式复用，不再二次 open）

    def _hash_ok(cand, off, size, want):
        fh.seek(cand + off)
        data = fh.read(min(size, _MAX_ASAR_ENTRY_BYTES))
        return hashlib.sha256(data).hexdigest() == want

    # 基址自标定：用前几个带 integrity 的中小文本叶试候选基址，SHA256 命中即锁定。
    # 我对三层 pickle 封装的字节算术可以算错，条目的 SHA256 不会骗人。
    base = None
    hashed_probes = [l for l in leaves if l[3] and 32 <= l[2] <= 1024 * 1024
                     and Path(l[0]).suffix.lower() in _TEXT_EXTS][:8]
    for _, off0, size0, h0 in hashed_probes:
        for cand in sorted(cands):
            if _hash_ok(cand, off0, size0, h0):
                base = cand
                break
        if base is not None:
            break
    if base is None:
        fh.close()
        raise _AsarError("基址标定失败（integrity 全不匹配）")

    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    n_ext = n_skip = n_bytes = 0
    for rel, off, size, ih in leaves:
        if not rel or ".." in rel.split("/") or os.path.isabs(rel):
            n_skip += 1
            continue
        if Path(rel).suffix.lower() not in _TEXT_EXTS or size > _MAX_ASAR_ENTRY_BYTES \
                or n_bytes > _MAX_ASAR_EXTRACT_MB * 1024 * 1024 or n_ext >= 600:
            n_skip += 1
            continue
        fh.seek(base + off)
        data = fh.read(size)
        if ih and hashlib.sha256(data).hexdigest() != ih:
            n_skip += 1
            continue
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        n_ext += 1
        n_bytes += size
    fh.close()
    return {"extracted": n_ext, "bytes": n_bytes, "skipped": n_skip, "entries": len(leaves)}


def _mask(v):
    v = v.group(0) if hasattr(v, "group") else str(v)
    return v[:6] + "***len=" + str(len(v))


def _iter_text_rows(root):
    """产出 (rel_path, 行号, 行文本)。单文件头部 3MB 为限。"""
    for dp, dns, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            if os.path.splitext(fn)[1].lower() not in _TEXT_EXTS:
                continue
            rel = os.path.relpath(p, root).replace("\\", "/")
            if ".audit-ext/" in rel or "/.audit-ext/" in ("/" + rel):
                continue  # asar 提取件只走带 asar! 前缀的专用扫描，防双份计数
            try:
                with open(p, "rb") as f:
                    head = f.read(3 * 1024 * 1024)
            except OSError:
                continue
            for i, line in enumerate(head.decode("utf-8", "replace").splitlines(), 1):
                yield rel, i, line


@tool("app_audit", "审计隔离沙箱内的克隆（拒绝原件路径）：JS危险面/秘密掩码采集/URL清单/二进制盘点/asar提取后复扫", "appaudit",
      {"type": "object",
       "properties": {
           "snapshot_dir": {"type": "string", "description": "app_clone 返回的 snapshot 路径"},
           "with_asar": {"type": "boolean", "description": "是否提取 .asar 文本条目再扫（默认 true）"},
       },
       "required": ["snapshot_dir"]})
def app_audit(snapshot_dir, with_asar=True):
    if not isinstance(snapshot_dir, str) or not snapshot_dir.strip():
        return {"error": "snapshot_dir 必须是非空字符串"}
    root = Path(snapshot_dir.strip())
    if not _strictly_under(str(root)):
        return {"error": "只允许审计隔离沙箱内的克隆；先 app_clone 再把它的 snapshot 路径传进来"}
    if not root.is_dir():
        return {"error": f"克隆不存在: {root}"}

    findings = []
    hosts = Counter()

    def emit(kind, label, file, line, detail):
        if len(findings) < _MAX_FINDINGS:
            findings.append({"kind": kind, "label": label,
                             "file": file, "line": line, "detail": detail})

    scanned = 0
    seen_js_labels = {}
    for rel, ln, line in _iter_text_rows(root):
        stripped = line.strip()
        if not stripped or len(stripped) > 800:
            continue
        scanned_hit = False
        for label, rx in _SURFACE_RULES:
            m = rx.search(line)
            if m and (label not in seen_js_labels or seen_js_labels[label] <= 50):
                seen_js_labels.setdefault(label, 0)
                seen_js_labels[label] += 1
                emit("clue", label, rel, ln, _mask(m.group(0)))
                scanned_hit = True
        for label, rx, kind in _SECRET_RULES:
            m = rx.search(line)
            if m:
                emit(kind, label, rel, ln, _mask(m.group(0)))  # 只落掩码，绝不回显原值
                scanned_hit = True
        for m in _URL_RE.finditer(line):
            host = m.group(0).split("//", 1)[-1].split("/", 1)[0].split(":")[0].lower()
            if host:
                hosts[host] += 1
        if scanned_hit:
            scanned += 1
    del seen_js_labels

    asar_report = []
    if with_asar:
        asars = []
        for dp, dns, fns in os.walk(root):
            for fn in fns:
                if fn.lower().endswith(".asar"):
                    asars.append(Path(dp) / fn)
        for ap in asars[:_MAX_ASARS]:
            ext_dir = Path(str(ap) + ".audit-ext")
            stat = {"asar": os.path.relpath(ap, root).replace("\\", "/")}
            try:
                stat.update(_extract_asar(ap, ext_dir))
            except Exception as e:
                stat["error"] = f"{e.__class__.__name__}: {e}"
            asar_report.append(stat)
            if stat.get("extracted"):
                # 提取件在克隆内 → 直接复扫同套规则，file 前缀 asar!
                sub = os.path.relpath(ext_dir, root)
                for rel, ln, line in _iter_text_rows(ext_dir):
                    stripped = line.strip()
                    if not stripped or len(stripped) > 800:
                        continue
                    for label, rx in _SURFACE_RULES:
                        m = rx.search(line)
                        if m:
                            emit("clue", "asar:" + label, f"{sub}/{rel}", ln, _mask(m.group(0)))
                    for label, rx, kind in _SECRET_RULES:
                        m = rx.search(line)
                        if m:
                            emit(kind, "asar:" + label, f"{sub}/{rel}", ln, _mask(m.group(0)))

    binaries = []
    for dp, dns, fns in os.walk(root):
        for fn in fns:
            if os.path.splitext(fn)[1].lower() in _BINARY_INVENTORY_EXTS:
                try:
                    binaries.append({
                        "file": os.path.relpath(os.path.join(dp, fn), root).replace("\\", "/"),
                        "size": os.stat(os.path.join(dp, fn)).st_size})
                except OSError:
                    pass
    binaries.sort(key=lambda x: -x["size"])

    findings.sort(key=lambda f: (f["kind"] != "definite", f["label"], f["file"], f["line"]))
    ai_hosts = {h: c for h, c in hosts.items() if any(k in h for k in _AI_HOST_HINTS)}
    return {
        "snapshot": str(root),
        "hit_lines": scanned,
        "definite": sum(1 for f in findings if f["kind"] == "definite"),
        "clues": sum(1 for f in findings if f["kind"] == "clue"),
        "findings": findings,
        "url_host_top": [{"host": h, "count": c} for h, c in hosts.most_common(25)],
        "ai_endpoint_hosts": ai_hosts,
        "native_binaries_top": binaries[:15],
        "binaries_total": len(binaries),
        "asar": asar_report,
    }


@tool("app_clean", "清理隔离沙箱内的克隆（requires_auth；严格限沙箱内，越界一律拒绝）", "appaudit",
      {"type": "object",
       "properties": {
           "target": {"type": "string", "description": "app_clone 返回的 snapshot 路径"},
       },
       "required": ["target"]},
      requires_auth=True)
def app_clean(target, __authorized=False):
    del __authorized
    if not isinstance(target, str) or not target.strip():
        return {"error": "target 必须是非空字符串"}
    if not _strictly_under(target):
        return {"error": "拒绝：清理目标必须在隔离沙箱内（app_clone 的 snapshot 路径）"}
    p = Path(os.path.abspath(target)).resolve(strict=False)
    try:
        shutil.rmtree(p)
        return {"removed": True, "path": str(p)}
    except OSError as e:
        return {"error": f"清理失败: {e.__class__.__name__}: {e}（沙箱内可手动重试；多半是文件被占用）"}
