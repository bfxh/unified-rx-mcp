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
mod ui;

use bevy::prelude::*;
use vf2_core::assembly::Assembly;
use vf2_core::module::{Category, Face, ModuleDef, MountMask, MountPoint, Shape, Vec3i};
use vf2_core::mount_rules;
use vf2_core::rotation::rotations_24;

use crate::input_systems::InteractPlugin;
use crate::render_bridge::{ModuleRef, module_transform};
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
    fx_assets: Option<Res<fx::FxAssets>>,
    q: Query<(Entity, &ModuleRef), Without<fx::LayerBuildFx>>,
) {
    let asm = &asm.0;
    let cube = meshes.add(Cuboid::new(0.95, 0.95, 0.95));
    let mut seen = std::collections::HashSet::new();
    for (mid, md) in asm.modules.iter() {
        seen.insert(mid);
        if q.iter().any(|(_, r)| r.0 == mid) {
            continue;
        }
        let Some(def) = asm.defs.get(&md.def_id) else { continue };
        let dims = render_bridge::shape_dims(&def.shape);
        let rot = &rotations_24()[md.rotation as usize];
        let tf = module_transform(md.origin, rot, dims);
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
        });

        // === 逐层堆叠生成动画 ===
        // 层切片：按世界 Y 分组（兼容任意形状/旋转——堆叠顺序 = 物理层序）
        let layers = fx::slice_layers(md.origin, rot, &def.shape);
        let layer_count = layers.len();
        let ripple = fx::ripple_delay(tf.translation);
        for (i, (_, lmin, lmax)) in layers.iter().enumerate() {
            let lsize = *lmax - *lmin;
            let center = (*lmin + *lmax) * 0.5;
            let delay = ripple + i as f32 * fx::LAYER_STEP_SECS;
            let target = Transform {
                translation: center,
                rotation: Quat::IDENTITY,
                scale: lsize * 0.95, // 与模块外观一致的 0.95 缩缝
            };
            commands.spawn((
                fx::LayerBuildFx {
                    elapsed: 0.0,
                    delay,
                    duration: fx::LAYER_GROW_SECS,
                    bottom_y: lmin.y,
                    target,
                },
                Mesh3d(cube.clone()),
                MeshMaterial3d(material.clone()),
                // 初始：贴底零高（从底部向上长）
                Transform {
                    translation: Vec3::new(center.x, lmin.y, center.z),
                    rotation: Quat::IDENTITY,
                    scale: Vec3::new(target.scale.x, 0.001, target.scale.z),
                },
            ));
            // 顶层起播时辉光涟漪扩散
            if i == layer_count - 1 && let Some(fx_assets) = fx_assets.as_ref() {
                fx::spawn_build_glow(&mut commands, fx_assets, &mut materials, tf);
            }
        }
        // 正常实体：近零缩放等待层动画播完
        let total = ripple + layer_count as f32 * fx::LAYER_STEP_SECS + fx::LAYER_GROW_SECS;
        let mut hold_tf = tf;
        hold_tf.scale = tf.scale * 0.001;
        commands.spawn((
            ModuleRef(mid),
            Mesh3d(cube.clone()),
            MeshMaterial3d(material.clone()),
            hold_tf,
            fx::BuildHold {
                elapsed: 0.0,
                total,
                target: tf,
            },
        ));
    }
    // despawn 多余实体（已从装配移除）
    for (e, r) in q.iter() {
        if !seen.contains(&r.0) {
            commands.entity(e).despawn();
        }
    }
}

/// 纹理材质缓存（def_id → 材质句柄——每帧不重复生成资产）
#[derive(Resource, Default)]
pub struct TextureCache(pub std::collections::HashMap<String, Handle<StandardMaterial>>);

/// 资产根：仓库根 assets/（crates/app → ../../assets，编译期拼接——跨机可移植，
/// 2026-08-19 修复：此前硬编码 D:/开发/VoxelForge/assets + bevy 默认 crates/app/assets，
/// GLB/音频/字体加载不到——'网格传到游戏'断点）
pub const ASSET_ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../assets");

/// 演示装配（从真实 RON 模块库加载 12 势力模块 + 拼一个小载具）
fn demo_assembly() -> Assembly {
    let mut asm = Assembly::new();
    // 内置结构块/轮子/驾驶舱（快速可玩，不依赖资产路径）
    asm.register(def("nexus.struct", Category::Structure, [1, 1, 1]));
    asm.register(def("nexus.long", Category::Structure, [2, 1, 1]));
    asm.register(def("nexus.cab", Category::Cab, [1, 1, 1]));
    asm.register(def("nexus.wheel", Category::Wheel, [1, 1, 1]));
    // 真实资产加载（失败不影响演示）
    let dir = std::path::Path::new(ASSET_ROOT).join("modules/rebuild");
    if let Ok(rd) = std::fs::read_dir(dir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().is_some_and(|x| x == "ron")
                && let Ok(t) = std::fs::read_to_string(&p)
                && let Ok(d) = ModuleDef::from_ron(&t)
            {
                // 连接点设计性规则校验（2026-08-18）：不合规打警告不静默
                if let Err(rules_err) = mount_rules::check_module(&d) {
                    eprintln!("[mount_rules] {} 不合规: {rules_err}", p.display());
                }
                asm.register(d);
            }
        }
    }
    // 拼演示载具：cab + 2×1 长块 + 轮子
    asm.place_root("nexus.cab", Vec3i(0, 0, 0), 0).unwrap();
    asm.place("nexus.long", Vec3i(1, 0, 0), 0).unwrap();
    asm.place("nexus.wheel", Vec3i(0, -1, 0), 0).unwrap();
    asm.place("nexus.wheel", Vec3i(1, -1, 0), 0).unwrap();
    asm.place("nexus.wheel", Vec3i(2, -1, 0), 0).unwrap();
    asm.place("nexus.wheel", Vec3i(3, -1, 0), 0).unwrap();
    asm
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
            procgen::spawn_procgen_scene, // 程序化场景布局（模板变体）
        ),
    );
    // ---- Update ----
    app.add_systems(Update, (orbit_camera, camera_reset, sync_entities, grid_display));
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
            shadows_enabled: true,
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
            shadows_enabled: false,
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
    // 地面网格（1 格 = 1 米）
    commands.spawn((
        Mesh3d(meshes.add(Plane3d::default().mesh().size(24.0, 24.0))),
        MeshMaterial3d(materials.add(StandardMaterial {
            base_color: Color::srgb(0.22, 0.22, 0.24), // 灰阶地面（V5 去蓝）
            ..default()
        })),
        Transform::from_xyz(4.0, 0.0, 4.0),
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
        bevy::render::view::Hdr,
        bevy::camera::Exposure { ev100: 12.0 },
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
fn grid_display(
    mut gizmos: Gizmos,
    window: Query<&Window>,
    camera: Query<(&Camera, &GlobalTransform), With<Camera3d>>,
    ui_state: Res<InventoryState>,
) {
    // 3D 网格常显（±12 格 × 1 米 × 高 4 层——体素语义，模块可堆叠；
    // 用户 2026-08-19："网格是平面的 我要的是3D啊"；与 Blender 端同规格）
    // 颜色=白色（用户："网格需要显示颜色 白色"）
    let half: f32 = 12.0;
    let top: f32 = 4.0;
    let y = 0.012;
    let line_c = Color::srgba(1.0, 1.0, 1.0, 0.5);
    // x 方向水平线（每条 z 线 × 每 y 层）
    for zi in -12..=12 {
        let z = zi as f32;
        for yi in 0..=4 {
            let yy = yi as f32;
            gizmos.line(Vec3::new(-half, yy, z), Vec3::new(half, yy, z), line_c);
        }
    }
    // z 方向水平线（每条 x 线 × 每 y 层）
    for xi in -12..=12 {
        let x = xi as f32;
        for yi in 0..=4 {
            let yy = yi as f32;
            gizmos.line(Vec3::new(x, yy, -half), Vec3::new(x, yy, half), line_c);
        }
    }
    // y 方向竖线（每个 xz 格点，y 0 → 顶）
    for xi in -12..=12 {
        let x = xi as f32;
        for zi in -12..=12 {
            let z = zi as f32;
            gizmos.line(Vec3::new(x, y, z), Vec3::new(x, top, z), line_c);
        }
    }
    // 悬停格高亮（射线打 y=0 地面）
    if ui_state.cursor_over_ui {
        return;
    }
    let Ok(window) = window.single() else { return };
    let Ok((cam, cam_tf)) = camera.single() else { return };
    let Some(cursor) = window.cursor_position() else { return };
    let Some((origin, dir)) = crate::input::mouse_ray(window, cam, cam_tf, cursor) else {
        return;
    };
    if dir.y.abs() < 1e-6 {
        return;
    }
    let t = -origin.y / dir.y;
    if t <= 0.0 {
        return;
    }
    let hit = origin + dir * t;
    let p = Vec3::new(hit.x.floor() + 0.5, y + 0.004, hit.z.floor() + 0.5);
    let c = Color::srgba(0.55, 0.92, 1.0, 0.95);
    let h = 0.5;
    gizmos.line(p + Vec3::new(-h, 0.0, -h), p + Vec3::new(h, 0.0, -h), c);
    gizmos.line(p + Vec3::new(h, 0.0, -h), p + Vec3::new(h, 0.0, h), c);
    gizmos.line(p + Vec3::new(h, 0.0, h), p + Vec3::new(-h, 0.0, h), c);
    gizmos.line(p + Vec3::new(-h, 0.0, h), p + Vec3::new(-h, 0.0, -h), c);
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
    // WASD 平移目标（搜索框持有焦点时屏蔽——打字不平移相机）
    let fwd = Vec3::new(-orbit.yaw.sin(), 0.0, -orbit.yaw.cos()).normalize_or_zero();
    let right = Vec3::new(fwd.z, 0.0, -fwd.x);
    let speed = if state.search_active { 0.0 } else { 6.0 * dt };
    if keys.pressed(KeyCode::KeyW) {
        orbit.target += fwd * speed;
    }
    if keys.pressed(KeyCode::KeyS) {
        orbit.target -= fwd * speed;
    }
    if keys.pressed(KeyCode::KeyA) {
        orbit.target -= right * speed;
    }
    if keys.pressed(KeyCode::KeyD) {
        orbit.target += right * speed;
    }
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
    tf.translation = eye;
    tf.look_at(orbit.target, Vec3::Y);
}
