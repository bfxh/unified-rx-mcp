#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_api —— 引擎 API 语义词典（2026-08-14，阶段3 引擎语义层）。

Bevy 0.18 优先 + Godot 4 基础（skill game-dev 覆盖对齐）。
防幻觉红线：未收录 → 明确返回"未收录"（绝不臆造签名/语义）。
词典数据驱动（可增补——配置化理念）。
"""
import json
import os

# ── Bevy 0.18 常用 API（组件/系统/派生宏/资源）──
BEVY_API = {
    # 组件
    "Transform": ("组件", "位置/旋转/缩放（translation/rotation/scale）——实体空间状态"),
    "Sprite": ("组件", "2D 精灵（texture/color/custom_size）"),
    "Text": ("组件", "UI 文本（Bevy UI——含字体渲染；CJK 需字体兜底）"),
    "Button": ("组件", "UI 按钮（配合 Interaction 组件检测点击）"),
    "Node": ("组件", "UI 节点（布局容器——style 驱动）"),
    "Camera2d": ("组件", "2D 相机（渲染视口必需——UI 无相机不可见）"),
    "Camera3d": ("组件", "3D 相机"),
    "RigidBody": ("组件", "Avian 刚体（物理模拟——碰撞/重力）"),
    "Collider": ("组件", "Avian 碰撞体（cuboid/ball/compound）"),
    "PositionType": ("枚举", "UI 定位（Relative/Absolute——覆盖层需 Absolute+全屏）"),
    "FocusPolicy": ("枚举", "UI 焦点策略（Pass=不拦截点击——全屏覆盖层必须 Pass）"),
    "Visibility": ("组件", "可见性（Hidden/Visible——模式隔离用）"),
    # 系统参数
    "Query": ("系统参数", "实体组件查询（&Query<&Transform>——只读；Mut 可变）"),
    "Res": ("系统参数", "资源读取（&Res<T>）"),
    "ResMut": ("系统参数", "资源可变读取"),
    "Commands": ("系统参数", "实体命令（spawn/despawn/insert——延迟执行）"),
    "EventWriter": ("系统参数", "事件发送（EventWriter<T>）"),
    "EventReader": ("系统参数", "事件读取"),
    "AssetServer": ("资源", "资产加载（load 异步——每帧循环内 load 会卡顿）"),
    "Time": ("资源", "时间（delta——帧率无关逻辑必需）"),
    # 系统/宏
    "Update": ("Schedule", "每帧更新调度（Update 系统集——帧内逻辑入口）"),
    "FixedUpdate": ("Schedule", "固定步长更新（物理）"),
    "Startup": ("Schedule", "启动一次（初始化）"),
    "derive(Component)": ("派生宏", "自定义组件派生（#[derive(Component)]）"),
    "derive(Resource)": ("派生宏", "自定义资源派生"),
    "derive(Bundle)": ("派生宏", "组件集打包"),
    "spawn": ("命令", "实体创建（Commands::spawn）"),
    "despawn": ("命令", "实体销毁"),
    "insert": ("命令", "实体加组件"),
    "query": ("World 方法", "World::query（测试/系统外查询）"),
    "just_pressed": ("输入", "按键刚按下（边缘触发——连打需冷却）"),
    "is_pressed": ("输入", "按键持续按住"),
    "send": ("事件", "EventWriter::send（事件发布）"),
    "bevy_remote": ("crate", "Bevy Remote Protocol（BRP localhost:15702——远程调试实体）"),
    "RemotePlugin": ("插件", "BRP 远程调试插件（bevy_remote crate——bevy-mcp 依赖）"),
}

# ── Godot 4 常用 API（节点/信号/回调）──
GODOT_API = {
    "_ready": ("回调", "节点就绪一次（初始化——非每帧）"),
    "_process": ("回调", "每帧处理（delta 参数——帧率无关必需）"),
    "_physics_process": ("回调", "固定步长处理（物理——推荐物理逻辑）"),
    "_input": ("回调", "输入事件回调（InputEvent——连打需冷却）"),
    "_unhandled_input": ("回调", "未处理输入回调"),
    "Node2D": ("节点", "2D 节点基类（位置/旋转/缩放）"),
    "Sprite2D": ("节点", "2D 精灵（texture 属性）"),
    "CharacterBody2D": ("节点", "角色体（move_and_slide——物理移动）"),
    "RigidBody2D": ("节点", "刚体（物理模拟）"),
    "CollisionShape2D": ("节点", "碰撞形状（父级物理体必需）"),
    "Label": ("节点", "UI 文本（theme 字体——CJK 需字体资源）"),
    "Button": ("节点", "UI 按钮（pressed 信号——死按钮检查）"),
    "Control": ("节点", "UI 基类（visible 属性——模式隔离）"),
    "Timer": ("节点", "计时器（timeout 信号——冷却实现）"),
    "AudioStreamPlayer": ("节点", "音频播放（程序化音频：AudioStreamGenerator）"),
    "FileAccess": ("类", "文件 IO（open/read/write——每帧内 IO 卡顿）"),
    "load": ("函数", "资源加载（res:// 路径——每帧内 load 卡顿）"),
    "preload": ("函数", "编译期资源预加载（推荐）"),
    "get_node": ("方法", "节点获取（不存在返回 null——建议 get_node_or_null 判空）"),
    "queue_free": ("方法", "安全释放（帧末——信号回调中安全）"),
    "free": ("方法", "立即释放（信号回调中崩溃风险）"),
    "connect": ("方法", "信号连接（connect(\"pressed\", ...)——旧式）"),
    "pressed": ("信号", "按钮按下信号（Button）"),
    "timeout": ("信号", "计时结束信号（Timer）"),
    "is_action_pressed": ("输入", "动作按下（Input 单例——输入映射）"),
    "move_and_slide": ("方法", "角色移动（CharacterBody2D——含碰撞）"),
    "headless": ("运行模式", "无头运行（--headless --path 项目——测试/CI 可复现）"),
    "XDG": ("运行环境", "项目本地 XDG 三件套（DATA/CONFIG/CACHE_HOME——CI 可复现）"),
}

_DB = {"bevy": BEVY_API, "godot": GODOT_API}


def query_api(engine: str, symbol: str) -> dict:
    """查 API 语义（防幻觉：未收录 → 诚实拒绝，不臆造）。"""
    db = _DB.get(engine.lower())
    if db is None:
        return {"ok": False, "error": f"未知引擎: {engine}（支持 bevy/godot）",
                "available": sorted(_DB.keys())}
    if symbol in db:
        kind, desc = db[symbol]
        return {"ok": True, "engine": engine, "symbol": symbol,
                "kind": kind, "description": desc}
    # 前缀模糊（防手误：bevy_remote → bevy_remote 精确已试；做前缀/包含提示）
    fuzzy = [k for k in db if symbol.lower() in k.lower()][:5]
    return {"ok": False, "error": f"未收录: {engine}.{symbol}（防幻觉——不臆造签名）",
            "fuzzy": fuzzy,
            "hint": "词典未收录的 API 请查引擎官方文档；或补进词典（数据驱动）"}


def list_engine(engine: str) -> dict:
    db = _DB.get(engine.lower())
    if db is None:
        return {"ok": False, "error": f"未知引擎: {engine}"}
    return {"ok": True, "engine": engine, "count": len(db),
            "symbols": sorted(db.keys())}


def _load_dicts_dir(path: str) -> None:
    """可增补：game_api.d/*.json 外部词典（引擎名=文件名）。"""
    if not os.path.isdir(path):
        return
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(path, fn), encoding="utf-8") as f:
                data = json.load(f)
            engine = fn[:-5]
            if isinstance(data, dict):
                _DB.setdefault(engine, {})
                for k, v in data.items():
                    if isinstance(v, list) and len(v) == 2:
                        _DB[engine][k] = tuple(v)
                    elif isinstance(v, str):
                        _DB[engine][k] = ("外部", v)
        except (OSError, ValueError):
            continue
