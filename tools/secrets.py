"""tools/secrets.py —— 凭据/密钥泄漏扫描（S123；S134 原生化薄壳）。

动机：仓库是泄漏重灾区——真 key 提交过一次就永远在 git 历史里。本工具对
目录做**模式匹配 + 高熵启发**两层扫描，输出只给**掩码**（前 4 后 2 + 长度），
完整值必须人工打开文件复核——扫描结果本身不能变成二次泄漏源。

两层口径（唯一实现在 rust/src/secrets.rs，`rx-scan secrets` 子命令）：
- 模式层（高置信）：AWS AKIA / GitHub gh*_ / Slack xox / Google AIza /
  Stripe sk_live / PEM 私钥头 / JWT 形状 / 通用 secret 赋值（password=、
  api_key: 等，值过滤占位符 changeme/${}/<your >/example）；
- 熵层（嫌疑）：长度 ≥20 的 token，Shannon 熵 ≥ min_entropy（默认 4.5
  bits/char）且至少 3 类字符——只报 suspect，不冒充确认。

S134 原生化（HARDENING §六 候选一，纪律=先测基线再做）：
- Python 基线 1.94s（本仓 745 文件）；Rust 首版 16.4s（每字符位置建字符串 +
  O(k²) 熵计数的两处热点）、重构后 **0.55s（3.5×）**；
- **对照实验**：同一夹具 Python 版与 Rust 版输出六字段 + hits 列表逐字节一致
  （14 命中/8 规则；语义要点=Unicode 词边界、行内规则源序、1-based 行号、
  Python splitlines 全字符集、占位符过滤、锁文件熵层跳过）；
- 本文件收敛为薄壳：沙盒解析 + 子进程转调 + root/note/elapsed 补齐；
  exe 缺失报清晰错误不静默降级（同 scan 域纪律）。

诚实边界：
- 这是**静态启发，不是保证**——自定义格式密钥（不匹配任何模式、熵不够）
  漏报；占位符过滤可能放过伪装成占位符的真 key；误报也会有（随机 UUID、
  测试夹具里的假 token）——所有 hit 都要人工复核；
- 非 UTF-8 文件的 snippet 文本在替换字符边界上可能与旧 Python 版有差异
  （命中/掩码不受影响——模式与 token 均为 ASCII 类）；
- 扫描结果不外发、不落盘原始值——掩码是产品行为不是可选项。
"""
import os
import time

from registry import tool
from tools.fs import _resolve as _fs_resolve
from tools.scan import _rx_scan_call  # 与 scan 域同一 exe 桥（单一实现）

DEFAULT_INCLUDE = ("py,rs,js,ts,jsx,tsx,json,yaml,yml,toml,md,go,java,c,h,cpp,"
                   "cs,php,rb,sh,ps1,bat,sql,env,cfg,ini,txt,xml,html,swift,kt")

_NOTE = ("掩码展示；完整值人工打开文件复核。静态启发：漏报可能"
         "（自定义格式）、误报可能（随机 UUID/测试夹具）。若真有"
         "泄漏：先吊销轮换 key，再清 git 历史（BFG/filter-repo），"
         "改密不能撤回已泄漏的凭据")


@tool("secrets_hunt",
      "凭据泄漏扫描：AWS/GitHub/Slack/Google/Stripe/PEM/JWT 模式 + 高熵 token（Shannon ≥4.5）；输出掩码（前4后2+长度），结果不外发；静态启发非保证，hit 一律人工确认", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "扫描根目录（默认当前目录）"},
           "include": {"type": "string",
                       "description": "逗号分隔的扩展名白名单（默认常见源码/配置 30+ 种）"},
           "max_file_kb": {"type": "integer", "description": "单文件上限 KB（默认 512）"},
           "min_entropy": {"type": "number",
                           "description": "熵层阈值 bits/char（默认 4.5）"},
           "max_files": {"type": "integer", "description": "最多扫描文件数（默认 3000）"},
           "max_results": {"type": "integer", "description": "返回 hit 上限（默认 200）"}},
       "required": []})
def secrets_hunt(path=None, include=None, max_file_kb=512, min_entropy=4.5,
                 max_files=3000, max_results=200):
    t0 = time.time()
    try:
        root = _fs_resolve(path or os.getcwd())   # S134：读路径过沙盒（S88 纪律补全）
    except ValueError as e:
        return {"error": str(e)}
    out = _rx_scan_call(["secrets", root, str(int(max_files)), str(int(max_file_kb)),
                         str(float(min_entropy)), str(int(max_results)),
                         include or ""])
    out["root"] = root
    out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    out["note"] = _NOTE
    return out
