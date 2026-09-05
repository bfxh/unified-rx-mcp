# -*- coding: utf-8 -*-
"""tools/bevy.py —— Bevy 专项规则（用户：引擎重点优化 Bevy）

S82 起 ui_check 已 Rust 原生化（BEVY_UI_PATTERNS / find_dead_buttons 移植进
rust/src/scan.rs，Python 侧删除）；本模块只剩 bevy_rules()，供 bug_scan 使用。
"""
import re


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
