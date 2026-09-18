# -*- coding: utf-8 -*-
"""工具面向宿主的展示元数据（S143，兑现 EXTERNAL-ALIGNMENT A1）。

`annotations.title`：MCP 规范 2025-03-26（我们钉的版本）起的 Tool.annotations
字段——给宿主权限提示/工具列表用的人类可读名。此前一直漏发。

纪律（与 skills 文档分工）：
- 这里只放**短标题**（展示用）；详细说明继续住 skills/*.md（人读）与
  description（模型读）——三处各司其职，不重复长文；
- 键必须与注册表**双向一致**：漏一个工具或留一个陈旧名，由
  tests/test_s143_toolface.py 拦截（改工具名/退役工具必须同步本表）。
"""

TOOL_TITLES = {
    # fs 域
    "fs_read": "读文件",
    "fs_write": "写文件",
    "fs_stat": "文件信息",
    "fs_list": "列目录",
    # scan 域
    "bug_scan": "静态缺陷扫描",
    "std_check": "工程标准检查",
    "ui_check": "UI 死按钮检查",
    "bug_locate": "报错定位",
    "project_scan": "项目三路扫描",
    "code_review": "多维代码评审",
    "project_health": "项目健康度",
    "ast_scan": "结构化扫描",
    "file_scan": "文件签名与熵扫描",
    "near_dupes": "近似重复聚类",
    "secrets_hunt": "密钥泄漏扫描",
    "vuln_knowledge": "漏洞知识库",
    "ast_grep": "结构搜索",
    # ide 域
    "ide_outline": "文件结构大纲",
    "ide_read_symbol": "读符号实现",
    "locate_edit": "改动点定位",
    "code_context": "光标上下文",
    "ide_edit_multi": "多行批量修改",
    "ide_rename": "重命名影响面",
    "ide_batch_edit": "跨文件批量替换",
    "ide_build": "编译与静态检查",
    "ide_debug": "调试捕获",
    "ide_break": "断点调试",
    "ide_diagnostics": "统一诊断",
    "ide_lsp": "LSP 语义查询",
    "ide_impact": "改动影响面",
    "ide_test": "测试执行",
    "ide_multi_check": "多项目联动体检",
    "ide_doctor": "一键项目体检",
    "ide_vscode": "VS Code 打开",
    "ide_auto_report": "开发目录快照",
    "ide_health_trend": "健康度趋势",
    "ide_dead_code": "死代码扫描",
    "ide_callgraph": "调用图分析",
    "ide_risk_rank": "风险榜",
    "scip_refs": "SCIP 索引查询",
    # guard 域
    "capability_manifest": "能力边界清单",
    "hallucination_guard": "声明核查",
    "breaker_status": "熔断状态",
    "breaker_reset": "复位熔断",
    # learn 域
    "lesson": "教训记忆",
    # ops 域
    "backup": "备份与回溯",
    "scan_log": "扫描日志",
    "usage_stats": "使用统计",
    "session_burn": "会话烧量",
    "profile_status": "渐进披露状态",
    "profile_enable": "开启工具域",
    "lesson_stats": "教训库统计",
    # search 域
    "code_search": "词面检索",
    "code_semantic": "语义检索",
    "repo_map": "仓库符号地图",
    # game 域
    "game_check": "游戏规则检查",
    "blender_verify": "Blender 实地验证",
    # meta 域
    "local_run": "执行命令",
    "process": "进程管理",
    "gpu_status": "GPU 状态",
    # engine 域
    "engine_status": "引擎接入状态",
    "engine_query": "引擎语义查询",
    # attack 域
    "input_fuzz": "输入模糊测试",
    "path_probe": "路径逃逸探测",
    "big_input": "大输入边界",
    "auth_gate_sweep": "授权门自审",
    "rust_taint_scan": "污点数据流扫描",
    "attack_cruise": "攻击面巡航",
    # appaudit 域
    "app_audit": "应用只读审计",
    "app_clone": "克隆应用到沙箱",
    "app_clean": "清理审计沙箱",
    # sys 域（S148）
    "sys_topology": "CPU 拓扑（P/E 核）",
    "sys_threads": "线程调度视图",
    "sys_steer": "线程调度引导",
    "sys_devices": "显示适配器清单",
    "sys_procs": "进程清单（找引导目标）",
    "sys_privilege": "开启调试特权",
    # metrics 域
    "code_coverage": "行覆盖率测量",
    "dep_graph": "依赖关系图",
    "module_stability": "模块稳定性",
}


def title_for(name):
    """工具标题；未知名回退工具名本身（双向覆盖由测试拦截，回退只为防崩）。"""
    return TOOL_TITLES.get(name, name)


# —— 间接注入立场（S144，兑现 EXTERNAL-ALIGNMENT B3）——
# 判据：**结果里会带文件内容/文件派生的文本**的工具（读文件、代码片段、诊断文本）。
# 这些输出可能携带敌意指令（OWASP MCP 间接注入）直入宿主上下文——协议层会给
# 其回包加"非指令·数据"前缀（server.tool_reply），skills/workflow.md 有宿主纪律。
# 集中一处声明 = 可审计的一份清单；测试锁"声明名必须都在册"与代表性成员。
UNTRUSTED_OUTPUT_TOOLS = frozenset({
    "fs_read",          # 文件正文
    "code_context",     # 光标上下文 = 文件片段
    "ide_read_symbol",  # 符号定义体 = 文件片段
    "locate_edit",      # 命中行 + 片段
    "ast_grep",         # 结构搜索命中片段
    "code_search",      # BM25 命中片段
    "bug_scan",         # 缺陷行 + 代码片段
    "code_review",      # 评审摘录
    "project_scan",     # 三路扫描汇总（含片段）
    "std_check",        # 标准检查行摘录
    "ui_check",         # UI 检查行摘录
    "secrets_hunt",     # 命中上下文（掩码后仍含周边文本）
    "near_dupes",       # 样本片段
    "ide_diagnostics",  # linter/clippy 诊断文本（含代码/消息）
})


def is_untrusted(name):
    return name in UNTRUSTED_OUTPUT_TOOLS
