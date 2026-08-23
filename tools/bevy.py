# -*- coding: utf-8 -*-
"""tools/bevy.py —— Bevy 专项规则（用户：引擎重点优化 Bevy）

提供 Bevy 常见错误模式规则，供 scan 域引用：
- Bevy 0.18/0.19 UI API 变化（with_children 空/Button 死/TextBundle 旧式）
- 组件缺失（spawn 后无交互处理）
- 资源/系统常见坑
"""
import re

# Bevy UI 死按钮/空容器模式（Bevy 0.18/0.19）
BEVY_UI_PATTERNS = [
    # 空 with_children（无子节点，UI 布局无效）
    (r"\.with_children\(\s*\)", "空 with_children()——无子节点（UI 无效）"),
    # Button 无交互处理（spawn 了 Button 但没后续 query 处理）
    (r"spawn\(\s*\(\s*[^)]*\bButton\b[^)]*\)", "Button 无后续交互处理（疑似死按钮）"),
    # 旧式 TextBundle（Bevy 0.15+ 用 Text::new）
    (r"TextBundle\s*\{", "旧式 TextBundle——Bevy 0.15+ 推荐 Text::new（API 迁移）"),
    # 旧式 TextStyle 直接构建
    (r"TextStyle\s*\{", "TextStyle 手动构建——Bevy 0.15+ 推荐 TextFont/TextColor 组件"),
    # 颜色硬编码（UI 质感问题）
    (r"Color::rgb\(\s*0\.[0-9]+\s*,\s*0\.[0-9]+\s*,\s*0\.[0-9]+\s*\)",
     "Color::rgb 硬编码颜色（建议设计 token 统一）"),
]

# Bevy 系统/资源常见错误
BEVY_CODE_PATTERNS = [
    (r"\.add_system\(", "add_system——Bevy 0.13+ 用 .add_systems（旧 API）"),
    (r"\.add_startup_system\(", "add_startup_system——Bevy 0.13+ 用 .add_systems(Startup, ...)"),
    (r"EventReader<[^>]+>\.iter\(", "EventReader.iter——Bevy 0.13+ 用 .read()（旧 API）"),
]


def bevy_rules():
    """返回 (name, pattern, msg, severity) 列表，供 bug_scan/ui_check 使用。"""
    return [
        ("bevy_old_system", r"\.add_system\(", "add_system 旧 API——Bevy 0.13+ 用 .add_systems", "medium"),
        ("bevy_old_startup", r"\.add_startup_system\(", "add_startup_system 旧 API——用 .add_systems(Startup, ...)", "medium"),
        ("bevy_event_iter", r"EventReader<[^>]+>\.iter\(", "EventReader.iter 旧 API——用 .read()", "medium"),
        ("bevy_text_old", r"TextBundle\s*\{", "TextBundle 旧式——用 Text::new", "medium"),
        ("bevy_query_single", r"\.single\(\)", "query.single() 多实体 panic——用 iter", "low"),
    ]
