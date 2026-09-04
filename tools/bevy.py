# -*- coding: utf-8 -*-
"""tools/bevy.py —— Bevy 专项规则（用户：引擎重点优化 Bevy）

提供 Bevy 常见错误模式规则，供 scan 域引用：
- Bevy 0.18/0.19 UI API 变化（with_children 空/Button 死/TextBundle 旧式）
- 组件缺失（spawn 后无交互处理）
- 资源/系统常见坑
"""
import re

# Bevy UI 死按钮/空容器模式（Bevy 0.18/0.19）
# S6：死按钮检测升级为结构化函数 find_dead_buttons（Marker-Query 跨 system 验证），
# 不再放正则误报——BEVY_UI_PATTERNS 只留无歧义模式。
BEVY_UI_PATTERNS = [
    # 空 with_children（无子节点，UI 布局无效）
    (r"\.with_children\(\s*\)", "空 with_children()——无子节点（UI 无效）"),
    # 旧式 TextBundle（Bevy 0.15+ 用 Text::new）
    (r"TextBundle\s*\{", "旧式 TextBundle——Bevy 0.15+ 推荐 Text::new（API 迁移）"),
    # 旧式 TextStyle 直接构建
    (r"TextStyle\s*\{", "TextStyle 手动构建——Bevy 0.15+ 推荐 TextFont/TextColor 组件"),
]

# Bevy 系统/资源常见错误
BEVY_CODE_PATTERNS = [
    (r"\.add_system\(", "add_system——Bevy 0.13+ 用 .add_systems（旧 API）"),
    (r"\.add_startup_system\(", "add_startup_system——Bevy 0.13+ 用 .add_systems(Startup, ...)"),
    (r"EventReader<[^>]+>\.iter\(", "EventReader.iter——Bevy 0.13+ 用 .read()（旧 API）"),
]

def find_dead_buttons(src):
    """Bevy UI 死按钮检测（S6 修正版）。

    正确语义：spawn((Button, Marker, ...)) 后必须存在 Query<...With<Marker>...Interaction>
    （可在任意 system）。此前只看 spawn 同处有没有后续处理，
    把"Marker 组件 + 跨 system 查询"这一 Bevy 标准模式全部误报（VoxelForge ui.rs 9/9 全误报）。

    返回误报修正后的 [(line, marker_name)]。
    """
    issues = []
    lines = src.split("\n")
    for i, line in enumerate(lines):
        # 只认 "Button," 独立元组成员行（spawn((Button,\n Marker,...)）或同行 (Button, Marker,
        if not re.search(r"\(?(Button,\s*(?:[A-Z][A-Za-z0-9_]*\s*[\{,])?)", line):
            continue
        is_lone = line.strip() == "Button," or line.strip().endswith("(Button,")
        marker = None
        if is_lone:
            scan = range(i + 1, min(i + 3, len(lines)))
        else:
            # 同行：Button 后面的 token 就是 marker
            m = re.search(r"Button,\s*([A-Z][A-Za-z0-9_]*)", line)
            scan = None
            if m and m.group(1) != "Node":
                marker = m.group(1)
                return_check = True
        if is_lone:
            for j in scan:
                mm = re.match(r"\s*([A-Z][A-Za-z0-9_]+)\s*[\{,]", lines[j])
                if mm and mm.group(1) != "Button":
                    marker = mm.group(1)
                    break
                if lines[j].strip().startswith((")", "//")):
                    break
        else:
            pass
        if not marker:
            continue
        # 全文找该 Marker 的交互查询（With<Marker> 或 &Marker 与 Interaction 同现）
        if re.search(r"With<" + marker + r">", src) or \
           re.search(r"&" + marker + r"[^\n]{0,80}Interaction|Interaction[^\n]{0,80}&" + marker, src):
            continue
        issues.append((i + 1, marker))
    return issues


def bevy_rules():
    """返回 (name, pattern, msg, severity) 列表，供 bug_scan/ui_check 使用。

    S4-D1：文本 API 迁移类全是线索（kind 由 scan.py 补），确定性崩溃才 high。
    物理规则（avian3d，09-05 增补）：全部来自 VoxelForge 实机案例（载具飞天/弹跳床），
    只收确定性高的模式当线索，宁缺毋滥。
    """
    return [
        ("bevy_old_system", r"\.add_system\(", "add_system 旧 API——用 .add_systems（迁移线索）", "info"),
        ("bevy_old_startup", r"\.add_startup_system\(", "add_startup_system 旧 API——用 .add_systems(Startup, ...)（迁移线索）", "info"),
        ("bevy_event_iter", r"EventReader<[^>]+>\.iter\(", "EventReader.iter 旧 API——用 .read()（迁移线索）", "info"),
        ("bevy_text_old", r"TextBundle\s*\{", "TextBundle 旧式——用 Text::new（迁移线索）", "info"),
        ("bevy_query_single", r"\.single\(\)", "query.single() 自 Bevy 0.16 起返回 Result——Err 静默失败是逻辑雷（用 let Ok = .. else return 兜）；.single().unwrap() 才会 panic（09-05 VoxelForge 11 处甄别：全部正确 else-return，零真险）（线索）", "low"),
        # ---- avian3d 物理（VoxelForge 09-04 载具飞天案沉淀）----
        ("bevy_phys_locked_axes_bits", r"LockedAxes::from_bits\(\s*0b",
         "LockedAxes 魔数位——位序易错（VoxelForge 0b000_101 曾误读为锁平移），用具名位常量 ROTATION_X/TRANSLATION_* 核对", "info"),
        ("bevy_phys_static_with_velocity",
         r"spawn\s*\(\s*\(?(?:[^);]|\([^)]*\)){0,160}?RigidBody::Static\s*,"
         r"[\s\S]{0,200}?(LinearVelocity(?!::ZERO)|ExternalForce|AngularVelocity(?!::ZERO))"
         r"|spawn\s*\(\s*\(?(?:[^);]|\([^)]*\)){0,160}?(LinearVelocity(?!::ZERO)|ExternalForce|AngularVelocity(?!::ZERO))\s*,"
         r"[\s\S]{0,200}?RigidBody::Static\s*,",
         "spawn 元组里 RigidBody::Static 携带速度/受力组件——Static 体不响应力与速度，写了不生效（::ZERO 冗余不报；VoxelForge 09-05 甄别：matches! 判断与测试 fixture 为误报源；S74 两个分支都锚 spawn 元组——前一条 spawn 的速度逗号 + 200 字符内另一条 Static spawn 不再跨语句误连）", "low"),
        ("bevy_phys_manual_support_force", r"apply_force_at_point\(\s*Vec3::Y\b",
         "手写竖直支撑/弹簧力（Vec3::Y × f）——多轮/多执行器各自封顶≠总和有界：四轮同压可叠到 3×车重持续弹起（VoxelForge 09-04 四轮弹跳床案），须有整车总力预算", "med"),
    ]
