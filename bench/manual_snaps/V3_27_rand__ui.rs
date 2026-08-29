//! VoxelForge-V3 UI 层：五区布局。
//!
//! 左上 = 建造状态面板（选中模块 / 放置状态）
//! 左下 = 左侧窄长条：顶部搜索框 → 分类区（窄，3 字）+ 物品栏（每排 2 个，上下滚动）
//! 右上 = 载具面板（质量 / 模块数 / 轮子数 / HP）
//! 底部中央 = 操作提示（低透明度，建造空闲时可见）
//! 右下 = toast 反馈（S/L / 错误，2.5s 淡出）
//!
//! 风格：深色半透明圆角面板 + 1px 低饱和描边；
//! 强调色语义与渲染层一致——青蓝=可吸附、白=散落、红=禁止。

use bevy::prelude::*;
use bevy::input::mouse::MouseWheel;
use bevy::ui::widget::Button;
use bevy::ui::FocusPolicy;
use bevy::text::EditableText;
use vxl_core::module::Category;

use crate::{cat_color, ActiveVehicle, AsmRes, CursorPos, HudState, Picked, PLACING_NEW, Selection, VehicleMenu};

// ---------- 风格常量 ----------

const PANEL_BG: Color = Color::srgba(0.05, 0.06, 0.08, 0.72);
const PANEL_BORDER: Color = Color::srgba(0.35, 0.38, 0.45, 0.6);
const SLOT_BG: Color = Color::srgba(0.09, 0.10, 0.13, 0.9);
const SLOT_ACTIVE_BORDER: Color = Color::srgb(0.35, 0.8, 1.0);
const SLOT_BORDER: Color = Color::srgba(0.25, 0.28, 0.34, 0.8);
const TEXT_MAIN: Color = Color::srgb(0.92, 0.93, 0.96);
const TEXT_DIM: Color = Color::srgb(0.6, 0.63, 0.7);
const TOAST_SECS: f32 = 2.5;

/// 每排物品数（上下滚动，窄长条但每排放 4 个）
const ITEMS_PER_ROW: usize = 4;
/// 物品槽尺寸
const SLOT_SIZE: f32 = 56.0;
/// 左侧整体面板宽度
pub(crate) const PANEL_WIDTH: f32 = 4.0 * (SLOT_SIZE + 6.0) + 6.0 + 52.0 + 40.0;
/// 滚轮分工边界：鼠标 x 在此左侧=滚物品栏，右侧=缩放镜头
/// （旧代码两处硬编码 330，比实际面板窄 16px，物品栏最后一列出现死区）
pub(crate) const WHEEL_PANEL_BOUNDARY: f32 = PANEL_WIDTH + 4.0;
/// 搜索框命中区上沿（面板顶 padding + 框高 + 余量），点击此线以上获得输入焦点
pub(crate) const SEARCH_FOCUS_TOP: f32 = 52.0;

/// 搜索框输入焦点：焦点在文本框时抑制全局热键（S/L/M/数字键/WASD……），
/// 否则在搜索框打字会同时保存读档、切地形、开车。
#[derive(Resource, Default)]
pub struct SearchFocus(pub bool);

/// 分类区显示模式：类别分类 或 势力分类（顶部旋转按钮切换）
#[derive(Resource, Default, PartialEq, Clone, Copy)]
pub enum ClassifyMode {
    #[default]
    Category,
    Corp,
}

/// 旋转切换按钮（分类区上方）
#[derive(Component)]
pub struct ClassifySwitch;

/// 分类区内容容器（模式切换时重建）
#[derive(Component)]
pub struct CategoryListRoot;

// ---------- 组件 ----------

/// 右上载具面板信息文本（系统每帧重写）
#[derive(Component)]
pub struct VehicleInfoText;

/// 左上“选中模块”文本
#[derive(Component)]
pub(crate) struct StatusSelected;

/// 左上“放置状态”文本
#[derive(Component)]
pub(crate) struct StatusPlacement;

/// 搜索框（EditableText 输入 + 过滤物品栏）
#[derive(Component)]
pub struct SearchBox;

/// 分类区槽位（None = 全部）
#[derive(Component)]
pub struct CategorySlot {
    pub category: Option<Category>,
}

/// 物品栏物品槽位
#[derive(Component)]
pub struct ItemSlot {
    pub def_id: String,
}

/// 物品栏滚动容器（上下滚动）
#[derive(Component)]
pub struct ItemScroller;

/// 悬停详情面板（默认隐藏）
#[derive(Component)]
pub struct Tooltip;

#[derive(Component)]
pub struct TooltipTitle;

#[derive(Component)]
pub struct TooltipBody;

/// 右下 toast 反馈文本
#[derive(Component)]
pub struct ToastText;

/// 底部中央操作提示
#[derive(Component)]
pub(crate) struct HelpText;

/// toast 内容：文本 + 剩余显示时间
#[derive(Resource, Default)]
pub struct ToastState(pub Option<(String, f32)>);

/// 当前分类过滤（None = 全部）
#[derive(Resource, Default)]
pub struct CategoryFilter(pub Option<Category>);

/// 地形切换请求：Some(0)=切到平坦, Some(1)=切到起伏（设置面板点击触发）
#[derive(Resource, Default)]
pub struct TerrainToggle(pub Option<u8>);

/// 设置面板：打开状态
#[derive(Resource, Default)]
pub struct SettingsOpen(pub bool);

/// 设置面板根
#[derive(Component)]
pub struct SettingsPanel;

/// 设置面板地形按钮（None=平坦, Some(true)=起伏）
#[derive(Component)]
pub struct TerrainButton {
    pub rolling: bool,
}

/// 设置面板关闭按钮
#[derive(Component)]
pub struct SettingsCloseBtn;

/// 齿轮按钮（打开设置）
#[derive(Component)]
pub struct GearButton;

/// 载具右键菜单
#[derive(Component)]
pub struct VehicleMenuPanel;

/// 载具菜单信息行
#[derive(Component)]
pub struct VehicleMenuTitle;

/// 载具菜单“驾驶”按钮
#[derive(Component)]
pub struct VehicleMenuDriveBtn;

/// 载具菜单关闭按钮（独立 marker，不再复用 SettingsCloseBtn——
/// 复用时两组查询互相命中，点一个关闭两个面板）
#[derive(Component)]
pub struct VehicleMenuCloseBtn;

/// 势力过滤（None = 全部势力）
#[derive(Resource, Default)]
pub struct CorpFilter(pub Option<String>);

/// 势力槽位（corp 名，空字符串 = 全部）
#[derive(Component)]
pub struct CorpSlot {
    pub corp: String,
}

/// 搜索词（从搜索框每帧同步）
#[derive(Resource, Default)]
pub struct SearchText(pub String);

// ---------- 工具 ----------

fn ui_rect(v: f32) -> UiRect {
    UiRect::all(Val::Px(v))
}

fn panel_bg() -> BackgroundColor {
    BackgroundColor(PANEL_BG)
}

fn panel_border() -> BorderColor {
    BorderColor::all(PANEL_BORDER)
}

fn text_style(font: &Handle<Font>, size: f32, color: Color) -> (TextFont, TextColor) {
    (TextFont { font: FontSource::Handle(font.clone()), font_size: FontSize::Px(size), ..default() }, TextColor(color))
}

/// 分类显示名（最多 3 字）
fn category_name(category: Option<Category>) -> &'static str {
    match category {
        None => "全部",
        Some(Category::Structure) => "结构",
        Some(Category::Cab) => "驾驶",
        Some(Category::Wheel) => "轮子",
        Some(Category::Engine) => "引擎",
        Some(Category::Weapon) => "武器",
        Some(Category::FuelTank) => "油箱",
        Some(Category::Light) => "灯",
    }
}

// ---------- Startup ----------

pub fn spawn_ui(mut commands: Commands, asset_server: Res<AssetServer>) {
    let font: Handle<Font> = asset_server.load("fonts/simhei.ttf");

    spawn_inventory_panel(&mut commands, &font);

    // 右上：载具面板
    commands.spawn((
        Node {
            position_type: PositionType::Absolute,
            top: Val::Px(8.0),
            right: Val::Px(8.0),
            padding: ui_rect(10.0),
            flex_direction: FlexDirection::Column,
            row_gap: Val::Px(4.0),
            align_items: AlignItems::FlexEnd,
            border: ui_rect(1.0),
            border_radius: BorderRadius::all(Val::Px(8.0)),
            ..default()
        },
        panel_bg(),
        panel_border(),
    ))
    .with_children(|p| {
        let (f, c) = text_style(&font, 12.0, TEXT_DIM);
        p.spawn((Text::new("载具"), f, c));
        let (f, c) = text_style(&font, 13.0, TEXT_MAIN);
        p.spawn((VehicleInfoText, Text::new("未组装"), f, c));
    });

    // 底部中央：操作提示（低透明度，不挡建造视野）
    commands.spawn((
        Node {
            position_type: PositionType::Absolute,
            bottom: Val::Px(8.0),
            width: Val::Percent(100.0),
            justify_content: JustifyContent::Center,
            ..default()
        },
        FocusPolicy::Pass,
    ))
    .with_children(|p| {
        let (f, c) = text_style(&font, 12.0, Color::srgba(0.85, 0.87, 0.9, 0.5));
        p.spawn((
            HelpText,
            Text::new("数字1-9选模块 · 左键拿起/放置 · R旋转 · ESC取消 · WASD驾驶 · 方向键转向 · S/L存档 · M地形 · 右键视角 · QE微调 · +/-缩放"),
            f,
            c,
        ));
    });

    // 右下：toast 反馈
    commands.spawn((
        Node {
            position_type: PositionType::Absolute,
            bottom: Val::Px(52.0),
            right: Val::Px(10.0),
            padding: ui_rect(8.0),
            border: ui_rect(1.0),
            border_radius: BorderRadius::all(Val::Px(6.0)),
            ..default()
        },
        panel_bg(),
        panel_border(),
    ))
    .with_children(|p| {
        let (f, c) = text_style(&font, 13.0, TEXT_MAIN);
        p.spawn((ToastText, Text::new(""), f, c));
    });

    // 载具右键菜单（默认隐藏）
    commands.spawn((
        VehicleMenuPanel,
        Node {
            position_type: PositionType::Absolute,
            left: Val::Px(0.0),
            top: Val::Px(0.0),
            display: Display::None,
            padding: ui_rect(12.0),
            flex_direction: FlexDirection::Column,
            row_gap: Val::Px(6.0),
            min_width: Val::Px(150.0),
            border: ui_rect(1.0),
            border_radius: BorderRadius::all(Val::Px(8.0)),
            ..default()
        },
        panel_bg(),
        panel_border(),
        FocusPolicy::Pass,
    ))
    .with_children(|p| {
        let (f, c) = text_style(&font, 13.0, TEXT_MAIN);
        p.spawn((VehicleMenuTitle, Text::new("载具"), f, c));
        p.spawn(Node {
            flex_direction: FlexDirection::Row,
            column_gap: Val::Px(6.0),
            ..default()
        })
        .with_children(|row| {
            row.spawn((
                Button,
                VehicleMenuDriveBtn,
                Node {
                    height: Val::Px(26.0),
                    padding: UiRect::axes(Val::Px(10.0), Val::Px(0.0)),
                    align_items: AlignItems::Center,
                    justify_content: JustifyContent::Center,
                    border: ui_rect(1.0),
                    border_radius: BorderRadius::all(Val::Px(5.0)),
                    ..default()
                },
                BackgroundColor(SLOT_BG),
                BorderColor::all(SLOT_ACTIVE_BORDER),
            ))
            .with_children(|slot| {
                let (f, c) = text_style(&font, 12.0, TEXT_MAIN);
                slot.spawn((Text::new("驾驶此载具"), f, c));
            });
            row.spawn((
                Button,
                VehicleMenuCloseBtn,
                Node {
                    height: Val::Px(26.0),
                    padding: UiRect::axes(Val::Px(10.0), Val::Px(0.0)),
                    align_items: AlignItems::Center,
                    justify_content: JustifyContent::Center,
                    border: ui_rect(1.0),
                    border_radius: BorderRadius::all(Val::Px(5.0)),
                    ..default()
                },
                BackgroundColor(SLOT_BG),
                BorderColor::all(SLOT_BORDER),
            ))
            .with_children(|slot| {
                let (f, c) = text_style(&font, 12.0, TEXT_DIM);
                slot.spawn((Text::new("关闭"), f, c));
            });
        });
    });

    // 齿轮按钮（打开设置）
    commands.spawn((
        Button,
        GearButton,
        Node {
            position_type: PositionType::Absolute,
            bottom: Val::Px(10.0),
            right: Val::Px(10.0),
            width: Val::Px(32.0),
            height: Val::Px(32.0),
            align_items: AlignItems::Center,
            justify_content: JustifyContent::Center,
            border: ui_rect(1.0),
            border_radius: BorderRadius::all(Val::Px(6.0)),
            ..default()
        },
        BackgroundColor(SLOT_BG),
        BorderColor::all(SLOT_BORDER),
    ))
    .with_children(|slot| {
        let (f, c) = text_style(&font, 16.0, TEXT_MAIN);
        slot.spawn((Text::new("⚙"), f, c));
    });

    // 设置面板（默认隐藏，齿轮 / F1 打开）
    commands.spawn((
        SettingsPanel,
        Node {
            position_type: PositionType::Absolute,
            bottom: Val::Px(52.0),
            right: Val::Px(10.0),
            display: Display::None,
            padding: ui_rect(14.0),
            flex_direction: FlexDirection::Column,
            row_gap: Val::Px(8.0),
            width: Val::Px(260.0),
            border: ui_rect(1.0),
            border_radius: BorderRadius::all(Val::Px(8.0)),
            ..default()
        },
        panel_bg(),
        panel_border(),
        FocusPolicy::Pass,
    ))
    .with_children(|p| {
        let (f, c) = text_style(&font, 15.0, TEXT_MAIN);
        p.spawn((Text::new("设置"), f, c));
        // 相机
        let (f, c) = text_style(&font, 12.0, TEXT_DIM);
        p.spawn((Text::new("相机: 滚轮 / +/- 缩放 · 右键拖拽旋转 · Q/E 微调"), f, c));
        // 地形
        p.spawn(Node {
            flex_direction: FlexDirection::Row,
            column_gap: Val::Px(6.0),
            align_items: AlignItems::Center,
            ..default()
        })
        .with_children(|row| {
            let (f, c) = text_style(&font, 12.0, TEXT_MAIN);
            row.spawn((Text::new("地形:"), f, c));
            for (label, rolling) in [("平坦", false), ("起伏", true)] {
                row.spawn((
                    Button,
                    TerrainButton { rolling },
                    Node {
                        height: Val::Px(24.0),
                        padding: UiRect::axes(Val::Px(10.0), Val::Px(0.0)),
                        align_items: AlignItems::Center,
                        justify_content: JustifyContent::Center,
                        border: ui_rect(1.0),
                        border_radius: BorderRadius::all(Val::Px(5.0)),
                        ..default()
                    },
                    BackgroundColor(SLOT_BG),
                    BorderColor::all(SLOT_BORDER),
                ))
                .with_children(|slot| {
                    let (f, c) = text_style(&font, 12.0, TEXT_MAIN);
                    slot.spawn((Text::new(label), f, c));
                });
            }
        });
        // 操作说明
        let (f, c) = text_style(&font, 12.0, TEXT_DIM);
        p.spawn((
            Text::new("数字键 选模块 · 左键 拿起/放置 · R 旋转 · ESC 取消\nWASD 驾驶 · 方向键 转向 · S/L 存档 · M 地形\n右键 视角 · 滚轮 缩放/物品栏 · Q/E 微调"),
            f,
            c,
        ));
        // 关闭
        p.spawn((
            Button,
            SettingsCloseBtn,
            Node {
                height: Val::Px(26.0),
                align_items: AlignItems::Center,
                justify_content: JustifyContent::Center,
                border: ui_rect(1.0),
                border_radius: BorderRadius::all(Val::Px(5.0)),
                ..default()
            },
            BackgroundColor(SLOT_BG),
            BorderColor::all(SLOT_BORDER),
        ))
        .with_children(|slot| {
            let (f, c) = text_style(&font, 12.0, TEXT_MAIN);
            slot.spawn((Text::new("关闭"), f, c));
        });
    });

    // 悬停详情 tooltip（默认隐藏，跟随鼠标）
    commands.spawn((
        Tooltip,
        Node {
            position_type: PositionType::Absolute,
            left: Val::Px(0.0),
            top: Val::Px(0.0),
            display: Display::None,
            padding: ui_rect(10.0),
            flex_direction: FlexDirection::Column,
            row_gap: Val::Px(3.0),
            min_width: Val::Px(150.0),
            border: ui_rect(1.0),
            border_radius: BorderRadius::all(Val::Px(6.0)),
            ..default()
        },
        panel_bg(),
        panel_border(),
        FocusPolicy::Pass,
    ))
    .with_children(|p| {
        let (f, c) = text_style(&font, 14.0, TEXT_MAIN);
        p.spawn((TooltipTitle, Text::new(""), f, c));
        let (f, c) = text_style(&font, 12.0, TEXT_DIM);
        p.spawn((TooltipBody, Text::new(""), f, c));
    });
}

/// 左下：左侧满高面板——搜索框 → 状态 → [旋转按钮+分类竖列 | 物品栏(满高滚动)]
fn spawn_inventory_panel(commands: &mut Commands, font: &Handle<Font>) {
    commands.spawn((
        Node {
            position_type: PositionType::Absolute,
            top: Val::Px(4.0),
            bottom: Val::Px(4.0),
            left: Val::Px(8.0),
            padding: ui_rect(6.0),
            flex_direction: FlexDirection::Column,
            row_gap: Val::Px(5.0),
            border: ui_rect(1.0),
            border_radius: BorderRadius::all(Val::Px(8.0)),
            width: Val::Px(PANEL_WIDTH),
            ..default()
        },
        panel_bg(),
        panel_border(),
    ))
    .with_children(|p| {
        // 搜索框
        let (sf, sc) = text_style(font, 12.0, TEXT_MAIN);
        p.spawn((
            SearchBox,
            EditableText::new(""),
            sf,
            sc,
            Node {
                width: Val::Percent(100.0),
                height: Val::Px(26.0),
                padding: UiRect::axes(Val::Px(8.0), Val::Px(3.0)),
                border: ui_rect(1.0),
                border_radius: BorderRadius::all(Val::Px(5.0)),
                ..default()
            },
            BackgroundColor(Color::srgba(0.12, 0.13, 0.17, 0.95)),
            BorderColor::all(SLOT_BORDER),
        ));

        // 建造状态（选中 / 放置状态），替代原左上角状态面板
        p.spawn(Node {
            flex_direction: FlexDirection::Column,
            row_gap: Val::Px(1.0),
            ..default()
        })
        .with_children(|s| {
            let (f, c) = text_style(font, 11.0, TEXT_MAIN);
            s.spawn((StatusSelected, Text::new("选中: -"), f, c));
            let (f, c) = text_style(font, 11.0, TEXT_DIM);
            s.spawn((StatusPlacement, Text::new("状态: -"), f, c));
        });

        // 内容行：旋转按钮 + 分类竖列 | 物品栏
        p.spawn(Node {
            flex_direction: FlexDirection::Row,
            column_gap: Val::Px(6.0),
            flex_grow: 1.0,
            min_height: Val::Px(0.0),
            ..default()
        })
        .with_children(|row| {
            // 左列：旋转切换按钮 + 分类竖列
            row.spawn(Node {
                flex_direction: FlexDirection::Column,
                row_gap: Val::Px(4.0),
                align_items: AlignItems::Center,
                ..default()
            })
            .with_children(|left| {
                // 旋转按钮：类别分类 ↔ 势力分类
                left.spawn((
                    Button,
                    ClassifySwitch,
                    Node {
                        width: Val::Px(52.0),
                        height: Val::Px(22.0),
                        align_items: AlignItems::Center,
                        justify_content: JustifyContent::Center,
                        border: ui_rect(1.0),
                        border_radius: BorderRadius::all(Val::Px(5.0)),
                        ..default()
                    },
                    BackgroundColor(SLOT_BG),
                    BorderColor::all(SLOT_BORDER),
                ))
                .with_children(|slot| {
                    let (f, c) = text_style(font, 12.0, TEXT_MAIN);
                    slot.spawn((Text::new("↻"), f, c));
                });
                // 分类竖列（内容由 ui_category_list_system 按模式重建）
                left.spawn((
                    CategoryListRoot,
                    Node {
                        flex_direction: FlexDirection::Column,
                        row_gap: Val::Px(4.0),
                        width: Val::Px(52.0),
                        flex_grow: 1.0,
                        min_height: Val::Px(0.0),
                        overflow: Overflow {
                            x: OverflowAxis::Visible,
                            y: OverflowAxis::Scroll,
                        },
                        ..default()
                    },
                    ScrollPosition::default(),
                ));
            });

            // 物品栏：每排 4 个，满高上下滚动
            row.spawn((
                ItemScroller,
                Node {
                    flex_direction: FlexDirection::Column,
                    row_gap: Val::Px(4.0),
                    overflow: Overflow {
                        x: OverflowAxis::Visible,
                        y: OverflowAxis::Scroll,
                    },
                    width: Val::Px(ITEMS_PER_ROW as f32 * (SLOT_SIZE + 6.0) + 6.0),
                    flex_grow: 1.0,
                    min_height: Val::Px(0.0),
                    ..default()
                },
                ScrollPosition::default(),
            ))
            .with_children(|p| {
                p.spawn((
                    Node {
                        flex_direction: FlexDirection::Column,
                        row_gap: Val::Px(4.0),
                        ..default()
                    },
                    InventoryContent,
                ));
            });
        });
    });
}

/// 物品栏内容容器（重建时整体替换）
#[derive(Component)]
pub(crate) struct InventoryContent;

// ---------- 物品栏 ----------

/// 搜索框 → SearchText 同步（内容变化时才写）
pub fn ui_search_system(
    mut search: ResMut<SearchText>,
    mut boxes: Query<&EditableText, (With<SearchBox>, Changed<EditableText>)>,
) {
    for box_text in boxes.iter_mut() {
        let value = box_text.value().to_string();
        if value != search.0 {
            search.0 = value;
        }
    }
}

/// 搜索框焦点维护：点击搜索框区域获得焦点（打字不再触发热键），
/// 点击别处 / Enter / Esc 释放。几何近似判定——搜索框位于面板顶部固定位置。
pub fn ui_search_focus_system(
    mut focus: ResMut<SearchFocus>,
    keys: Res<ButtonInput<KeyCode>>,
    clicks: Res<ButtonInput<MouseButton>>,
    cursor: Res<CursorPos>,
) {
    if keys.just_pressed(KeyCode::Escape) || keys.just_pressed(KeyCode::Enter) {
        focus.0 = false;
        return;
    }
    if clicks.just_pressed(MouseButton::Left) {
        focus.0 = cursor.pos
            .map(|p| p.x <= WHEEL_PANEL_BOUNDARY && p.y <= SEARCH_FOCUS_TOP)
            .unwrap_or(false);
    }
}

/// 旋转按钮：切换分类区模式（类别 ↔ 势力）
pub fn ui_classify_switch_system(
    mut mode: ResMut<ClassifyMode>,
    mut switches: Query<&Interaction, (With<ClassifySwitch>, Changed<Interaction>)>,
) {
    for interaction in switches.iter_mut() {
        if *interaction != Interaction::Pressed { continue; }
        *mode = match *mode {
            ClassifyMode::Category => ClassifyMode::Corp,
            ClassifyMode::Corp => ClassifyMode::Category,
        };
    }
}

/// 分类区重建：模式切换时重建竖列（类别槽 或 势力槽）
pub fn ui_category_list_system(
    asm: Res<AsmRes>,
    mode: Res<ClassifyMode>,
    asset_server: Res<AssetServer>,
    mut commands: Commands,
    mut roots: Query<(Entity, &mut ScrollPosition), With<CategoryListRoot>>,
    mut last: Local<Option<ClassifyMode>>,
    mut font_cache: Local<Option<Handle<Font>>>,
) {
    if *last == Some(*mode) { return; }
    *last = Some(*mode);
    let font = font_cache.get_or_insert_with(|| asset_server.load("fonts/simhei.ttf")).clone();
    for (entity, mut scroll) in roots.iter_mut() {
        scroll.0.y = 0.0;
        commands.entity(entity).despawn_children();
        let mut children = commands.entity(entity);
        match *mode {
            ClassifyMode::Category => spawn_category_slots(&mut children, &font, &asm),
            ClassifyMode::Corp => spawn_corp_slots(&mut children, &font, &asm),
        }
    }
}

fn spawn_category_slots(children: &mut EntityCommands, font: &Handle<Font>, asm: &AsmRes) {
    let mut cats: Vec<Option<Category>> = vec![None];
    cats.extend(
        [
            Category::Structure, Category::Cab, Category::Wheel,
            Category::Engine, Category::Weapon, Category::FuelTank, Category::Light,
        ].into_iter().filter(|c| {
            asm.0.defs.values().any(|d| d.category == *c)
        }).map(Some),
    );
    for category in cats {
        let color = match category {
            Some(c) => cat_color(c),
            None => Color::srgb(0.55, 0.58, 0.65),
        };
        children.with_children(|slot| {
            slot.spawn((
                Button,
                CategorySlot { category },
                Node {
                    width: Val::Px(52.0),
                    height: Val::Px(24.0),
                    align_items: AlignItems::Center,
                    justify_content: JustifyContent::Center,
                    border: ui_rect(1.0),
                    border_radius: BorderRadius::all(Val::Px(5.0)),
                    ..default()
                },
                BackgroundColor(SLOT_BG),
                BorderColor::all(SLOT_BORDER),
            ))
            .with_children(|s| {
                let (f, c) = text_style(font, 11.0, TEXT_MAIN);
                s.spawn((Text::new(category_name(category)), f, c));
                s.spawn((
                    Node {
                        position_type: PositionType::Absolute,
                        left: Val::Px(3.0),
                        top: Val::Px(3.0),
                        width: Val::Px(5.0),
                        height: Val::Px(5.0),
                        border_radius: BorderRadius::all(Val::Px(2.5)),
                        ..default()
                    },
                    BackgroundColor(color),
                ));
            });
        });
    }
}

fn spawn_corp_slots(children: &mut EntityCommands, font: &Handle<Font>, asm: &AsmRes) {
    let mut corps: Vec<String> = vec![String::new()];
    let mut present: Vec<String> = asm.0.defs.values().map(|d| d.corp.clone()).collect();
    present.sort();
    present.dedup();
    corps.extend(present);
    for corp in corps {
        let label = if corp.is_empty() { "全部".to_string() } else { corp.clone() };
        children.with_children(|slot| {
            slot.spawn((
                Button,
                CorpSlot { corp },
                Node {
                    width: Val::Px(52.0),
                    height: Val::Px(24.0),
                    align_items: AlignItems::Center,
                    justify_content: JustifyContent::Center,
                    border: ui_rect(1.0),
                    border_radius: BorderRadius::all(Val::Px(5.0)),
                    ..default()
                },
                BackgroundColor(SLOT_BG),
                BorderColor::all(SLOT_BORDER),
            ))
            .with_children(|s| {
                let (f, c) = text_style(font, 11.0, TEXT_MAIN);
                s.spawn((Text::new(label), f, c));
            });
        });
    }
}

/// 点击势力 → 切换势力过滤
pub fn ui_corp_system(
    mut filter: ResMut<CorpFilter>,
    mut slots: Query<(&CorpSlot, &Interaction), Changed<Interaction>>,
) {
    for (slot, interaction) in slots.iter_mut() {
        if *interaction != Interaction::Pressed { continue; }
        filter.0 = if slot.corp.is_empty() { None } else { Some(slot.corp.clone()) };
    }
}

/// 物品栏过滤状态（分类 + 势力 + 搜索词）
type FilterState = (Option<Category>, Option<String>, String);

/// 分类/势力/搜索过滤变化时重建物品栏（每排 4 个，上下滚动）
#[allow(clippy::too_many_arguments)]
pub fn ui_inventory_system(
    asm: Res<AsmRes>,
    filter: Res<CategoryFilter>,
    corp: Res<CorpFilter>,
    search: Res<SearchText>,
    sel: Res<Selection>,
    asset_server: Res<AssetServer>,
    mut commands: Commands,
    mut content: Query<(Entity, &mut Node), With<InventoryContent>>,
    mut slots: Query<(&ItemSlot, &mut BorderColor)>,
    mut last: Local<Option<FilterState>>,
) {
    // 高亮跟随当前选择（每帧轻量更新）
    for (slot, mut border) in slots.iter_mut() {
        border.set_all(if slot.def_id == sel.def_id { SLOT_ACTIVE_BORDER } else { SLOT_BORDER });
    }
    let current = (filter.0, corp.0.clone(), search.0.clone());
    if *last == Some(current.clone()) {
        return;
    }
    *last = Some(current.clone());

    // 按势力 + 类别 + 搜索词过滤（名称/id 包含，忽略大小写）
    let keyword = current.2.trim().to_lowercase();
    let mut defs: Vec<&vxl_core::module::ModuleDef> = asm.0.defs.values()
        .filter(|d| current.0.is_none_or(|c| d.category == c))
        .filter(|d| current.1.as_deref().is_none_or(|c| d.corp == c))
        .filter(|d| {
            keyword.is_empty()
                || d.name.to_lowercase().contains(&keyword)
                || d.id.to_lowercase().contains(&keyword)
        })
        .collect();
    defs.sort_by(|a, b| a.id.cmp(&b.id));

    let Ok((entity, _)) = content.single_mut() else { return };
    commands.entity(entity).despawn_children();

    let rows: Vec<Vec<&vxl_core::module::ModuleDef>> = defs.chunks(ITEMS_PER_ROW)
        .map(|chunk| chunk.to_vec())
        .collect();

    let mut children = commands.entity(entity);
    for row in &rows {
        let row = row.clone();
        children.with_children(|row_node| {
            row_node.spawn(Node {
                flex_direction: FlexDirection::Row,
                column_gap: Val::Px(8.0),
                ..default()
            })
            .with_children(|row_children| {
                for def in &row {
                    spawn_item_slot(row_children, &asset_server, def);
                }
            });
        });
    }
}

fn spawn_item_slot(
    parent: &mut bevy::ecs::relationship::RelatedSpawnerCommands<'_, ChildOf>,
    asset_server: &Res<AssetServer>,
    def: &vxl_core::module::ModuleDef,
) {
    parent.spawn((
        Button,
        ItemSlot { def_id: def.id.clone() },
        Node {
            width: Val::Px(SLOT_SIZE),
            height: Val::Px(SLOT_SIZE),
            align_items: AlignItems::Center,
            justify_content: JustifyContent::Center,
            border: ui_rect(1.0),
            border_radius: BorderRadius::all(Val::Px(6.0)),
            overflow: Overflow {
                x: OverflowAxis::Clip,
                y: OverflowAxis::Clip,
            },
            ..default()
        },
        BackgroundColor(SLOT_BG),
        BorderColor::all(SLOT_BORDER),
    ))
    .with_children(|slot| {
        // 缩略图：data/thumbnails/{def_id}.png（Blender 缩略图插件输出）
        slot.spawn((
            ImageNode {
                image: asset_server.load(format!("thumbnails/{}.png", def.id)),
                ..default()
            },
            Node {
                width: Val::Percent(100.0),
                height: Val::Percent(100.0),
                ..default()
            },
        ));
        slot.spawn((
            Node {
                position_type: PositionType::Absolute,
                bottom: Val::Px(2.0),
                left: Val::Px(2.0),
                width: Val::Percent(100.0),
                height: Val::Px(2.0),
                border_radius: BorderRadius::all(Val::Px(1.0)),
                ..default()
            },
            BackgroundColor(cat_color(def.category)),
        ));
    });
}

/// 点击物品 → 选择模块（与数字键同一入口）
pub fn ui_item_click_system(
    mut sel: ResMut<Selection>,
    mut picked: ResMut<Picked>,
    mut hud: ResMut<HudState>,
    time: Res<Time>,
    mut slots: Query<(&ItemSlot, &Interaction), Changed<Interaction>>,
) {
    for (slot, interaction) in slots.iter_mut() {
        if *interaction != Interaction::Pressed { continue; }
        if picked.module_id.is_some() && picked.module_id != Some(PLACING_NEW) { continue; }
        sel.def_id = slot.def_id.clone();
        picked.module_id = Some(PLACING_NEW);
        picked.def_id = slot.def_id.clone();
        picked.rot = 0;
        picked.vehicle = 0;
        picked.origin = None;
        // 记录入手时间：新模块模式的短按防误触门槛由此生效
        // （旧代码写 None，release 分支读到 None 直接跳过门槛 → 单击即放置）
        picked.hold_start = Some(time.elapsed_secs());
        picked.was_root = false;
        hud.selected = sel.def_id.clone();
    }
}

/// 点击分类 → 切换过滤
pub fn ui_category_system(
    mut filter: ResMut<CategoryFilter>,
    mut slots: Query<(&CategorySlot, &Interaction), Changed<Interaction>>,
) {
    for (slot, interaction) in slots.iter_mut() {
        if *interaction != Interaction::Pressed { continue; }
        filter.0 = slot.category;
    }
}

/// 物品栏滚轮滚动（鼠标在左侧面板内时，两排并行上下滚动）
pub fn ui_item_scroll_system(
    mut scrollers: Query<&mut ScrollPosition, With<ItemScroller>>,
    cursor: Res<CursorPos>,
    mut wheel: MessageReader<MouseWheel>,
) {
    // 鼠标不在左侧面板区域时不滚动（那里滚轮归镜头缩放）；边界与 camera_control 共用常量
    if cursor.pos.is_none_or(|p| p.x > WHEEL_PANEL_BOUNDARY) { return; }
    let mut total = 0.0f32;
    for ev in wheel.read() {
        total += ev.y;
    }
    if total == 0.0 { return; }
    for mut scroll in scrollers.iter_mut() {
        scroll.0.y = (scroll.0.y - total * 48.0).clamp(0.0, 4000.0);
    }
}

/// 悬停物品 → 显示详情 tooltip（跟随鼠标）
pub fn ui_tooltip_system(
    asm: Res<AsmRes>,
    cursor: Res<CursorPos>,
    slots: Query<(&ItemSlot, &Interaction), Changed<Interaction>>,
    mut tooltip: Query<&mut Node, (With<Tooltip>, Without<ItemSlot>)>,
    mut title: Query<&mut Text, With<TooltipTitle>>,
    mut body: Query<&mut Text, (With<TooltipBody>, Without<TooltipTitle>)>,
) {
    let mut hovering: Option<String> = None;
    for (slot, interaction) in slots.iter() {
        if *interaction == Interaction::Hovered {
            hovering = Some(slot.def_id.clone());
            break;
        }
    }
    let Ok(mut node) = tooltip.single_mut() else { return };
    let Some(def_id) = hovering else {
        node.display = Display::None;
        return;
    };
    let Some(def) = asm.0.defs.get(&def_id) else { return };
    node.display = Display::Flex;
    if let Some(pos) = cursor.pos {
        node.left = Val::Px((pos.x + 16.0).min(1500.0));
        node.top = Val::Px((pos.y - 90.0).max(8.0));
    }
    for mut t in title.iter_mut() {
        t.0 = format!("{}（{}）", def.name, category_name(Some(def.category)));
    }
    let mut body_text = String::new();
    body_text.push_str(&format!("厂商: {}\n", def.corp));
    body_text.push_str(&format!("质量: {:.0}    HP: {}\n", def.mass, def.hp));
    body_text.push_str(&format!("挂点: {} 个", def.mount_points.len()));
    for mut t in body.iter_mut() {
        t.0 = body_text.clone();
    }
}

// ---------- 其他 Update 系统 ----------

/// 左上状态：选中模块名称 + 放置状态（语义色）
#[allow(clippy::type_complexity)]
pub fn ui_status_system(
    hud: Res<HudState>,
    asm: Res<AsmRes>,
    mut texts: ParamSet<(
        Query<&mut Text, With<StatusSelected>>,
        Query<&mut Text, (With<StatusPlacement>, Without<StatusSelected>)>,
    )>,
) {
    let name = if hud.selected.is_empty() {
        "未选择".to_string()
    } else {
        asm.0.defs.get(&hud.selected).map(|d| d.name.clone()).unwrap_or_else(|| hud.selected.clone())
    };
    for mut t in texts.p0().iter_mut() {
        t.0 = format!("选中: {name}");
    }
    let status = if hud.status.is_empty() { "空闲".to_string() } else { hud.status.clone() };
    for mut t in texts.p1().iter_mut() {
        t.0 = format!("状态: {status}");
    }
}

/// 右上载具面板：质量 / 模块数 / 轮子数 / 平均 HP
pub fn ui_vehicle_system(
    asm: Res<AsmRes>,
    mut texts: Query<&mut Text, With<VehicleInfoText>>,
) {
    let a = &asm.0;
    let Some(root) = a.root else {
        for mut t in texts.iter_mut() { t.0 = "未组装".into(); }
        return;
    };
    let vehicle = a.modules.get(&root).map(|m| m.vehicle).unwrap_or(0);
    if vehicle == 0 {
        for mut t in texts.iter_mut() { t.0 = "未组装".into(); }
        return;
    }
    let mut mass = 0.0f32;
    let mut count = 0usize;
    let mut wheels = 0usize;
    let mut hp_sum = 0.0f32;
    let mut hp_max = 0.0f32;
    for md in a.modules.values().filter(|m| m.vehicle == vehicle) {
        if let Some(def) = a.defs.get(&md.def_id) {
            mass += def.mass;
            hp_sum += md.hp as f32;
            hp_max += def.hp as f32;
            if def.category == Category::Wheel { wheels += 1; }
        }
        count += 1;
    }
    let hp = if hp_max > 0.0 { hp_sum / hp_max * 100.0 } else { 0.0 };
    for mut t in texts.iter_mut() {
        t.0 = format!("质量 {mass:.0}\n模块 {count}\n轮子 {wheels}\nHP {hp:.0}%");
    }
}

/// toast：计时淡出
pub fn ui_toast_system(
    time: Res<Time>,
    mut toast: ResMut<ToastState>,
    mut texts: Query<(&mut Text, &mut TextColor), With<ToastText>>,
) {
    let Some((text, mut left)) = toast.0.take() else { return };
    left -= time.delta_secs();
    if left <= 0.0 {
        for (mut t, mut c) in texts.iter_mut() {
            t.0 = String::new();
            c.0 = TEXT_MAIN;
        }
        return;
    }
    for (mut t, mut c) in texts.iter_mut() {
        t.0 = text.clone();
        let alpha = (left / 0.5).clamp(0.0, 1.0);
        let base = TEXT_MAIN.to_srgba();
        c.0 = Color::srgba(base.red, base.green, base.blue, alpha);
    }
    toast.0 = Some((text, left));
}

/// 载具右键菜单：显隐/内容/定位；“驾驶”按钮切换 ActiveVehicle
#[allow(clippy::too_many_arguments)]
pub fn ui_vehicle_menu_system(
    asm: Res<AsmRes>,
    mut menu: ResMut<VehicleMenu>,
    mut active: ResMut<ActiveVehicle>,
    mut toast: ResMut<ToastState>,
    mut panels: Query<&mut Node, With<VehicleMenuPanel>>,
    mut titles: Query<&mut Text, With<VehicleMenuTitle>>,
    mut closes: Query<&Interaction, (With<VehicleMenuCloseBtn>, Changed<Interaction>)>,
    mut drives: Query<&Interaction, (With<VehicleMenuDriveBtn>, Changed<Interaction>)>,
) {
    // 驾驶按钮：切换到菜单对应载具
    for interaction in drives.iter_mut() {
        if *interaction == Interaction::Pressed
            && let Some((_, vehicle)) = menu.0 {
            active.0 = Some(vehicle);
            show_toast(&mut toast, format!("驾驶载具 #{}", vehicle));
            menu.0 = None;
        }
    }
    let Some((pos, vehicle)) = menu.0 else {
        for mut node in panels.iter_mut() { node.display = Display::None; }
        return;
    };
    let module_count = asm.0.modules.values().filter(|m| m.vehicle == vehicle).count();
    // 菜单引用的载具被拆光 → 自动关菜单，避免"驾驶死 id"按钮残留
    if module_count == 0 {
        menu.0 = None;
        for mut node in panels.iter_mut() { node.display = Display::None; }
        return;
    }
    let wheel_count = asm.0.modules.values().filter(|m| {
        m.vehicle == vehicle && asm.0.defs.get(&m.def_id).is_some_and(|d| d.category == Category::Wheel)
    }).count();
    let is_active = active.0 == Some(vehicle);
    for mut t in titles.iter_mut() {
        t.0 = format!("载具 #{} · 模块 {} · 轮子 {}{}", vehicle, module_count, wheel_count,
            if is_active { "（当前驾驶）" } else { "" });
    }
    for interaction in closes.iter_mut() {
        if *interaction == Interaction::Pressed { menu.0 = None; }
    }
    for mut node in panels.iter_mut() {
        node.display = Display::Flex;
        node.left = Val::Px((pos.x + 8.0).min(1500.0));
        node.top = Val::Px((pos.y - 40.0).max(8.0));
    }
}

#[allow(clippy::type_complexity)]
pub fn ui_settings_system(
    keys: Res<ButtonInput<KeyCode>>,
    mut open: ResMut<SettingsOpen>,
    mut gears: Query<&Interaction, (With<GearButton>, Changed<Interaction>)>,
    mut closes: Query<&Interaction, (With<SettingsCloseBtn>, Changed<Interaction>)>,
    mut panels: Query<&mut Node, (With<SettingsPanel>, Without<GearButton>, Without<SettingsCloseBtn>)>,
) {
    if keys.just_pressed(KeyCode::F1) { open.0 = !open.0; }
    for interaction in gears.iter_mut() {
        if *interaction == Interaction::Pressed { open.0 = !open.0; }
    }
    for interaction in closes.iter_mut() {
        if *interaction == Interaction::Pressed { open.0 = false; }
    }
    for mut node in panels.iter_mut() {
        node.display = if open.0 { Display::Flex } else { Display::None };
    }
}

/// 设置面板地形按钮 → 地形切换请求（M 键同效）
pub fn ui_terrain_buttons_system(
    mut toggle: ResMut<TerrainToggle>,
    mut buttons: Query<(&TerrainButton, &Interaction), Changed<Interaction>>,
) {
    for (button, interaction) in buttons.iter_mut() {
        if *interaction == Interaction::Pressed {
            toggle.0 = Some(if button.rolling { 1 } else { 0 });
        }
    }
}

/// 发送 toast 文案（供 save_load 等系统调用）
pub fn show_toast(toast: &mut ToastState, text: impl Into<String>) {
    toast.0 = Some((text.into(), TOAST_SECS));
}
