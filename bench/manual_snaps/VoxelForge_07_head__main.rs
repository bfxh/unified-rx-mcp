//! VoxelForge 2.0 渲染壳（Bevy 0.18）。
//!
//! 演示：从 core Assembly 生成模块实体（立方体，scale=dims、rotation=旋转矩阵
//! Quat、center=矩阵旋转——与 core 占格严格一致）；轨道相机 + 网格地面。
//!
//! V5：灰阶玻璃 UI + 逐层堆叠生成动画（层切片 + 涟漪延迟）+ 连接点虚线指引。
//! 新模块实体生成流程（sync_entities）：
//! 1. 正常实体立即生成（带 fx::BuildHold，近零缩放等待）
//! 2. 模块按世界 Y 层切片（fx::slice_layers），每层一个 LayerBuildFx 实体
//!    从底部向上展开（层序延迟 → 涟漪堆叠感）
//! 3. 层播完 → BuildHold 恢复正常缩放；顶层起播时辉光涟漪扩散

mod audio;
mod fx;
mod input;
mod input_map;
mod input_systems;
mod inventory;
mod module_texture;
mod physics;
mod procgen;
mod render_bridge;
mod resources;
mod terrain;
mod ui;

use bevy::prelude::*;
use vf2_core::assembly::Assembly;
use vf2_core::module::{Category, Face, ModuleDef, MountMask, MountPoint, Shape, Vec3i};
// 模块验证逻辑（rebuild 资产暂不加载——挂名模块删除，恢复加载时用 mount_rules::check_module）
use vf2_core::rotation::rotations_24;

use crate::input_systems::InteractPlugin;
use crate::render_bridge::ModuleRef;

/// 逐格实体标记（局部占用格——同步位置时按格计算；2026-08-21 P1-1 修复）
#[derive(Component, Clone, Copy)]
pub struct CellOffset(pub vf2_core::module::Vec3i);
use crate::resources::inventory_state::InventoryState;

/// 装配 → 实体同步（每帧 diff：spawn 缺失、despawn 多余——简单可靠）
///
/// 新模块带逐层生成动画（对所有模块通用——按形状切片，兼容任意 dims）。
/// Bevy 系统参数无法组合——clippy: too_many_arguments
#[allow(clippy::too_many_arguments)]
fn sync_entities(
    asm: Res<crate::render_bridge::AsmRes>,
    mut commands: Commands,
    mut meshes: ResMut<Assets<Mesh>>,
    mut images: ResMut<Assets<Image>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
    mut cache: ResMut<TextureCache>,
    q: Query<(Entity, &ModuleRef), Without<fx::LayerBuildFx>>,
) {
    let asm = &asm.0;
    let cube = meshes.add(Cuboid::new(0.95, 0.95, 0.95));
    let mut seen = std::collections::HashSet::new();
    // 已渲染模块集合（2026-08-21 P2-2：O(1) 判存在，替代每模块线性扫描实体）
    let rendered: std::collections::HashSet<vf2_core::assembly::ModuleId> =
        q.iter().map(|(_, r)| r.0).collect();
    for (mid, md) in asm.modules.iter() {
        seen.insert(mid);
        if rendered.contains(&mid) {
            continue;
        }
        let Some(def) = asm.defs.get(&md.def_id) else { continue };
        let rot = &rotations_24()[(md.rotation % 24) as usize];
        // 纹理材质缓存（按 def_id——修复每帧 add 资产泄漏）
        let material = cache.0.entry(md.def_id.clone()).or_insert_with(|| {
            let (sat, light) = module_texture::category_sat_light(def.category);
            let look = module_texture::module_look(&md.def_id, sat, light);
            let tex = images.add(module_texture::look_to_texture(&look));
            // AAA 材质（rust-engineer P1）：按类别差异化 metallic/roughness/reflectance
            // + emissive 发光（Light/Engine——配合 HDR 有辉光感）
            let (metallic, roughness, reflectance, emissive) =
                category_material(def.category);
            materials.add(StandardMaterial {
                base_color: Color::WHITE,
                base_color_texture: Some(tex),
                metallic,
                perceptual_roughness: roughness,
                reflectance,
                emissive,
                ..default()
            })
        }).clone();

        // === 逐格渲染（2026-08-19 高压检查：Cells 形状渲染成 dims 整块——
        // 体积露出/格子不匹配。改为每个占用格一个 cube 实体——镂空正确，
        // 与 hover 网格/占用检测一致；带逐格缩放入场动画（BuildHold））===
        let cell_count = def.shape.local_cells().len() as f32;
        // 2026-08-22 防穿模：模块刚性贴地——所有格共享一个基高（单点维护
        // module_render_base），不再逐格取地形高度（旧行为：多格模块在
        // 坡上一格高一格低→模块断裂/格互相穿插）。
        let base = crate::render_bridge::module_render_base(asm, mid);
        for (ci, lc) in def.shape.local_cells().iter().enumerate() {
            let wc = rot.apply_to_coord(*lc) + md.origin;
            let wx = wc.0 as f32 + 0.5;
            let wz = wc.2 as f32 + 0.5;
            let pos = Vec3::new(
                wx,
                base + wc.1 as f32 + 0.5,
                wz,
            );
            let target = Transform::from_translation(pos).with_scale(Vec3::splat(0.95));
            // 软吸附起点：目标上方 0.35m + 近零缩放（松手 → 0.1s ease-out 滑入，
            // MASTER_DESIGN §5.2）
            let start = Transform {
                translation: pos + Vec3::Y * 0.35,
                rotation: Quat::IDENTITY,
                scale: Vec3::splat(0.001),
            };
            let delay = fx::ripple_delay(pos) + ci as f32 * 0.05 * cell_count.min(6.0);
            commands.spawn((
                ModuleRef(mid),
                CellOffset(*lc),
                Mesh3d(cube.clone()),
                MeshMaterial3d(material.clone()),
                start,
                fx::BuildHold {
                    elapsed: 0.0,
                    total: delay + 0.1,
                    start,
                    target,
                },
            ));
        }
    }
    // despawn 多余实体（已从装配移除）
    for (e, r) in q.iter() {
        if !seen.contains(&r.0) {
            commands.entity(e).despawn();
        }
    }
}

/// 载具面板（2026-08-19 用户："右键长按单个载具模块出现面板"——
/// 面板显示**整个载具**的标准/统计 + 组件 UI（工厂等），因为方便）。
/// "载具"是状态简称：固定基地也可移动也可叫载具。
#[derive(Resource, Default)]
pub struct VehiclePanel {
    pub open: bool,
    pub entity: Option<Entity>,
}

/// 右键长按模块（0.4s 不动）→ 载具面板；拖拽=相机旋转；单击（拿起时）=放回
fn vehicle_panel_system(
    mut panel: ResMut<VehiclePanel>,
    mouse: Res<ButtonInput<MouseButton>>,
    keys: Res<ButtonInput<KeyCode>>,
    window: Query<&Window>,
    camera: Query<(&Camera, &GlobalTransform), With<Camera3d>>,
    modules: Query<(Entity, &Transform, &crate::render_bridge::ModuleRef)>,
    picked: Res<crate::input::Picked>,
    asm: Res<crate::render_bridge::AsmRes>,
    time: Res<Time>,
    fonts: Res<ui::font::UiFonts>,
    mut commands: Commands,
    mut hold: Local<(f32, Vec2, bool)>,
) {
    // ESC 或右键释放关闭
    if keys.just_pressed(KeyCode::Escape) && panel.open {
        if let Some(e) = panel.entity.take() {
            commands.entity(e).despawn();
        }
        panel.open = false;
    }
    if mouse.just_released(MouseButton::Right) && panel.open {
        if let Some(e) = panel.entity.take() {
            commands.entity(e).despawn();
        }
        panel.open = false;
    }
    if panel.open {
        return;
    }
    // 拿起中：右键=放回（input_systems 处理），不触发面板
    if picked.module.is_some() {
        return;
    }
    if mouse.just_pressed(MouseButton::Right) {
        hold.0 = time.elapsed_secs();
        hold.1 = window
            .single()
            .ok()
            .and_then(|w| w.cursor_position())
            .unwrap_or_default();
        hold.2 = false;
    }
    if !mouse.pressed(MouseButton::Right) || hold.2 {
        return;
    }
    let moved = window
        .single()
        .ok()
        .and_then(|w| w.cursor_position())
        .map(|p| (p - hold.1).length())
        .unwrap_or(f32::MAX);
    if time.elapsed_secs() - hold.0 < 0.4 || moved >= 10.0 {
        return; // 未到长按阈值或已移动（拖拽旋转）
    }
    // 长按达成：射线命中模块 → 打开载具面板
    let Ok(window) = window.single() else { return };
    let Ok((cam, cam_tf)) = camera.single() else { return };
    let Some(cursor) = window.cursor_position() else { return };
    let Some(ray) = crate::input::mouse_ray(window, cam, cam_tf, cursor) else {
        return;
    };
    if crate::input::hit_module(ray, &modules).is_none() {
        return;
    }
    hold.2 = true;
    // 载具统计（root 连通组）
    let asm = &asm.0;
    let root = asm.root;
    let mut stats = crate::ui::island::VehicleStats {
        name: String::new(),
        modules: 0,
        mass: 0.0,
        hp: 0u32,
        wheels: 0,
        has_cab: false,
        components: Vec::new(),
        stress: 0,
    };
    let mut seen = std::collections::HashSet::new();
    if let Some(r) = root {
        let mut stack = vec![r];
        seen.insert(r);
        while let Some(m) = stack.pop() {
            let Some(md) = asm.modules.get(m) else { continue };
            let Some(def) = asm.defs.get(&md.def_id) else { continue };
            if stats.name.is_empty() {
                stats.name = def.name.clone();
            }
            stats.modules += 1;
            stats.mass += md.mass;
            stats.hp += def.hp;
            if def.category == vf2_core::module::Category::Wheel {
                stats.wheels += 1;
            }
            if def.category == vf2_core::module::Category::Cab {
                stats.has_cab = true;
            }
            for c in &def.components {
                stats.components.push(format!("{c:?}"));
            }
            for nb in asm.graph.neighbors(m) {
                if seen.insert(nb) {
                    stack.push(nb);
                }
            }
        }
    }
    // 应力档位（超载渐进反馈——绿黄橙红）
    if let Some(r) = root {
        stats.stress = vf2_core::baseplay::stress_level(&asm, r) as u8;
    }
    let entity = crate::ui::island::spawn_vehicle_panel(&mut commands, &fonts, &stats);
    panel.open = true;
    panel.entity = Some(entity);
    info!("载具面板打开: {}", stats.name);
}

/// 载具驱动（2026-08-19 用户："先把轮子模块各种东西搞完"——模块是散的
/// 只是组装在一起；WASD 移动整个载具组（含轮子），镜头跟随）：
/// 每次按键移动 1 格（基础版），实体贴地形同步
fn vehicle_drive(
    keys: Res<ButtonInput<KeyCode>>,
    mut asm: ResMut<crate::render_bridge::AsmRes>,
    mut modules: Query<(&crate::render_bridge::ModuleRef, &CellOffset, &mut Transform)>,
    state: Res<InventoryState>,
) {
    if state.search_active || state.cursor_over_ui {
        return;
    }
    let dir = if keys.just_pressed(KeyCode::KeyW) {
        Some(Vec3i(0, 0, -1))
    } else if keys.just_pressed(KeyCode::KeyS) {
        Some(Vec3i(0, 0, 1))
    } else if keys.just_pressed(KeyCode::KeyA) {
        Some(Vec3i(-1, 0, 0))
    } else if keys.just_pressed(KeyCode::KeyD) {
        Some(Vec3i(1, 0, 0))
    } else {
        None
    };
    let Some(dir) = dir else { return };
    let Some(root) = asm.0.root else { return };
    // 收集 root 连通组（BFS）——只同步被移动的组（2026-08-22 防穿模：
    // 旧实现更新**所有**模块实体，落地碎片/断裂体在下次按键时被吸回
    // 原位——碎片穿模/瞬移）
    let mut group = std::collections::HashSet::new();
    let mut stack = vec![root];
    while let Some(m) = stack.pop() {
        if group.insert(m) {
            for nb in asm.0.graph.neighbors(m) {
                stack.push(nb);
            }
        }
    }
    if asm.0.move_group(root, dir).is_err() {
        return;
    }
    // 同步实体位置（逐格——2026-08-21 P1-1 修复：不能把模块中心写进
    // 所有逐格实体（多格模块塌缩）；按 CellOffset 每格算位置）
    // 2026-08-22 防穿模：模块刚性基高（max 地形 over footprint）——
    // 驱动越坡时多格模块不再一格高一格低断裂。
    for (mref, cell, mut tf) in modules.iter_mut() {
        if !group.contains(&mref.0) {
            continue; // 非移动组（落地碎片）不动
        }
        let Some(md) = asm.0.modules.get(mref.0) else { continue };
        let rot = &rotations_24()[(md.rotation % 24) as usize];
        let base = crate::render_bridge::module_render_base(&asm.0, mref.0);
        let wc = rot.apply_to_coord(cell.0) + md.origin;
        let wx = wc.0 as f32 + 0.5;
        let wz = wc.2 as f32 + 0.5;
        tf.translation = Vec3::new(
            wx,
            base + wc.1 as f32 + 0.5,
            wz,
        );
        tf.rotation = Quat::IDENTITY;
    }
}

/// 镜头跟随目标（None = 跟随 root/驾驶舱）——2026-08-19 用户：
/// "镜头永远跟随模块，默认跟随驾驶舱；双击右键把镜头移动过去"
#[derive(Resource, Default)]
pub struct CamFollow(pub Option<vf2_core::assembly::ModuleId>);

/// 每帧平滑跟随目标模块（默认 root=驾驶舱）
fn camera_follow_system(
    asm: Res<crate::render_bridge::AsmRes>,
    follow: Res<CamFollow>,
    mut cam: Query<&mut OrbitCamera>,
    time: Res<Time>,
) {
    let Ok(mut orbit) = cam.single_mut() else { return };
    let asm = &asm.0;
    let Some(mid) = follow.0.or(asm.root) else { return };
    let Some(md) = asm.modules.get(mid) else { return };
    let Some(def) = asm.defs.get(&md.def_id) else { return };
    let dims = crate::render_bridge::shape_dims(&def.shape);
    let rot = &rotations_24()[(md.rotation % 24) as usize];
    let tf = crate::render_bridge::module_transform(md.origin, rot, dims);
    // 镜头跟随贴地渲染位置（与实体一致——高压检查修正；2026-08-22 改模块刚性基高）
    let base = crate::render_bridge::module_render_base(asm, mid);
    let follow = Vec3::new(
        tf.translation.x,
        base + md.origin.1 as f32 + (dims[1] as f32) * 0.5,
        tf.translation.z,
    );
    // 平滑插值（保持用户 yaw/pitch/dist 视角习惯）
    let k = (1.0 - (-6.0 * time.delta_secs()).exp()).min(1.0);
    orbit.target = orbit.target.lerp(follow + Vec3::Y * 0.6, k);
}

/// 双击右键：镜头移动到点击的模块（切换跟随目标）
fn camera_double_click_follow(
    mut follow: ResMut<CamFollow>,
    mouse: Res<ButtonInput<MouseButton>>,
    window: Query<&Window>,
    camera: Query<(&Camera, &GlobalTransform), With<Camera3d>>,
    modules: Query<(Entity, &Transform, &crate::render_bridge::ModuleRef)>,
    time: Res<Time>,
    mut last: Local<f32>,
) {
    if !mouse.just_pressed(MouseButton::Right) {
        return;
    }
    let now = time.elapsed_secs();
    let is_double = now - *last <= 0.35;
    *last = now;
    if !is_double {
        return;
    }
    let Ok(window) = window.single() else { return };
    let Ok((cam, cam_tf)) = camera.single() else { return };
    let Some(cursor) = window.cursor_position() else { return };
    let Some(ray) = crate::input::mouse_ray(window, cam, cam_tf, cursor) else {
        return;
    };
    if let Some((mr, _, _)) = crate::input::hit_module(ray, &modules) {
        follow.0 = Some(mr.0);
        info!("镜头跟随模块 {:?}", mr.0);
    }
}

/// 纹理材质缓存（def_id → 材质句柄——每帧不重复生成资产）
#[derive(Resource, Default)]
pub struct TextureCache(pub std::collections::HashMap<String, Handle<StandardMaterial>>);

/// 资产根：仓库根 assets/（crates/app → ../../assets，编译期拼接——跨机可移植，
/// 2026-08-19 修复：此前硬编码 D:/开发/VoxelForge/assets + bevy 默认 crates/app/assets，
/// GLB/音频/字体加载不到——'网格传到游戏'断点）
pub const ASSET_ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../assets");

/// 演示装配（2026-08-19 用户："先把场景里面的所有东西都清除了，我不需要"）——
/// **空场景**：不装配任何模块；物品栏保留最小可玩集（结构块/驾驶舱 + 程序化轮子）
fn demo_assembly() -> Assembly {
    let mut asm = Assembly::new();
    asm.register(def("nexus.struct", Category::Structure, [1, 1, 1]));
    asm.register(def("nexus.long", Category::Structure, [2, 1, 1]));
    // 基础玩法框架资产（MASTER_DESIGN §8/§9/§10——tags 数据驱动，
    // 代码不硬编码：底座承重/围栏高度/货物标签/制作台全部走 tag）
    asm.register(def_tagged(
        "nexus.base",
        Category::Structure,
        [1, 1, 1],
        vec!["base:carry:50".into()],
    ));
    asm.register(def_tagged(
        "nexus.base_long",
        Category::Structure,
        [2, 1, 1],
        vec!["base:carry:100".into()],
    ));
    asm.register(def_tagged(
        "nexus.fence",
        Category::Structure,
        [1, 2, 1],
        vec!["fence:h:1.0".into()],
    ));
    asm.register(def_tagged(
        "nexus.bench",
        Category::Manufacturer,
        [2, 1, 2],
        vec!["bench".into()],
    ));
    asm.register(def_tagged(
        "nexus.log",
        Category::Structure,
        [1, 1, 1],
        vec!["collectible".into(), "raw:wood".into()],
    ));
    asm.register(def_tagged(
        "nexus.ore",
        Category::Structure,
        [1, 1, 1],
        vec!["collectible".into(), "raw:ore".into()],
    ));
    // 奇特驾驶舱（2026-08-19 用户："组装一个奇特的模块先把它命名为驾驶舱"）——
    // 箭头形 Cells：底 3 格一排 + 中间高一层（驾驶位凸起），非方块模块
    asm.register(ModuleDef {
        schema_version: 4,
        id: "nexus.cab".into(),
        name: "驾驶舱".into(),
        corp: "nexus".into(),
        category: Category::Cab,
        mass: 12.0,
        hp: 120,
        shape: Shape::Cells {
            cells: vec![
                Vec3i(0, 0, 0),
                Vec3i(1, 0, 0),
                Vec3i(2, 0, 0),
                Vec3i(1, 1, 0),
            ],
        },
        mount_points: Face::ALL
            .iter()
            .flat_map(|&face| {
                [
                    Vec3i(0, 0, 0),
                    Vec3i(1, 0, 0),
                    Vec3i(2, 0, 0),
                    Vec3i(1, 1, 0),
                ]
                .into_iter()
                .map(move |cell| MountPoint {
                    cell,
                    face,
                    accepts: MountMask::Any,
                    strength: 100.0,
                    layer: 0,
                    offset: [0.0; 3],
                    align: false,
                })
            })
            .collect(),
        components: vec![],
        model_path: String::new(),
        tags: vec![],
    });
    // 程序化轮子（core wheel 生成器——尺寸→部位→贴架面→生成→适配连接点）
    use vf2_core::wheel::{WheelAttach, WheelRole, WheelSpec};
    for (id, d, role, attach) in [
        ("nexus.wheel_small_front", 1, WheelRole::Front, WheelAttach::West),
        ("nexus.wheel_small_front_e", 1, WheelRole::Front, WheelAttach::East),
        ("nexus.wheel_small_rear", 1, WheelRole::Rear, WheelAttach::West),
        ("nexus.wheel_small_rear_e", 1, WheelRole::Rear, WheelAttach::East),
        ("nexus.wheel_big_rear", 2, WheelRole::Rear, WheelAttach::West),
        ("nexus.wheel_big_rear_e", 2, WheelRole::Rear, WheelAttach::East),
    ] {
        let spec = WheelSpec::new(d, 1, role, attach).expect("轮子参数合法");
        asm.register(spec.to_def(id, "nexus", id.strip_prefix("nexus.").unwrap_or(id)));
    }
    // 驾驶舱是 root（2026-08-19 用户："至少有驾驶舱，没有驾驶舱就代表着死亡"）——
    // 场景初始放置一个驾驶舱，玩家在其基础上组装；root 保护不可拿起
    asm.place_root("nexus.cab", Vec3i(0, 0, 0), 0).expect("驾驶舱 root 放置");
    asm
}

fn def_tagged(id: &str, cat: Category, dims: [u32; 3], tags: Vec<String>) -> ModuleDef {
    let mut d = def(id, cat, dims);
    d.tags = tags;
    d
}

fn def(id: &str, cat: Category, dims: [u32; 3]) -> ModuleDef {
    ModuleDef {
        schema_version: 4,
        id: id.into(),
        name: id.into(),
        corp: id.split('.').next().unwrap_or("nexus").into(),
        category: cat,
        mass: 10.0,
        hp: 100,
        shape: Shape::Block { dims },
        mount_points: Face::ALL
            .iter()
            .map(|&face| MountPoint {
                cell: Vec3i(0, 0, 0),
                face,
                accepts: MountMask::Any,
                strength: 100.0,
                layer: 0,
                offset: [0.0; 3],
                align: false,
            })
            .collect(),
        components: vec![],
        model_path: String::new(),
        tags: vec![],
    }
}

fn main() {
    let mut app = App::new();
    app.add_plugins((
        DefaultPlugins.set(bevy::asset::AssetPlugin {
            file_path: ASSET_ROOT.to_string(),
            ..default()
        }),
        InteractPlugin,
    ));
    // 音频系统（P0：mp3 管线——Sfx 预载 + 事件播放）
    app.init_resource::<bevy::ecs::message::Messages<audio::SfxMessage>>();
    app.insert_resource(audio::Sfx::load(&app.world().resource::<AssetServer>().clone()));
    // UI 热调整（MCP ui_tune——ui_styles.json 热重载）
    let theme = ui::theme::UiTheme::from_json(
        &std::fs::read_to_string(format!("{ASSET_ROOT}/ui_styles.json")).unwrap_or_default());
    app.insert_resource(theme);
    app.add_systems(PreStartup, ui::theme::theme_system);
    app.add_systems(Update, audio::play_sfx);
    let asm = demo_assembly();
    eprintln!("装配就绪：{} 个模块（演示载具 + 12 势力库 {} 种）", asm.len(), asm.defs.len());
    app.insert_resource(crate::render_bridge::AsmRes(asm));
    // ---- 全局资源 ----
    app.init_resource::<TextureCache>();
    app.init_resource::<ui::thumbnail::ThumbnailCache>(); // 缩略图全局唯一缓存
    app.init_resource::<InventoryState>(); // 物品栏全局状态
    app.init_resource::<fx::FxAssets>(); // 特效共享网格
    app.init_resource::<input_map::RemoveMode>(); // 拆除模式（X）
    app.init_resource::<input_map::HelpOpen>(); // 帮助面板（F1）
    app.init_resource::<input_map::ScreenshotRequest>(); // 截图请求（F2）
    app.init_resource::<input_map::AutoShot>(); // 启动自动截图（6 秒后截一张）
    // 程序化建模模板库（core 生成规则 + 数据驱动模板）
    let templates = procgen::load_templates(ASSET_ROOT);
    app.insert_resource(templates);
    // ---- 物品栏插件（面板/交互/预览/信息栏系统）----
    app.add_plugins(crate::inventory::InventoryPlugin);
    // ---- Startup ----
    app.add_systems(PreStartup, ui::font::load_fonts); // 字体必须先于 Startup UI 生成
    app.add_systems(
        Startup,
        (
            setup_scene,
            ui::thumbnail::init_thumbnail_renderer,
            ui::minimap::spawn_minimap,
            ui::quest_bar::spawn_quest_bar,
            // 2026-08-19 用户："场景全部删除，包括建筑物我不要"——
            // 移除 spawn_procgen_scene（程序化建筑/场景装饰物不再生成）
        ),
    );
    // ---- Update ----
    app.insert_resource(CamFollow::default());
    app.insert_resource(VehiclePanel::default());
    app.add_systems(
        Update,
        (
            orbit_camera,
            camera_follow_system,
            camera_double_click_follow,
            vehicle_drive,
            vehicle_panel_system,
            camera_reset,
            sync_entities,
            grid_display,
        ),
    );
    // 断裂体重力（拆除/断裂碎片坠落——2026-08-18）
    app.add_systems(Update, physics::gravity_system);
    app.add_systems(
        Update,
        (
            input_map::toggle_help,
            input_map::help_overlay,
            input_map::help_click_close,
            input_map::auto_screenshot,
            input_map::request_screenshot,
            input_map::take_screenshot,
            input_map::toggle_remove_mode,
            input_map::focus_camera,
            input_map::tab_cycle_category,
        ),
    );
    app.add_systems(
        Update,
        (
            ui::animation::animate_ui,
            ui::input::handle_search_input,
            ui::thumbnail::process_thumbnail_queue,
            ui::minimap::minimap_update,
            ui::quest_bar::quest_bar_update,
            ui::keycap::keycap_flash,
            ui::island::animate_island,
        ),
    );
    app.add_systems(
        Update,
        (
            fx::animate_layer_build,
            fx::animate_build_hold,
            fx::animate_build_glow,
            fx::update_mount_hint,
            fx::animate_place_light,
        ),
    );
    app.run();
}

/// 类别 → 材质参数（AAA 材质分层——金属/橡胶/漆面/抛光——单点维护）
fn category_material(cat: vf2_core::module::Category) -> (f32, f32, f32, bevy::prelude::LinearRgba) {
    use vf2_core::module::Category as C;
    match cat {
        C::Structure => (0.10, 0.60, 0.30, LinearRgba::new(0.0, 0.0, 0.0, 1.0)),       // 混凝土
        C::Cab => (0.60, 0.30, 0.90, LinearRgba::new(0.0, 0.0, 0.0, 1.0)),             // 漆面
        C::Wheel => (0.00, 0.85, 0.15, LinearRgba::new(0.0, 0.0, 0.0, 1.0)),           // 橡胶哑光
        C::Engine => (0.85, 0.25, 1.00, LinearRgba::new(0.3, 0.4, 0.9, 1.0)), // 抛光金属+微蓝辉光
        C::Weapon => (0.75, 0.30, 0.95, LinearRgba::new(0.0, 0.0, 0.0, 1.0)),          // 军械金属
        C::FuelTank => (0.50, 0.35, 0.80, LinearRgba::new(0.0, 0.0, 0.0, 1.0)),        // 罐体
        C::Light => (0.20, 0.40, 0.60, LinearRgba::new(1.2, 1.0, 0.7, 1.0)),   // 暖黄发光
        _ => (0.15, 0.45, 0.50, LinearRgba::new(0.0, 0.0, 0.0, 1.0)),
    }
}

fn setup_scene(
    mut commands: Commands,
    mut meshes: ResMut<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
) {
    // === 光照（AAA 光影：rust-engineer 审查 P0）===
    // 主光：斜俯角 55°（顶面受光 + 阴影有方向体积感）+ 暖白太阳色 + 明亮日景照度
    commands.spawn((
        DirectionalLight {
            illuminance: 35_000.0,
            color: Color::srgb(1.0, 0.97, 0.92), // 暖白太阳色
            shadow_depth_bias: 0.015, // 防模块接缝漏光（默认 0.02）
            shadow_normal_bias: 1.4,  // 防阴影漂浮（默认 1.8）
            ..default()
        },
        Transform::from_rotation(Quat::from_euler(
            EulerRot::XYZ,
            (-55f32).to_radians(),
            35f32.to_radians(),
            0.0,
        )),
    ));
    // 补光：冷蓝 8000（主光暖 + 补光冷——阴影侧带冷色调，AAA 标志手法）
    commands.spawn((
        DirectionalLight {
            illuminance: 8_000.0,
            color: Color::srgb(0.72, 0.82, 1.0),
            ..default()
        },
        Transform::from_rotation(Quat::from_euler(
            EulerRot::XYZ,
            (-35f32).to_radians(),
            (-145f32).to_radians(),
            0.0,
        )),
    ));
    // 0.18 起 AmbientLight 是相机组件；全局环境光用 GlobalAmbientLight 资源
    // （修复：旧写法 spawn(AmbientLight) 会因 #[require(Camera)] 白起一个隐藏相机）
    commands.insert_resource(GlobalAmbientLight {
        color: Color::srgb(0.75, 0.75, 0.8), // 暖白环境（光影增强——去蓝）
        brightness: 150.0,                    // 只防死黑（主光 35000 已足够——AAA 对比）
        ..default()
    });
    // 阴影质量提升（光影增强——4096 阴影贴图）
    commands.insert_resource(bevy::light::DirectionalLightShadowMap { size: 4096 });
    // 自由地形（2026-08-19 用户："地形不是模块、不是方块、没有网格约束"——
    // 程序化起伏高度场，不占格不参与连接；沙地区域落沙沉入）
    let (tpos, tidx, tnor, tuv) = terrain::build_mesh(64, 48.0);
    let mut tmesh = bevy::mesh::Mesh::new(
        bevy::mesh::PrimitiveTopology::TriangleList,
        bevy::asset::RenderAssetUsages::RENDER_WORLD,
    );
    tmesh.insert_attribute(Mesh::ATTRIBUTE_POSITION, tpos);
    tmesh.insert_attribute(Mesh::ATTRIBUTE_NORMAL, tnor);
    tmesh.insert_attribute(Mesh::ATTRIBUTE_UV_0, tuv);
    tmesh.insert_indices(bevy::mesh::Indices::U32(tidx));
    // 顶点色（沙区米黄 / 草地灰绿——bevy StandardMaterial 自动读取）
    let tcolors: Vec<[f32; 4]> = (0..64 * 64)
        .map(|i| {
            let x = -24.0 + (i % 64) as f32 * (48.0 / 63.0);
            let z = -24.0 + (i / 64) as f32 * (48.0 / 63.0);
            let c = terrain::surface_color(x, z);
            [c[0], c[1], c[2], 1.0]
        })
        .collect();
    tmesh.insert_attribute(Mesh::ATTRIBUTE_COLOR, tcolors);
    commands.spawn((
        Mesh3d(meshes.add(tmesh)),
        MeshMaterial3d(materials.add(StandardMaterial {
            base_color: Color::WHITE, // 顶点色生效（bevy 0.15+）
            ..default()
        })),
        Transform::default(),
    ));
    // 相机（轨道：右键拖拽旋转 + 滚轮缩放——光标在 UI 上时缩放让位）
    commands.spawn((
        Camera3d::default(),
        bevy::ui::IsDefaultUiCamera,
        // ACES 电影感色调映射（引擎插件评估：bloom 0.18 已移除——tonemapping
        // 是提升光影质感的原生最强选项：对比增强 + 高光自然衰减）
        bevy::core_pipeline::tonemapping::Tonemapping::AcesFitted,
        // AAA 光影（rust-engineer P0-1）：HDR 中间缓冲 + 曝光——
        // 高光不再裁成纯白（emissive/强高光自然滚落）
        
        bevy::camera::Exposure { ev100: 12.0 },
        // 接触阴影（2026-08-19 用户："游戏增强画质…就是搞光影"）——
        // 模块与地形/模块间细节阴影（近距离 AAA 质感）
        bevy::pbr::ContactShadows {
            linear_steps: 24,
            thickness: 0.02,
            length: 1.5,
        },
        // 距离雾（远景大气氛围——模块/地形融入环境）
        bevy::pbr::DistanceFog {
            color: Color::srgb(0.55, 0.58, 0.62),
            falloff: bevy::pbr::FogFalloff::Exponential { density: 0.008 },
            ..default()
        },
        Transform::from_xyz(10.0, 8.0, 10.0).looking_at(Vec3::new(1.0, 0.0, 0.0), Vec3::Y),
        OrbitCamera::default(),
    ));
}

#[derive(Component)]
struct OrbitCamera {
    yaw: f32,
    pitch: f32,
    dist: f32,
    target: Vec3,
}

impl Default for OrbitCamera {
    fn default() -> Self {
        Self {
            yaw: -0.8,
            pitch: 0.5,
            dist: 14.0,
            target: Vec3::new(1.0, 0.0, 0.0),
        }
    }
}

/// C 键：复位视角（搜索框持有焦点时屏蔽）
fn camera_reset(
    keys: Res<ButtonInput<KeyCode>>,
    state: Res<InventoryState>,
    mut q: Query<&mut OrbitCamera>,
) {
    if state.search_active {
        return;
    }
    if keys.just_pressed(KeyCode::KeyC) {
        for mut orbit in &mut q {
            *orbit = OrbitCamera::default();
        }
    }
}

/// 地面网格 + 悬停格高亮（用户 2026-08-18："网格是默认悬停显示的"——
/// 网格常显作拼接参考，鼠标悬停格高亮；光标在 UI 上时不高亮）
/// 模块体积网格：只有鼠标悬浮到模块时才显示该模块的逐格网格
/// （2026-08-19 用户："网格显示是只有在我鼠标悬浮到某一个模块才有网格显示的"；
/// 不悬浮不显示——不再常显大网格）
/// 2026-08-22 用户定案："那个框在游戏上只有悬停或者拿模块才会显示的 仅限于
/// 模块才有"——悬停模块 或 拿起模块（预览位置）时显示整体外框（按最大边缘，
/// 不再逐格 12 条边/格——大模块上千条线卡死）。
/// Bevy 系统参数无法组合——8 个参数为系统签名固有（clippy: too_many_arguments）
#[allow(clippy::too_many_arguments)]
fn grid_display(
    mut gizmos: Gizmos,
    window: Query<&Window>,
    camera: Query<(&Camera, &GlobalTransform), With<Camera3d>>,
    modules: Query<
        (Entity, &Transform, &crate::render_bridge::ModuleRef),
        (Without<crate::input::PickPreview>, Without<crate::inventory::LibPreview>),
    >,
    asm: Res<crate::render_bridge::AsmRes>,
    ui_state: Res<InventoryState>,
    picked: Res<crate::input::Picked>,
    preview_q: Query<&Transform, With<crate::input::PickPreview>>,
) {
    if ui_state.cursor_over_ui {
        return;
    }
    // 拿起中：预览位置显示该模块外框（跟随鼠标——所见即所得）
    if picked.module.is_some() {
        if let Ok(tf) = preview_q.single() {
            let line_c = Color::srgba(0.4, 0.9, 1.0, 0.9);
            // 预览实体 scale=dims（module_transform 语义）——外框 = 旋转感知
            // AABB（与 hit_module 同路径）
            let half = crate::input::rotated_half_extent(tf.rotation, tf.scale);
            let (min, max) = (tf.translation - half, tf.translation + half);
            let (x0, y0, z0) = (min.x, min.y, min.z);
            let (x1, y1, z1) = (max.x, max.y, max.z);
            for (a, b) in [
                (Vec3::new(x0, y0, z0), Vec3::new(x1, y0, z0)),
                (Vec3::new(x1, y0, z0), Vec3::new(x1, y1, z0)),
                (Vec3::new(x1, y1, z0), Vec3::new(x0, y1, z0)),
                (Vec3::new(x0, y1, z0), Vec3::new(x0, y0, z0)),
                (Vec3::new(x0, y0, z1), Vec3::new(x1, y0, z1)),
                (Vec3::new(x1, y0, z1), Vec3::new(x1, y1, z1)),
                (Vec3::new(x1, y1, z1), Vec3::new(x0, y1, z1)),
                (Vec3::new(x0, y1, z1), Vec3::new(x0, y0, z1)),
                (Vec3::new(x0, y0, z0), Vec3::new(x0, y0, z1)),
                (Vec3::new(x1, y0, z0), Vec3::new(x1, y0, z1)),
                (Vec3::new(x1, y1, z0), Vec3::new(x1, y1, z1)),
                (Vec3::new(x0, y1, z0), Vec3::new(x0, y1, z1)),
            ] {
                gizmos.line(a, b, line_c);
            }
        }
        // 不 return：场景拾取时继续画被悬停目标模块外框，
        // 使绿面（连接面，画在目标模块上）与同模块橙框对齐——
        // 旧逻辑 return 后只画手里模块预览框，绿面却画在目标模块，
        // 两个不同模块永不对齐（"绿面太小/不贴合"根因）
    }
    let Ok(window) = window.single() else { return };
    let Ok((cam, cam_tf)) = camera.single() else { return };
    let Some(cursor) = window.cursor_position() else { return };
    let Some(ray) = crate::input::mouse_ray(window, cam, cam_tf, cursor) else {
        return;
    };
    let Some((mr, _, _)) = crate::input::hit_module(ray, &modules) else {
        return; // 悬浮不在模块上：无网格
    };
    let Some(md) = asm.0.modules.get(mr.0) else { return };
    let Some(def) = asm.0.defs.get(&md.def_id) else { return };
    let rot = &rotations_24()[(md.rotation % 24) as usize];
    // 该模块的占用格（世界格坐标，含旋转）
    let cells: Vec<Vec3i> = match &def.shape {
        vf2_core::module::Shape::Block { dims } => {
            let mut v = Vec::with_capacity((dims[0] * dims[1] * dims[2]) as usize);
            for x in 0..dims[0] {
                for y in 0..dims[1] {
                    for z in 0..dims[2] {
                        v.push(rot.apply_to_coord(Vec3i(x as i32, y as i32, z as i32)));
                    }
                }
            }
            v
        }
        vf2_core::module::Shape::Cells { cells } => cells
            .iter()
            .map(|c| rot.apply_to_coord(*c))
            .collect(),
    };
    // 整体外框 12 条边（2026-08-22：按最大边缘，不逐格——防卡）
    let line_c = Color::srgba(1.0, 1.0, 1.0, 0.9);
    let cells_world: Vec<Vec3i> = cells
        .iter()
        .map(|c| Vec3i(c.0 + md.origin.0, c.1 + md.origin.1, c.2 + md.origin.2))
        .collect();
    // 与模块渲染同基高（2026-08-22 防穿模：外框贴模块，不在网格高度漂浮）
    let base = crate::render_bridge::module_render_base(&asm.0, mr.0);
    for (a, b) in crate::input::outline_edges(&cells_world) {
        gizmos.line(a + Vec3::Y * base, b + Vec3::Y * base, line_c);
    }
}

fn orbit_camera(
    mut cam: Query<(&mut Transform, &mut OrbitCamera)>,
    mouse: Res<ButtonInput<MouseButton>>,
    mouse_delta: Res<bevy::input::mouse::AccumulatedMouseMotion>,
    scroll: Res<bevy::input::mouse::AccumulatedMouseScroll>,
    keys: Res<ButtonInput<KeyCode>>,
    time: Res<Time>,
    state: Res<InventoryState>,
) {
    let Ok((mut tf, mut orbit)) = cam.single_mut() else {
        return;
    };
    let dt = time.delta_secs();
    // 右键拖拽旋转
    if mouse.pressed(MouseButton::Right) {
        orbit.yaw -= mouse_delta.delta.x * 0.008;
        orbit.pitch = (orbit.pitch - mouse_delta.delta.y * 0.008).clamp(0.05, 1.5);
    }
    // 滚轮缩放（光标在 UI 面板内时让位给列表滚动——冲突修复）
    if !state.cursor_over_ui {
        orbit.dist = (orbit.dist - scroll.delta.y * 0.5).clamp(3.0, 60.0);
    }
    // WASD 让位给载具驱动（2026-08-19："镜头永远跟随模块"——
    // 模块/载具移动由 vehicle_drive 处理，镜头跟随；不再手动平移）
    // Q/E 视角升降（全键盘覆盖 2026-08-18——Q/E 不再是死键）
    if keys.pressed(KeyCode::KeyQ) {
        orbit.target.y += 6.0 * dt;
    }
    if keys.pressed(KeyCode::KeyE) {
        orbit.target.y -= 6.0 * dt;
    }
    // 应用
    let eye = orbit.target
        + Vec3::new(
            orbit.dist * orbit.pitch.cos() * orbit.yaw.sin(),
            orbit.dist * orbit.pitch.sin(),
            orbit.dist * orbit.pitch.cos() * orbit.yaw.cos(),
        );
    // 2026-08-22 防穿模：镜头不穿地形（眼高低于地形时抬到地形上 0.5m）
    let eye_y = eye.y.max(crate::terrain::height(eye.x, eye.z) + 0.5);
    tf.translation = Vec3::new(eye.x, eye_y, eye.z);
    tf.look_at(orbit.target, Vec3::Y);
}
