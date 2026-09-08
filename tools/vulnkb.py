# -*- coding: utf-8 -*-
"""tools/vulnkb.py —— 漏洞知识库（S110，ADVANCES P2 第 9 项，Vul-RAG 式）。

给扫描器命中附"这类问题的成因/修法/先例"——Vul-RAG（arXiv:2406.11147）证明
知识级检索能把人工复核准确率从 60% 提到 77%。本仓实现取**知识库 + 关键词检索**
（零依赖、不引模型），知识来源三处：本仓规则语义、本仓实战先例（$Deleted 竞态/
四轮弹跳床等）、通用漏洞常识。

诚实口径：
- `rules` 非空的条目 = 本仓扫描器**能报**的模式；`rules: []` 的条目 = 本仓规则
  **未覆盖**的类别（如路径穿越/竞态）——写清"未覆盖"本身也是知识（见
  VULN-HUNTING 附录 B 覆盖矩阵），不假装能扫。
- 检索是关键词/规则号匹配，非语义嵌入；命中少时不硬凑（返回空 + 提示）。
"""
import re

from registry import tool

# 每条：id / rules（可空=本仓未覆盖）/ langs / title / cause / fix / precedent / tags
KB = [
    # ---------- 注入类 ----------
    {"id": "kb-eval-exec", "rules": ["eval_exec", "eval_call", "new_function"],
     "langs": ["python", "javascript"], "title": "动态执行外部可控代码",
     "cause": "eval/exec/new Function 把字符串当代码执行；输入若来自用户/网络/文件即任意代码执行",
     "fix": "改显式映射/解析（ast.literal_eval、JSON、白名单分发）；确需动态执行则限定能力并隔离进程",
     "precedent": "本仓 appaudit 对 Electron 快照的 eval_call/new_function 规则即此模式",
     "tags": ["注入", "CWE-95", "RCE"]},
    {"id": "kb-shell-inject", "rules": ["child_process"],
     "langs": ["javascript", "python"], "title": "命令拼接注入",
     "cause": "把外部输入拼进 shell 命令行；shell=True/exec 元字符使参数越界成命令",
     "fix": "argv 列表直调、shell=False；无法避免时用白名单校验参数（本仓 local_run 的做法）",
     "precedent": "本仓所有 exe 转调一律 argv 列表 + 无 shell（fs/scan/search/ide 壳同纪律）",
     "tags": ["注入", "CWE-78"]},
    {"id": "kb-sql-inject", "rules": [],
     "langs": ["python", "javascript", "java"], "title": "SQL 拼接注入（本仓未覆盖）",
     "cause": "SQL 语句用字符串拼接外部输入；静态规则只能发现明显拼接，参数化与否需数据流",
     "fix": "一律参数化查询/ORM 绑定；禁止 f-string 拼 SQL",
     "precedent": "code_review 的 security 透镜只报'疑似拼接'模式，非污点分析（VULN-HUNTING 附录 B）",
     "tags": ["注入", "CWE-89", "未覆盖"]},
    {"id": "kb-path-traversal", "rules": [],
     "langs": ["any"], "title": "路径穿越（本仓未覆盖，运行时防线）",
     "cause": "外部输入参与路径拼接（../ 或绝对路径）→ 读写预期之外的文件；静态判定需数据流",
     "fix": "规范化后做包含性校验（realpath + startswith 根目录）；本仓以**运行时沙盒钳制**为防线",
     "precedent": "S95/S97 两轮：ast_scan 与 hallucination_guard 的读路径漏过沙盒 → 已补门（_fs_resolve）",
     "tags": ["路径", "CWE-22", "未覆盖"]},
    # ---------- Python 逻辑面 ----------
    {"id": "kb-bare-except", "rules": ["bare_except"],
     "langs": ["python"], "title": "裸 except 吞掉一切异常",
     "cause": "except: 捕获包括 KeyboardInterrupt/SystemExit；错误被吞后表现为'静默失败'",
     "fix": "捕获具体异常类型；确需兜底时 except Exception 并记录（log_msg/notify）",
     "precedent": "本仓 S72 修复：registry 错误上浮 + error_detail 堆栈尾，就是防'吞异常'",
     "tags": ["逻辑", "CWE-391"]},
    {"id": "kb-undefined-name", "rules": ["undefined_name", "redefined_import"],
     "langs": ["python"], "title": "未定义名/遮蔽导入",
     "cause": "拼写错误、作用域误用、或导入名与内建/局部变量同名遮蔽",
     "fix": "跑一次静态检查或 pytest 收集；改名避免遮蔽内建",
     "precedent": "本仓 bug_scan 的 defined 收集按 3.14 AST 口径（S83 对照实验）",
     "tags": ["逻辑"]},
    {"id": "kb-placeholder", "rules": ["placeholder", "magic_number"],
     "langs": ["any"], "title": "占位/假数据与魔法数字",
     "cause": "TODO 占位、测试假数据残留、无解释的数字常量——交付前漏清",
     "fix": "占位词清零或转 TODO 工单；魔法数字提取为具名常量",
     "precedent": "std_check 的 12 种占位词 + 6 语言魔法数门（S82 原生化）",
     "tags": ["质量"]},
    {"id": "kb-equal-float", "rules": ["equal_float", "assert_always_true"],
     "langs": ["any"], "title": "浮点相等比较 / 恒真断言",
     "cause": "== 比较浮点受精度影响；assert True 永远通过等于没断言",
     "fix": "用容差比较（math.isclose）；删恒真断言或补真实条件",
     "precedent": "generic 规则（S83）",
     "tags": ["逻辑", "测试"]},
    # ---------- Rust 面 ----------
    {"id": "kb-unwrap-expect", "rules": ["unwrap", "expect"],
     "langs": ["rust"], "title": "unwrap/expect 直接 panic",
     "cause": "None/Err 时 panic；库代码里等于把错误处理推给调用方炸栈",
     "fix": "用 ? / match / unwrap_or 兜底；确需 panic 处加注释说明不变量",
     "precedent": "clue 档全量上报是设计（skills/scan.md），测试目录命中降级",
     "tags": ["逻辑", "CWE-248"]},
    {"id": "kb-indexing", "rules": ["indexing", "as_cast"],
     "langs": ["rust"], "title": "索引越界与 as 截断",
     "cause": "v[i] 越界 panic；as 转换静默截断/丢精度（usize→u32 等）",
     "fix": "用 .get() 返回 Option；数值转换用 try_from 并处理失败",
     "precedent": "S27 修复：indexing 正则支持 `[x.f as usize]`（此前漏报）",
     "tags": ["逻辑", "CWE-190"]},
    {"id": "kb-panic-unreachable", "rules": ["panic", "unreachable", "todo_unimplemented"],
     "langs": ["rust"], "title": "panic!/unreachable!/todo! 到达即炸",
     "cause": "断言式崩溃或未实现占位进了生产路径",
     "fix": "改为 Result 返回；unreachable 处补防御分支并记录日志",
     "precedent": "本仓 definite 档规则（kind=clue/info 分级见 skills/scan.md）",
     "tags": ["逻辑"]},
    {"id": "kb-bevy-phys-force", "rules": ["bevy_phys_manual_support_force"],
     "langs": ["rust"], "title": "手写竖直支撑力无总力预算（四轮弹跳床）",
     "cause": "每个执行器各自封顶 ≠ 总和有界：四轮同压可叠到 3×车重持续弹起",
     "fix": "整车/整实体级总力预算，或改用引擎约束/阻尼",
     "precedent": "VoxelForge 2026-09-04 四轮弹跳床案（S74 转规则）",
     "tags": ["物理引擎", "游戏"]},
    {"id": "kb-bevy-static-velocity", "rules": ["bevy_phys_static_with_velocity"],
     "langs": ["rust"], "title": "静态刚体带速度",
     "cause": "Static 刚体设了 LinearVelocity——物理引擎不推进它，读速度的地方会误判",
     "fix": "静态体不要写速度；需要移动用 Kinematic",
     "precedent": "S74 规则三件套（误报守卫：::ZERO/matches! 三类放行）",
     "tags": ["物理引擎", "游戏"]},
    {"id": "kb-bevy-locked-axes", "rules": ["bevy_phys_locked_axes_bits"],
     "langs": ["rust"], "title": "锁定轴位运算写错",
     "cause": "locked_axes 是位标志，拼错位或覆盖写会让'锁定'失效",
     "fix": "用常量按位或；改动后加断言/单测覆盖轴组合",
     "precedent": "S74 规则档案（VoxelForge sync.rs:371/397/591 命中）",
     "tags": ["物理引擎", "游戏"]},
    {"id": "kb-bevy-api", "rules": ["bevy_old_system", "bevy_old_startup", "bevy_query_single",
                                   "bevy_event_iter", "bevy_text_old"],
     "langs": ["rust"], "title": "Bevy 旧版 API 残留",
     "cause": "跨版本升级后旧写法仍能编译（或只在新版本行为变化），语义悄悄漂移",
     "fix": "按当前版本迁移表改；升级后跑一次扫描确认清零",
     "precedent": "本仓 bevy 8 条规则（S74 起）",
     "tags": ["物理引擎", "游戏", "升级"]},
    # ---------- 凭据面 ----------
    {"id": "kb-hardcoded-secret", "rules": ["private_key_block", "api_key_sk", "github_pat",
                                            "aws_access_key", "secret_by_key"],
     "langs": ["any"], "title": "硬编码凭据",
     "cause": "密钥/令牌写进源码或配置，随仓库扩散且难以轮换",
     "fix": "移到环境变量/密钥管理；已泄露的立即轮换（代码删除不够）",
     "precedent": "本仓宿主 config.json 的 API key 属同类风险（已提醒，需宿主侧轮换）",
     "tags": ["凭据", "CWE-798"]},
    # ---------- 并发/资源（本仓未覆盖，如实标注） ----------
    {"id": "kb-race", "rules": [], "langs": ["any"], "title": "并发竞态（本仓未覆盖，需运行时）",
     "cause": "共享状态在无同步下被并发读写；静态匹配器看不见时序",
     "fix": "锁/原子/不可变数据；用压力电池（多线程同靶）复现并回归",
     "precedent": "S95 高压电池实锤：rename-replace 与 canonicalize 竞态 → $Deleted 幽灵路径（已修）",
     "tags": ["并发", "未覆盖"]},
    {"id": "kb-resource-leak", "rules": [], "langs": ["any"], "title": "资源泄漏（本仓未覆盖）",
     "cause": "句柄/文件/连接未在异常路径释放；需数据流与控制流分析",
     "fix": "with/RAII/defer；长驻进程加句柄计数探针",
     "precedent": "本仓 bench 的 psapi 内存 soak 是同类'运行时探针'思路（EVAL §6）",
     "tags": ["资源", "未覆盖"]},
    {"id": "kb-toctou", "rules": [], "langs": ["any"], "title": "TOCTOU 检查-使用竞态（本仓未覆盖）",
     "cause": "先检查后使用之间状态可变（文件/权限/存在性）",
     "fix": "用原子操作或打开后校验句柄；避免基于路径的两段式判断",
     "precedent": "本仓沙盒 resolve 采用'规范化+包含性校验'而非两段式判断",
     "tags": ["并发", "路径", "未覆盖"]},
]

_BY_RULE = {}
for _e in KB:
    for _r in _e["rules"]:
        _BY_RULE.setdefault(_r, []).append(_e)


def lookup(rule):
    """规则号 → 条目列表（精确匹配；无则空）。"""
    return list(_BY_RULE.get(rule or "", []))


def _tokens(text):
    return {t for t in re.findall(r"[a-z0-9_]{2,}", (text or "").lower())
            if t not in ("the", "and", "for", "with", "this", "that")}


def search(query, k=3):
    """关键词检索：规则号/标题/成因/修法/标签的词元重叠打分（零依赖、非嵌入）。"""
    q = _tokens(query)
    if not q:
        return []
    scored = []
    for e in KB:
        text = " ".join([e["title"], e["cause"], e["fix"], e["precedent"],
                         " ".join(e["tags"]), " ".join(e["rules"])])
        t = _tokens(text)
        if not t:
            continue
        score = len(q & t) / len(q)
        if score > 0:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:max(1, int(k))]]


def _brief(e):
    return {"id": e["id"], "title": e["title"], "fix": e["fix"],
            "covered": bool(e["rules"])}


def annotate_issues(result):
    """给 bug_scan 结果的每条 issue 附 `kb`（命中才加，一条最多一个）。"""
    if not isinstance(result, dict):
        return result
    for it in result.get("issues") or []:
        ents = lookup(it.get("rule"))
        if ents:
            it["kb"] = _brief(ents[0])
    return result


@tool("vuln_knowledge", "漏洞知识库（S110）：按规则号或关键词查'成因/修法/先例'——"
      "扫描器命中后查怎么修；未覆盖类别如实标注（不假装能扫）", "scan",
      {"type": "object",
       "properties": {
           "query": {"type": "string", "description": "关键词（中文/英文均可）"},
           "rule": {"type": "string", "description": "规则号精确查询（如 bare_except）"},
           "k": {"type": "integer", "description": "返回条数（默认 3）"},
       },
       "required": []})
def vuln_knowledge(query=None, rule=None, k=3):
    if rule:
        ents = lookup(rule)
        return {"rule": rule, "total": len(ents), "entries": ents}
    if not query:
        return {"error": "query 或 rule 至少给一个"}
    ents = search(query, k)
    return {"query": query, "total": len(ents), "entries": ents,
            "note": "关键词检索（非语义嵌入）；未覆盖类别 entries[].rules 为空数组"}
