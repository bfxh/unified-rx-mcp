"""tools/installerscan.py —— 伪造安装包扫描（1 工具）：installer_scan

抓"假安装包"：网上下载的 setup.exe 在双击前过一遍三层证据：
1. 数字签名（Windows；ctypes 直调 wintrust/crypt32，零依赖）：未签名 /
   签名无效（签后文件被改）/ 吊销 / 过期 / 签名者与自称厂商不符；
2. PE 版本资源（version.dll FFI）：CompanyName / ProductName / FileVersion /
   OriginalFilename——自称知名厂商却拿不出有效签名 = 伪造的典型形态；
3. 文件名与来源启发式：破解/激活/keygen 类关键词、双扩展名、MOTW
   （Zone.Identifier=3 = 来自网络）。

判定三档 + unknown：clean / suspicious / likely-fake / metadata-only。
静态启发不是保证：verdict 是线索不是结论，安装前人工确认；
与 file_scan 的"签名"（用户自定义字面量/哈希匹配）互补，两者无重叠。
非 Windows 退化为第 3 层（签名/版本资源层如实报 unavailable）。
"""
import ctypes
import hashlib
import os
import re
import sys
import time

from registry import tool
from tools.fs import _resolve as _fs_resolve

_IS_WIN = sys.platform == "win32"

# 判定档位
V_TRUSTED = "signed-trusted"
V_SUSPICIOUS = "suspicious"
V_FAKE = "likely-fake"
V_UNKNOWN = "unknown"

# 自称这些厂商却没有有效签名 = 伪造的典型形态（小写规范化后比对；可自行扩充）
_KNOWN_VENDORS = {
    "microsoft corporation", "microsoft", "google llc", "mozilla corporation",
    "adobe inc.", "apple inc.", "tencent technology (shenzhen) company limited",
    "tencent", "腾讯科技（深圳）有限公司", "阿里巴巴（中国）网络技术有限公司",
    "网易（杭州）网络有限公司", "北京奇虎科技有限公司", "华为技术有限公司",
    "kingsoft corporation", "金山软件", "哔哩哔哩", "字节跳动",
}

# 文件名启发式
_SUSPECT_RE = re.compile(
    r"crack|keygen|patcher|注册机|破解|激活工具|免激活|已激活|去广告|绿化版", re.IGNORECASE)
_DOUBLE_EXT_RE = re.compile(r"\.(pdf|jpg|jpeg|png|docx?|xlsx?|pptx?|txt|zip|rar)\.exe$", re.IGNORECASE)
_COPY_SUFFIX_RE = re.compile(r"\(\d+\)\.exe$", re.IGNORECASE)

_EXTS = (".exe", ".msi")

# ---- 常见 HRESULT（WinVerifyTrust 判据）----
_TRUST_OK = 0
_TRUST_MAP = {
    0x800B0100: "unsigned",              # TRUST_E_NOSIGNATURE
    0x800B0001: "unsigned",              # TRUST_E_PROVIDER_UNKNOWN：非 PE，无签名结构
    0x800B0009: "unsigned",              # TRUST_E_SUBJECT_FORM_UNKNOWN：同上
    0x80096010: "modified",              # TRUST_E_BAD_DIGEST：签后文件被改
    0x800B0101: "expired",               # CERT_E_EXPIRED
    0x800B0104: "untrusted-root",        # CERT_E_UNTRUSTEDROOT
    0x80092010: "revoked",               # CERT_E_REVOKED
    0x80092012: "revocation-unchecked",  # CRYPT_E_NO_REVOCATION_CHECK
    0x80092013: "revocation-offline",    # CRYPT_E_REVOCATION_OFFLINE
    0x800B010A: "unsigned",              # CERT_E_NO_SIGNER
    0x80092002: "unreadable",            # CRYPT_E_FILE_ERROR
}

if _IS_WIN:
    _wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _version = ctypes.WinDLL("version", use_last_error=True)

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    _ACTION_GENERIC_VERIFY_V2 = _GUID(
        0x00AAC56B, 0xCD44, 0x11D0,
        (ctypes.c_ubyte * 8)(0xBC, 0x3C, 0x00, 0xA0, 0xC9, 0x23, 0xE4, 0x9C))

    class _WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", ctypes.c_ulong),
                    ("pcwszFilePath", ctypes.c_wchar_p),
                    ("hFile", ctypes.c_void_p),
                    ("pgKnownSubject", ctypes.c_void_p)]

    class _WTD_UNION(ctypes.Union):
        _fields_ = [("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO))]

    class _WINTRUST_DATA(ctypes.Structure):
        _fields_ = [("cbStruct", ctypes.c_ulong),
                    ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p),
                    ("dwUIChoice", ctypes.c_ulong),
                    ("fdwRevocationChecks", ctypes.c_ulong),
                    ("dwUnionChoice", ctypes.c_ulong),
                    ("pUnion", _WTD_UNION),
                    ("dwStateAction", ctypes.c_ulong),
                    ("hWVTStateData", ctypes.c_void_p),
                    ("pwszURLReference", ctypes.c_void_p),
                    ("dwProvFlags", ctypes.c_ulong),
                    ("dwUIContext", ctypes.c_ulong),
                    ("pSignatureSettings", ctypes.c_void_p)]

    _wintrust.WinVerifyTrust.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(_WINTRUST_DATA)]
    _wintrust.WinVerifyTrust.restype = ctypes.c_long

    class _CERT_CONTEXT(ctypes.Structure):
        _fields_ = [("dwCertEncodingType", ctypes.c_ulong),
                    ("pbCertEncoded", ctypes.c_void_p),
                    ("cbCertEncoded", ctypes.c_ulong),
                    ("pCertInfo", ctypes.c_void_p),
                    ("hCertStore", ctypes.c_void_p)]

    class _VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [("dwSignature", ctypes.c_ulong),
                    ("dwStrucVersion", ctypes.c_ulong),
                    ("dwFileVersionMS", ctypes.c_ulong),
                    ("dwFileVersionLS", ctypes.c_ulong),
                    ("dwProductVersionMS", ctypes.c_ulong),
                    ("dwProductVersionLS", ctypes.c_ulong),
                    ("dwFlags", ctypes.c_ulong),
                    ("dwFileOS", ctypes.c_ulong),
                    ("dwFileType", ctypes.c_ulong),
                    ("dwFileSubtype", ctypes.c_ulong),
                    ("dwFileDateMS", ctypes.c_ulong),
                    ("dwFileDateLS", ctypes.c_ulong)]


def _win_verify_trust(path):
    """Authenticode 校验。返回 (status, detail)；status ∈ _TRUST_MAP 值域。"""
    fi = _WINTRUST_FILE_INFO()
    fi.cbStruct = ctypes.sizeof(fi)
    fi.pcwszFilePath = str(path)
    uni = _WTD_UNION()
    uni.pFile = ctypes.pointer(fi)
    wd = _WINTRUST_DATA()
    wd.cbStruct = ctypes.sizeof(wd)
    wd.dwUIChoice = 2          # WTD_UI_NONE：绝不弹窗
    wd.fdwRevocationChecks = 0  # WTD_REVOKE_NONE：吊销检查交给上层策略
    wd.dwUnionChoice = 1       # WTD_CHOICE_FILE
    wd.pUnion = uni
    wd.dwStateAction = 0       # 一次性校验，不留状态句柄
    ret = _wintrust.WinVerifyTrust(
        None, ctypes.byref(_ACTION_GENERIC_VERIFY_V2), ctypes.byref(wd))
    code = ret & 0xFFFFFFFF
    if code == _TRUST_OK:
        return "trusted", ""
    status = _TRUST_MAP.get(code, f"hresult-0x{code:08X}")
    return status, f"WinVerifyTrust=0x{code:08X}"


def _signer_display_name(path):
    """签名者简单显示名（CMSG_SIGNER_CERT_PARAM → CertGetNameStringW）；拿不到返回 None。"""
    content_type = ctypes.c_ulong()
    format_type = ctypes.c_ulong()
    hstore = ctypes.c_void_p()
    hmsg = ctypes.c_void_p()
    pctx = ctypes.c_void_p()
    if not _crypt32.CryptQueryObject(
            1, str(path),                       # CERT_QUERY_OBJECT_FILE
            0x00003FFE, 0x00003FFE, 0,          # CONTENT_FLAG_ALL / FORMAT_FLAG_ALL
            None, ctypes.byref(content_type), ctypes.byref(format_type),
            ctypes.byref(hstore), ctypes.byref(hmsg), ctypes.byref(pctx)):
        return None
    try:
        cb = ctypes.c_ulong()
        if not _crypt32.CryptMsgGetParam(hmsg, 9, 0, None, ctypes.byref(cb)):
            return None                          # CMSG_SIGNER_CERT_PARAM
        buf = ctypes.create_string_buffer(cb.value)
        if not _crypt32.CryptMsgGetParam(hmsg, 9, 0, buf, ctypes.byref(cb)):
            return None
        cert = ctypes.cast(buf, ctypes.POINTER(_CERT_CONTEXT)).contents
        need = _crypt32.CertGetNameStringW(
            ctypes.byref(cert), 4, 0, None, None, 0)  # CERT_NAME_SIMPLE_DISPLAY_TYPE
        if not need:
            return None
        out = ctypes.create_unicode_buffer(need)
        if not _crypt32.CertGetNameStringW(
                ctypes.byref(cert), 4, 0, None, out, need):
            return None
        return out.value or None
    finally:
        if hmsg.value:
            _crypt32.CryptMsgClose(hmsg)
        if pctx.value:
            _crypt32.CertFreeCertificateContext(pctx)
        if hstore.value:
            _crypt32.CertCloseStore(hstore, 0)


def _version_info(path):
    """PE 版本资源：VS_FIXEDFILEINFO + StringFileInfo 关键键；没有返回 {}。"""
    size = _version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return {}
    data = ctypes.create_string_buffer(size)
    if not _version.GetFileVersionInfoW(str(path), 0, size, data):
        return {}
    out = {}
    p = ctypes.c_void_p()
    ulen = ctypes.c_ulong()
    if _version.VerQueryValueW(data, "\\", ctypes.byref(p), ctypes.byref(ulen)) \
            and ulen.value >= ctypes.sizeof(_VS_FIXEDFILEINFO):
        ffi = ctypes.cast(p, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
        out["FileVersion_numeric"] = (
            f"{ffi.dwFileVersionMS >> 16}.{ffi.dwFileVersionMS & 0xFFFF}"
            f".{ffi.dwFileVersionLS >> 16}.{ffi.dwFileVersionLS & 0xFFFF}")
    if not _version.VerQueryValueW(
            data, "\\VarFileInfo\\Translation", ctypes.byref(p), ctypes.byref(ulen)):
        return out
    count = ulen.value // 4
    if count <= 0:
        return out
    langs = ctypes.cast(p, ctypes.POINTER(ctypes.c_ulong * count)).contents
    keys = ("CompanyName", "ProductName", "FileVersion", "OriginalFilename")
    for lang in langs:
        lang_id = lang & 0xFFFF
        codepage = (lang >> 16) & 0xFFFF
        for key in keys:
            if key in out:
                continue
            sub = f"\\StringFileInfo\\{lang_id:04x}{codepage:04x}\\{key}"
            if _version.VerQueryValueW(data, sub, ctypes.byref(p), ctypes.byref(ulen)) \
                    and ulen.value:
                out[key] = ctypes.wstring_at(p, ulen.value - 1)
    return out


def _motw_zone(path):
    """Mark of the Web：Zone.Identifier ADS 里的 ZoneId；无标记返回 None。"""
    try:
        with open(f"{path}:Zone.Identifier", encoding="utf-8", errors="replace") as ads:
            m = re.search(r"ZoneId\s*=\s*(\d+)", ads.read())
            return int(m.group(1)) if m else None
    except (OSError, ValueError):
        return None


def _parse_zone_id(text):
    """纯函数：从 Zone.Identifier 文本解析 ZoneId。"""
    m = re.search(r"ZoneId\s*=\s*(\d+)", text or "")
    return int(m.group(1)) if m else None


def _norm_org(name):
    """组织名规范化：小写 + 去空白 + 去常见公司后缀，用于签名者↔厂商比对。"""
    s = re.sub(r"\s+", "", (name or "").lower())
    for suffix in ("corporation", "inc.", "inc", "llc", "ltd.", "ltd", "co.,ltd", "corp",
                   "limited", "company", "有限公司", "股份", "科技", "集团", "公司"):
        s = s.replace(suffix, "")
    return s


def _orgs_match(signer, company):
    a, b = _norm_org(signer), _norm_org(company)
    if not a or not b:
        return True  # 缺一边不算矛盾
    return a == b or a in b or b in a


def _name_flags(filename):
    """纯函数：文件名启发式，返回 [{"flag", "detail"}]。"""
    flags = []
    if _SUSPECT_RE.search(filename):
        flags.append({"flag": "suspect-keyword",
                      "detail": "文件名含破解/激活/keygen 类关键词"})
    if _DOUBLE_EXT_RE.search(filename):
        flags.append({"flag": "double-extension",
                      "detail": "双扩展名伪装（文档图标 + .exe）"})
    if _COPY_SUFFIX_RE.search(filename):
        flags.append({"flag": "download-copy",
                      "detail": "浏览器重复下载副本命名（弱信号）"})
    return flags


def _signature_severity(signature, version_meta, indicators):
    """签名层证据 → severity（0/1/2），把指标追加进 indicators。"""
    company = (version_meta.get("CompanyName") or "").strip()
    if signature == "trusted":
        signer = version_meta.get("_signer") or ""
        if not _orgs_match(signer, company):
            indicators.append({
                "flag": "signer-vendor-mismatch", "severity": 1,
                "detail": f"签名者「{signer}」与自称厂商「{company}」不一致"})
            return 1
        return 0
    severity = 2 if signature in ("modified", "revoked") else 1
    indicators.append({
        "flag": f"signature-{signature}", "severity": severity,
        "detail": f"数字签名层：{signature}"})
    if signature == "unsigned" and _norm_org(company) in _KNOWN_VENDORS:
        indicators.append({
            "flag": "impersonated-vendor", "severity": 2,
            "detail": f"自称「{company}」（知名厂商）却没有任何有效签名"})
        return 2
    return severity


def _verdict(signature, version_meta, name_flags, motw_zone, has_sig_layer):
    """纯函数：证据 → (verdict, indicators)。severity：0=clean/unknown 1=suspicious 2=likely-fake。"""
    indicators = []
    sev = 0
    has_meta = bool(version_meta.get("CompanyName") or version_meta.get("ProductName"))

    # 签名层（Windows 才有）
    if has_sig_layer:
        sev = max(sev, _signature_severity(signature, version_meta, indicators))

    # 文件名层
    for flag in name_flags:
        indicators.append(flag)
        sev = max(sev, 2 if flag["flag"] in ("suspect-keyword", "double-extension") else 1)

    # 来源层
    if motw_zone == 3 and signature in ("unsigned", None, ""):
        indicators.append({
            "flag": "from-internet-unsigned", "severity": 1,
            "detail": "Mark of the Web=3（来自网络）且无可信签名"})
        sev = max(sev, 1)

    if not has_sig_layer and not has_meta and not indicators:
        return V_UNKNOWN, indicators
    if sev >= 2:
        return V_FAKE, indicators
    if sev == 1:
        return V_SUSPICIOUS, indicators
    if has_sig_layer and signature == "trusted":
        return V_TRUSTED, indicators
    return V_UNKNOWN, indicators


def _collect_files(path, max_files):
    """文件 → [自身]；目录 → 递归收集 .exe/.msi（上限 max_files）。"""
    if os.path.isfile(path):
        return [path]
    out = []
    for root, _dirs, names in os.walk(path):
        for name in sorted(names):
            if name.lower().endswith(_EXTS):
                out.append(os.path.join(root, name))
                if len(out) >= max_files:
                    return out
    return out


def _scan_one(path):
    """单个文件：三层证据 → {path, size, sha256, signature, version, motw, verdict, indicators}。"""
    size = os.path.getsize(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    entry = {
        "path": path, "size": size, "sha256": h.hexdigest(),
        "name_flags": _name_flags(os.path.basename(path)),
    }
    has_sig_layer = _IS_WIN
    signature = None
    version_meta = {}
    if _IS_WIN:
        status, detail = _win_verify_trust(path)
        signature = status
        entry["signature"] = {"status": status}
        if detail:
            entry["signature"]["detail"] = detail
        signer = _signer_display_name(path)
        version_meta = _version_info(path)
        if signer:
            version_meta["_signer"] = signer
            entry["signature"]["signer"] = signer
        entry["version"] = {k: v for k, v in version_meta.items() if not k.startswith("_")}
    else:
        entry["signature"] = {"status": "unavailable", "detail": "非 Windows，无 Authenticode 层"}
        entry["version"] = {}
    motw = _motw_zone(path)
    entry["motw_zone"] = motw
    verdict, indicators = _verdict(
        signature if _IS_WIN else None, version_meta, entry["name_flags"], motw, has_sig_layer)
    entry["verdict"] = verdict
    entry["indicators"] = indicators
    return entry


@tool("installer_scan",
      "伪造安装包扫描：数字签名（Authenticode：未签名/签后被改/吊销/过期/签名者≠自称厂商）"
      "+ PE 版本资源（CompanyName/ProductName/FileVersion/OriginalFilename）"
      "+ 文件名与来源启发式（破解/激活/keygen/双扩展名/MOTW）；"
      "三档判定 signed-trusted/suspicious/likely-fake（+metadata-only）+ 逐条证据；"
      "静态启发非保证——verdict 是线索不是结论，安装前人工确认",
      "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string",
                    "description": "安装包文件或包含 .exe/.msi 的目录"},
           "max_files": {"type": "integer",
                         "description": "目录模式下最多扫描文件数（默认 50）"}},
       "required": ["path"]})
def installer_scan(path, max_files=50):
    t0 = time.time()
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    max_files = max(1, min(int(max_files), 500))
    files = _collect_files(path, max_files)
    if not files:
        return {"ok": True, "root": path, "files": [], "summary": {},
                "note": "未找到 .exe/.msi 文件"}
    entries = [_scan_one(p) for p in files]
    summary = {}
    for ent in entries:
        summary[ent["verdict"]] = summary.get(ent["verdict"], 0) + 1
    return {
        "ok": True,
        "root": path,
        "files": entries,
        "summary": summary,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "note": ("静态启发非保证：verdict 是线索不是结论，安装前人工确认；"
                 "判定口径见模块 docstring"),
    }
