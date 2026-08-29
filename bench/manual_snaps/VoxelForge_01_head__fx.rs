//! fx.rs — 放置特效 V5：逐层堆叠生成 + 连接点高亮 + 虚线指引 + 光效
//!
//! ## 逐层生成动画（Layer-by-Layer Build-up）
//! 摒弃"整体旋转出现"。模拟建筑搭建：模块按**世界 Y 层**切片，从底部开始
//! 一层一层向上堆叠显现（每层 Y 向从 0 展开 + ease_out_back 回弹），
//! 顶层到位时辉光涟漪扩散。兼容任意形状——按 `shape.local_cells()` 经
//! 旋转矩阵变换后的世界坐标分组切片，堆叠顺序即物理层序。
//!
//! 实现结构：
//! - 正常模块实体立即生成（带 [`BuildHold`]，scale≈0 等层动画播完恢复）
//! - 每层一个临时 [`LayerBuildFx`] 实体（共享模块材质，Y 展开生长）
//! - 层动画由位置派生微延迟 → 相邻模块拼接时形成"涟漪"波次
//!
//! ## 连接点（物理对齐接口）
//! 拿起模块时：射线命中面 = 物理对接面，在其中心显示脉动光点（严格贴合
//! 命中模块的旋转感知 AABB 面），并从连接点向目标格画虚线指引。
//!
//! 性能：层实体短生命周期（≤1s）；虚线/光点/辉光常驻复用或池化；
//! 每帧只做标量插值，零分配；点光源不开阴影。

use bevy::prelude::*;
use vf2_core::module::Vec3i;

use crate::input::{PickPreview, Picked, hit_module, mouse_ray, rotated_half_extent};
use crate::inventory::{LibPreview, LibraryPick};
use crate::render_bridge::ModuleRef;

// ============================================================================
// 缓动函数
// ============================================================================

/// ease-out cubic：快出慢收（落地位移动画）
pub fn ease_out_cubic(t: f32) -> f32 {
    let t = t.clamp(0.0, 1.0);
    1.0 - (1.0 - t).powi(3)
}

/// ease-out back：轻微过冲回弹（缩放开箱感）
pub fn ease_out_back(t: f32) -> f32 {
    let t = t.clamp(0.0, 1.0);
    const C1: f32 = 1.70158;
    const C3: f32 = C1 + 1.0;
    1.0 + C3 * (t - 1.0).powi(3) + C1 * (t - 1.0).powi(2)
}

// ============================================================================
// 特效共享资产（网格——避免重复 add 的分配与资产泄漏）
// ============================================================================

/// 特效共享网格
#[derive(Resource)]
pub struct FxAssets {
    /// 单位立方体（层实体/辉光壳/虚线点共用，靠 scale 调形）
    pub cube: Handle<Mesh>,
    /// 1×1 整面（连接点渲染——2026-08-22 用户："连接点渲染改成一整面"；
    /// 法向 +Y，渲染时旋转对齐命中面法线）
    pub plane: Handle<Mesh>,
}

impl FromWorld for FxAssets {
    fn from_world(world: &mut World) -> Self {
        let mut meshes = world.resource_mut::<Assets<Mesh>>();
        Self {
            cube: meshes.add(Cuboid::new(1.0, 1.0, 1.0)),
            plane: meshes.add(bevy::math::primitives::Plane3d::new(
                Vec3::Y,
                Vec2::splat(0.5), // half_size 0.5 → 1×1 整面
            )),
        }
    }
}

// ============================================================================
// 逐层生成：BuildHold（正常实体等待）+ LayerBuildFx（层实体生长）
// ============================================================================

/// 正常模块实体的生成等待（层动画播完前保持近零缩放）
#[derive(Component, Debug, Clone)]
pub struct BuildHold {
    pub elapsed: f32,
    /// 总时长（= 所有层动画播完的时刻）
    pub total: f32,
    /// 起始变换（软吸附插值起点——MASTER_DESIGN §5.2 松手 AP 软吸附 0.1s 滑入）
    pub start: Transform,
    /// 目标变换（插值终点，精确归位）
    pub target: Transform,
}

/// 单个 Y 层的生长动画实体
#[derive(Component, Debug, Clone)]
pub struct LayerBuildFx {
    pub elapsed: f32,
    /// 起播延迟（层序 × 步长 + 位置涟漪）
    pub delay: f32,
    /// 单层动画时长
    pub duration: f32,
    /// 层包围盒底部世界 Y（Y 展开的锚点——从底部向上长）
    pub bottom_y: f32,
    /// 层目标变换（中心位置 + 完整缩放）
    pub target: Transform,
}

/// 层动画参数
pub const LAYER_STEP_SECS: f32 = 0.12;
pub const LAYER_GROW_SECS: f32 = 0.34;

/// 按位置派生涟漪微延迟（同层相邻位置错开 → 波纹感；确定性）
pub fn ripple_delay(pos: Vec3) -> f32 {
    ((pos.x + pos.z) * 0.04).rem_euclid(0.15)
}

/// 模块按世界 Y 层切片（兼容任意形状/旋转）。
///
/// 返回每层（世界 Y, 该层世界包围盒 min/max），按 Y 升序（堆叠顺序）。
pub fn slice_layers(origin: Vec3i, rot: &vf2_core::rotation::RotMat, shape: &vf2_core::module::Shape) -> Vec<(i32, Vec3, Vec3)> {
    use std::collections::BTreeMap;
    let mut layers: BTreeMap<i32, (Vec3, Vec3)> = BTreeMap::new();
    for lc in shape.local_cells() {
        let wc = origin + rot.apply_to_coord(lc);
        let lo = Vec3::new(wc.0 as f32, wc.1 as f32, wc.2 as f32);
        let hi = lo + Vec3::ONE;
        layers
            .entry(wc.1)
            .and_modify(|(mn, mx)| {
                *mn = mn.min(lo);
                *mx = mx.max(hi);
            })
            .or_insert((lo, hi));
    }
    layers.into_iter().map(|(y, (mn, mx))| (y, mn, mx)).collect()
}

/// 层动画更新：延迟 → Y 从 0 展开（锚定底部）+ XZ 回弹 → 销毁
pub fn animate_layer_build(
    mut commands: Commands,
    time: Res<Time>,
    mut q: Query<(Entity, &mut LayerBuildFx, &mut Transform)>,
) {
    let dt = time.delta_secs();
    for (e, mut fx, mut tf) in &mut q {
        if fx.delay > 0.0 {
            fx.delay -= dt;
            // 延迟期贴地隐身（从底部 0 高度等待）
            tf.scale = Vec3::new(fx.target.scale.x, 0.001, fx.target.scale.z);
            continue;
        }
        fx.elapsed += dt;
        // 防御：0 时长直接完成（0/0=NaN 会污染变换）
        let p = if fx.duration <= 0.0 {
            1.0
        } else {
            (fx.elapsed / fx.duration).clamp(0.0, 1.0)
        };
        let grow = ease_out_back(p).max(0.001);
        // Y 展开并锚定底部：中心 = 底部 + 高度×grow/2
        let h = fx.target.scale.y;
        tf.scale = Vec3::new(fx.target.scale.x, h * grow, fx.target.scale.z);
        tf.translation = Vec3::new(
            fx.target.translation.x,
            fx.bottom_y + h * grow * 0.5,
            fx.target.translation.z,
        );
        if p >= 1.0 {
            commands.entity(e).despawn();
        }
    }
}

/// 生成等待更新：ease-out 插值从 start 滑入 target（AP 软吸附 0.1s 滑入——
/// MASTER_DESIGN §5.2：拖动中不触发 AP；松手瞬间检测 → 有则软吸附滑入）
pub fn animate_build_hold(
    mut commands: Commands,
    time: Res<Time>,
    mut q: Query<(Entity, &mut BuildHold, &mut Transform)>,
) {
    let dt = time.delta_secs();
    for (e, mut hold, mut tf) in &mut q {
        hold.elapsed += dt;
        if hold.elapsed >= hold.total {
            *tf = hold.target; // 精确归位
            commands.entity(e).remove::<BuildHold>();
        } else {
            let t = (hold.elapsed / hold.total.max(0.001)).clamp(0.0, 1.0);
            let k = 1.0 - (1.0 - t).powi(3); // ease-out cubic
            tf.translation = hold.start.translation.lerp(hold.target.translation, k);
            tf.scale = hold.start.scale.lerp(hold.target.scale, k);
        }
    }
}

// ============================================================================
// BuildGlowFx —— 顶层到位辉光涟漪（放大淡出 = 光影扩散）
// ============================================================================

/// 生成辉光壳组件（顶层到位时生成，放大淡出模拟涟漪扩散）
#[derive(Component)]
pub struct BuildGlowFx {
    pub elapsed: f32,
    pub duration: f32,
    pub mat: Handle<StandardMaterial>,
    /// 基准缩放（模块目标缩放 ×1.04——涟漪以它为底扩散）
    pub base_scale: Vec3,
}

/// 生成辉光涟漪壳（sync_entities 在顶层起播时刻调用）
pub fn spawn_build_glow(
    commands: &mut Commands,
    fx_assets: &FxAssets,
    materials: &mut Assets<StandardMaterial>,
    target: Transform,
) {
    let mat = materials.add(StandardMaterial {
        base_color: Color::srgba(0.92, 0.92, 0.95, 0.4), // 金属白——灰阶设计语
        alpha_mode: bevy::material::AlphaMode::Blend,
        unlit: true,
        emissive: bevy::color::LinearRgba::new(0.7, 0.7, 0.75, 1.0),
        ..default()
    });
    commands.spawn((
        BuildGlowFx {
            elapsed: 0.0,
            duration: 0.55,
            mat: mat.clone(),
            base_scale: target.scale * 1.04,
        },
        Mesh3d(fx_assets.cube.clone()),
        MeshMaterial3d(mat),
        Transform {
            translation: target.translation,
            rotation: target.rotation,
            scale: target.scale * 1.04,
        },
    ));
}

/// 辉光涟漪更新：透明度淡出 + 匀速扩散 → 结束销毁
pub fn animate_build_glow(
    mut commands: Commands,
    time: Res<Time>,
    mut materials: ResMut<Assets<StandardMaterial>>,
    mut q: Query<(Entity, &mut BuildGlowFx, &mut Transform)>,
) {
    let dt = time.delta_secs();
    for (e, mut fx, mut tf) in &mut q {
        fx.elapsed += dt;
        let p = (fx.elapsed / fx.duration).clamp(0.0, 1.0);
        if let Some(mut m) = materials.get_mut(&fx.mat) {
            m.base_color = m.base_color.with_alpha(0.4 * (1.0 - p));
        }
        tf.scale = fx.base_scale * (1.0 + 0.35 * p); // 涟漪扩散
        if p >= 1.0 {
            commands.entity(e).despawn();
        }
    }
}

// ============================================================================
// 连接点高亮 + 虚线指引
// ============================================================================

/// 连接点高亮标记（mat = 自发光材质句柄，脉动直接改材质）
#[derive(Component)]
pub struct MountHint {
    mat: Handle<StandardMaterial>,
}

/// 虚线指引点（index = 距连接点的序号——池化复用）
#[derive(Component)]
pub struct DashDot {
    pub index: usize,
}

/// 虚线点数
pub const DASH_COUNT: usize = 5;

/// hint/dash 命中查询过滤（排除预览与特效实体——查询 disjoint 要求）
type HintMarkersFilter = (
    Without<PickPreview>,
    Without<LibPreview>,
    Without<MountHint>,
    Without<DashDot>,
);

/// 连接点高亮 + 虚线指引：
/// 拿起模块（库/场景）且射线命中已有模块时——
/// 1. 命中面中心显示脉动光点（物理对接面，严格贴合旋转感知 AABB）
/// 2. 从光点沿外移方向画虚线，指向即将拼接的目标格
/// - 无命中/无拿起 → 全部隐藏（常驻复用，不增删实体）
///
/// 模块连接点（用户规则：每格一个——格中心，相对模块中心）。
/// 局部格坐标 = g - (dims-1)/2；tf.translation 是模块中心，
/// 旧实现用 g+0.5 导致连接点偏移半个模块。
/// 当前整面渲染走 `crate::input::module_hit_face`（模块级 AABB，
/// 与 `outline_edges` 同坐标系——绿面严格坐落橙框对应面，2026-08-22
/// 替换 `hit_face_geometry` 以修"绿面只 1 格 vs 橙框整模块"错位）；
/// `hit_face_geometry`（单格 tf 路径）保留为旧契约测试资产。
#[allow(dead_code)]
pub fn connection_points(
    translation: Vec3,
    rotation: Quat,
    dims: [u32; 3],
) -> Vec<Vec3> {
    let center = Vec3::new(
        (dims[0] - 1) as f32 * 0.5,
        (dims[1] - 1) as f32 * 0.5,
        (dims[2] - 1) as f32 * 0.5,
    );
    let mut pts = Vec::with_capacity((dims[0] * dims[1] * dims[2]) as usize);
    for gx in 0..dims[0] {
        for gy in 0..dims[1] {
            for gz in 0..dims[2] {
                let local = Vec3::new(gx as f32, gy as f32, gz as f32) - center;
                pts.push(translation + rotation * local);
            }
        }
    }
    pts
}

/// 命中面几何（2026-08-22 彻查抽纯函数，可测锁定）：
/// 模块 Transform（scale=dims）+ 命中面轴 + 射线方向 →
/// (面中心, 外移法线方向, 局部 scale)。
///
/// - 面中心 = 模块中心 + 外移法线 × 旋转感知 AABB half[ax]
///   （旧 bug：取"指针最近连接点格中心+0.5"——指针偏内格时整面贴模块内部）
/// - 局部 scale = (面宽, 1, 面高)——Plane3d 顶点在局部 XZ 平面（法向 +Y），
///   尺寸必须放 x/z；放 y 只影响无厚度法向轴（旧 bug：非正方形命中面
///   只盖一半）
pub fn hit_face_geometry(tf: &Transform, ax: usize, ray_dir: Vec3) -> (Vec3, Vec3, Vec3) {
    let half = rotated_half_extent(tf.rotation, tf.scale);
    let outward_sign = -ray_dir[ax].signum();
    let axis_vec = match ax {
        0 => Vec3::X,
        1 => Vec3::Y,
        _ => Vec3::Z,
    };
    let outward = axis_vec * outward_sign;
    let face_center = tf.translation + outward * half[ax];
    let (ua, va) = match ax {
        0 => (1, 2),
        1 => (0, 2),
        _ => (0, 1),
    };
    let face_scale = Vec3::new(half[ua] * 2.0, 1.0, half[va] * 2.0);
    (face_center, outward, face_scale)
}

#[allow(clippy::too_many_arguments)] // Bevy 系统参数固有
pub fn update_mount_hint(
    lib_pick: Res<LibraryPick>,
    scene_pick: Res<Picked>,
    time: Res<Time>,
    mut commands: Commands,
    fx_assets: Option<Res<FxAssets>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
    window: Query<&Window>,
    camera: Query<(&Camera, &GlobalTransform), With<Camera3d>>,
    markers: Query<(Entity, &Transform, &ModuleRef), HintMarkersFilter>,
    asm: Res<crate::render_bridge::AsmRes>,
    mut hint_q: Query<(&mut Transform, &mut Visibility, &MountHint)>,
    mut dash_q: Query<(&DashDot, &mut Transform, &mut Visibility), Without<MountHint>>,
) {
    let active = lib_pick.0.is_some() || scene_pick.module.is_some();
    // 计算连接点（每格一个——用户规则）+ 外移方向；指针优先瞄准最近连接点
    let target = (|| {
        if !active {
            return None;
        }
        let window = window.single().ok()?;
        let (cam, cam_tf) = camera.single().ok()?;
        let cursor = window.cursor_position()?;
        let ray = mouse_ray(window, cam, cam_tf, cursor)?;
        let (mr, _, _) = hit_module(ray, &markers)?;
        // 模块级 AABB 面（与 outline_edges 同坐标系——绿面严格坐落在橙框
        // 对应面上；2026-08-22 修"绿面只 1 格 vs 橙框整模块"错位）
        let md = asm.0.modules.get(mr.0)?;
        if md.cells.is_empty() {
            return None;
        }
        let base = crate::render_bridge::module_render_base(&asm.0, mr.0);
        let (face_center, outward, face_size) =
            crate::input::module_hit_face(&md.cells, base, ray.0, ray.1)?;
        Some((face_center, outward, face_size))
    })();

    match target {
        Some((face_center, outward, face_size)) => {
            // --- 连接点整面（2026-08-22 用户："连接点渲染改成一整面"）---
            // 覆盖整个命中面（与 Blender 端连接面标记同语义），
            // 法向 = 命中面外移方向；外移 0.05 防与模块表面 z-fight（深度缓冲
            // 在斜视/小平面下精度不足；0.02 仍会丢角，0.05 在远距离也安全）
            let face_pos = face_center + outward * 0.05;
            let face_rot = Quat::from_rotation_arc(Vec3::Y, outward);
            if let Ok((mut tf, mut vis, hint)) = hint_q.single_mut() {
                tf.translation = face_pos;
                tf.rotation = face_rot;
                tf.scale = face_size;
                if *vis != Visibility::Inherited {
                    *vis = Visibility::Inherited;
                }
                pulse_hint(&time, &mut materials, hint);
            } else {
                // 首次需要时生成（懒加载——无拿起不建实体）
                let Some(fx_assets) = fx_assets else { return };
                let mat = materials.add(StandardMaterial {
                    base_color: Color::srgba(0.95, 0.95, 0.98, 0.6),
                    alpha_mode: bevy::material::AlphaMode::Blend,
                    unlit: true,
                    emissive: bevy::color::LinearRgba::new(0.8, 0.8, 0.85, 1.0),
                    ..default()
                });
                commands.spawn((
                    MountHint { mat: mat.clone() },
                    Mesh3d(fx_assets.plane.clone()),
                    MeshMaterial3d(mat),
                    Transform::from_translation(face_pos)
                        .with_rotation(face_rot)
                        .with_scale(face_size),
                ));
                // 虚线点池（与连接点同材质色系，更小更暗）
                for i in 0..DASH_COUNT {
                    let dmat = materials.add(StandardMaterial {
                        base_color: Color::srgba(0.9, 0.9, 0.93, 0.45),
                        alpha_mode: bevy::material::AlphaMode::Blend,
                        unlit: true,
                        ..default()
                    });
                    commands.spawn((
                        DashDot { index: i },
                        Mesh3d(fx_assets.cube.clone()),
                        MeshMaterial3d(dmat),
                        Transform::from_translation(face_center)
                            .with_scale(Vec3::splat(0.07)),
                        Visibility::Hidden,
                    ));
                }
            }
            // --- 虚线指引：整面 → 目标格（沿外移方向等距排布）---
            for (dot, mut tf, mut vis) in &mut dash_q {
                // 起点从整面中心外推 0.08（衔接整面边缘，不重叠）
                tf.translation = face_center + outward * (0.08 + dot.index as f32 * 0.2);
                if *vis != Visibility::Inherited {
                    *vis = Visibility::Inherited;
                }
            }
        }
        None => {
            for (_, mut vis, _) in &mut hint_q {
                if *vis != Visibility::Hidden {
                    *vis = Visibility::Hidden;
                }
            }
            for (_, _, mut vis) in &mut dash_q {
                if *vis != Visibility::Hidden {
                    *vis = Visibility::Hidden;
                }
            }
        }
    }
}

/// 脉动：透明度随时间正弦起伏（呼吸感）
fn pulse_hint(time: &Time, materials: &mut Assets<StandardMaterial>, hint: &MountHint) {
    let pulse = 0.45 + 0.30 * (time.elapsed_secs() * 4.0).sin(); // 终审 P2-14：6→4rad/s（0.96Hz 呼吸感）
    if let Some(mut m) = materials.get_mut(&hint.mat) {
        m.base_color = m.base_color.with_alpha(pulse.clamp(0.0, 1.0));
    }
}

// ============================================================================
// PlaceLightFx —— 放置成功点光闪光（暖白，灰阶语）
// ============================================================================

/// 点光闪光组件（放置成功时生成，快速衰减后销毁）
#[derive(Component)]
pub struct PlaceLightFx {
    pub elapsed: f32,
    pub duration: f32,
    pub from_intensity: f32,
}

/// 在指定位置生成一次放置闪光（inventory/input_systems 的放置成功路径调用）
pub fn spawn_place_light(commands: &mut Commands, pos: Vec3) {
    commands.spawn((
        PlaceLightFx {
            elapsed: 0.0,
            duration: 0.5,
            from_intensity: 150_000.0, // 动效审查 P1-3：26000 在 14m 处仅 133 lux 被淹没
        },
        PointLight {
            intensity: 150_000.0,
            range: 12.0,
            color: Color::srgb(0.95, 0.94, 0.9), // 暖白
            ..default()
        },
        Transform::from_translation(pos + Vec3::Y * 1.2),
    ));
}

/// 闪光衰减：强度线性归零 → 销毁
pub fn animate_place_light(
    mut commands: Commands,
    time: Res<Time>,
    mut q: Query<(Entity, &mut PlaceLightFx, &mut PointLight)>,
) {
    let dt = time.delta_secs();
    for (e, mut fx, mut light) in &mut q {
        fx.elapsed += dt;
        let p = (fx.elapsed / fx.duration).clamp(0.0, 1.0);
        light.intensity = fx.from_intensity * (1.0 - p);
        if p >= 1.0 {
            commands.entity(e).despawn();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use vf2_core::module::Shape;
    use vf2_core::rotation::rotations_24;

    #[test]
    fn test_ease_functions_endpoints() {
        assert!((ease_out_cubic(0.0) - 0.0).abs() < 1e-6);
        assert!((ease_out_cubic(1.0) - 1.0).abs() < 1e-6);
        assert!((ease_out_back(0.0) - 0.0).abs() < 1e-6);
        assert!((ease_out_back(1.0) - 1.0).abs() < 1e-6);
        // back 缓动中途过冲（>1）
        assert!(ease_out_back(0.7) > 1.0, "ease_out_back 应有回弹过冲");
    }

    #[test]
    fn test_ripple_delay_deterministic_and_bounded() {
        let d1 = ripple_delay(Vec3::new(1.0, 0.0, 2.0));
        let d2 = ripple_delay(Vec3::new(1.0, 0.0, 2.0));
        assert_eq!(d1, d2, "同位置延迟应确定");
        assert!((0.0..0.15).contains(&d1), "延迟应有界");
    }

    /// 分层切片：2×2×1 模块 → 2 层（y=0, y=1），层包围盒正确
    #[test]
    fn test_slice_layers_groups_by_world_y() {
        let shape = Shape::Block { dims: [1, 2, 1] };
        let rot = &rotations_24()[0];
        let layers = slice_layers(Vec3i(3, 0, 5), rot, &shape);
        assert_eq!(layers.len(), 2, "两层");
        assert_eq!(layers[0].0, 0, "底层在前（堆叠顺序）");
        assert_eq!(layers[1].0, 1);
        // 底层包围盒 [3,0,5]→[4,1,6]
        assert_eq!(layers[0].1, Vec3::new(3.0, 0.0, 5.0));
        assert_eq!(layers[0].2, Vec3::new(4.0, 1.0, 6.0));
    }

    /// 分层切片：旋转 90° 的 2×1 长块仍是 1 层（层序 = 物理层序）
    #[test]
    fn test_slice_layers_rotated_single_layer() {
        let shape = Shape::Block { dims: [2, 1, 1] };
        let rot = &rotations_24()[0];
        let layers = slice_layers(Vec3i(0, 0, 0), rot, &shape);
        assert_eq!(layers.len(), 1, "平躺长块只有一层");
        assert_eq!(layers[0].2, Vec3::new(2.0, 1.0, 1.0));
    }

    /// 层动画完整生命周期：延迟 → Y 展开锚底 → 销毁
    #[test]
    fn test_layer_build_lifecycle() {
        let mut app = App::new();
        app.add_plugins(MinimalPlugins);
        app.add_systems(Update, animate_layer_build);
        let target = Transform::from_translation(Vec3::new(1.5, 1.0, 2.5))
            .with_scale(Vec3::new(2.0, 1.0, 1.0));
        let e = app
            .world_mut()
            .spawn((
                Transform::from_translation(Vec3::new(1.5, 0.5, 2.5))
                    .with_scale(Vec3::new(2.0, 0.001, 1.0)),
                LayerBuildFx {
                    elapsed: 0.0,
                    delay: 0.0,
                    duration: 0.0, // 0 时长 → 单帧完成（确定性测试）
                    bottom_y: 0.5,
                    target,
                },
            ))
            .id();
        app.update();
        // 结束即销毁（层实体是临时的）
        assert!(app.world().get_entity(e).is_err(), "层动画结束应销毁实体");
    }

    /// BuildHold：到时恢复正常缩放并移除组件
    #[test]
    fn test_build_hold_restores_transform() {
        let mut app = App::new();
        app.add_plugins(MinimalPlugins);
        app.add_systems(Update, animate_build_hold);
        let target = Transform::from_translation(Vec3::new(1.0, 0.5, 2.0))
            .with_scale(Vec3::new(2.0, 1.0, 1.0));
        let e = app
            .world_mut()
            .spawn((
                target.with_scale(Vec3::splat(0.001)),
                BuildHold {
                    elapsed: 0.0,
                    total: 0.0,
                    start: target.with_scale(Vec3::splat(0.001)),
                    target,
                },
            ))
            .id();
        app.update();
        let tf = app.world().entity(e).get::<Transform>().unwrap();
        assert_eq!(tf.translation, target.translation, "结束应精确归位");
        assert_eq!(tf.scale, target.scale);
        assert!(app.world().entity(e).get::<BuildHold>().is_none());
    }
}

/// 连接点纯函数测试（用户规则：每格一个 + 坐标中心化）
#[cfg(test)]
mod connection_point_tests {
    use super::*;

    #[test]
    fn test_connection_points_single_cell_centered() {
        // 单格模块：连接点 = 模块中心（旧 bug：偏移 (dims-1)/2 = 0.5 格）
        let pts = connection_points(Vec3::new(2.0, 0.5, 0.5), Quat::IDENTITY, [1, 1, 1]);
        assert_eq!(pts.len(), 1, "单格 1 个连接点");
        let d = (pts[0] - Vec3::new(2.0, 0.5, 0.5)).length();
        assert!(d < 1e-4, "单格连接点应在模块中心，偏差 {d}");
    }

    #[test]
    fn test_connection_points_two_cells_centered() {
        // 2×1×1 模块：2 个连接点，±0.5 格居中（旧 bug：整体偏移 0.5 格）
        let pts = connection_points(Vec3::new(2.0, 0.5, 0.5), Quat::IDENTITY, [2, 1, 1]);
        assert_eq!(pts.len(), 2, "2 格 2 个连接点");
        let mut xs: Vec<f32> = pts.iter().map(|p| p.x - 2.0).collect();
        xs.sort_by(|a, b| a.total_cmp(b));
        assert!((xs[0] - (-0.5)).abs() < 1e-4, "左格中心应 -0.5，实得 {}", xs[0]);
        assert!((xs[1] - 0.5).abs() < 1e-4, "右格中心应 +0.5，实得 {}", xs[1]);
    }

    #[test]
    fn test_connection_points_rotated() {
        // 旋转 90°（绕 Y）：2×1×1 的连接点应沿 Z 分布（X→Z）
        let q = Quat::from_rotation_y(90f32.to_radians());
        let pts = connection_points(Vec3::new(2.0, 0.5, 0.5), q, [2, 1, 1]);
        assert_eq!(pts.len(), 2);
        let mut zs: Vec<f32> = pts.iter().map(|p| p.z - 0.5).collect();
        zs.sort_by(|a, b| a.total_cmp(b));
        assert!((zs[0] - (-0.5)).abs() < 1e-4, "旋转后沿 Z：-0.5，实得 {}", zs[0]);
        assert!((zs[1] - 0.5).abs() < 1e-4, "旋转后沿 Z：+0.5，实得 {}", zs[1]);
        // 旋转后 X 不变（都在模块中心 X）
        for p in &pts {
            assert!((p.x - 2.0).abs() < 1e-4, "旋转后 X 保持中心");
        }
    }

    #[test]
    fn test_connection_points_four_cells() {
        // 4×1×1：4 个连接点，-1.5/-0.5/+0.5/+1.5（用户语义：4 格 4 点）
        let pts = connection_points(Vec3::ZERO, Quat::IDENTITY, [4, 1, 1]);
        assert_eq!(pts.len(), 4);
        let mut xs: Vec<f32> = pts.iter().map(|p| p.x).collect();
        xs.sort_by(|a, b| a.total_cmp(b));
        for (i, expect) in [-1.5, -0.5, 0.5, 1.5].iter().enumerate() {
            assert!((xs[i] - expect).abs() < 1e-4, "第 {i} 点应 {expect}，实得 {}", xs[i]);
        }
    }

    /// 命中面几何（2026-08-22 彻查锁定）：
    /// 1. 面中心必须在命中面上（不在模块内部）——旧 bug：指针偏内格时整面贴内部
    /// 2. 局部 scale 尺寸必须放 x/z（Plane3d 顶点在局部 XZ 平面）——旧 bug：
    ///    非正方形命中面只盖一半（尺寸放 y 只影响无厚度法向轴）
    #[test]
    fn test_hit_face_geometry_center_and_scale() {
        // 2×2×1 模块（scale=[2,2,1]），未旋转，中心在原点。
        // 命中 +X 面（ax=0，射线 -X 方向）：
        // half = (1,1,0.5)；面中心 = (1,0,0)；法向 = +X；面尺寸 = (2,1,2)
        let tf = Transform::from_scale(Vec3::new(2.0, 2.0, 1.0));
        let (center, outward, scale) = hit_face_geometry(&tf, 0, -Vec3::X);
        assert!((center - Vec3::new(1.0, 0.0, 0.0)).length() < 1e-4,
            "命中面中心应在模块表面 (1,0,0)，实得 {center}");
        assert_eq!(outward, Vec3::X, "外移法线 = 命中面法线 +X");
        assert!((scale - Vec3::new(2.0, 1.0, 1.0)).length() < 1e-4,
            "2×2×1 命中 +X 面（YZ 平面 2×1）局部 scale 应 (2,1,1)，实得 {scale}——尺寸在 x/z 轴");
        // 反向射线 → 命中 -X 面
        let (c2, o2, _) = hit_face_geometry(&tf, 0, Vec3::X);
        assert!((c2 - Vec3::new(-1.0, 0.0, 0.0)).length() < 1e-4, "反向命中 -X 面");
        assert_eq!(o2, -Vec3::X);
        // Top 面（ax=1）：2×2×1 模块顶面 = x×z = 2×1；scale=(2,1,1)
        let (c3, o3, s3) = hit_face_geometry(&tf, 1, -Vec3::Y);
        assert!((c3 - Vec3::new(0.0, 1.0, 0.0)).length() < 1e-4, "Top 面中心 (0,1,0)");
        assert_eq!(o3, Vec3::Y);
        assert!((s3 - Vec3::new(2.0, 1.0, 1.0)).length() < 1e-4,
            "Top 面 2×1：scale=(2,1,1)，实得 {s3}");
        // 旋转 90°（绕 Y）：2×2×1 模块旋转后半宽 x=0.5、z=1——+X 面中心
        // x=+0.5（旋转感知 AABB）
        let q = Quat::from_rotation_y(std::f32::consts::FRAC_PI_2);
        let tf_rot = Transform::from_rotation(q).with_scale(Vec3::new(2.0, 2.0, 1.0));
        let (c4, _, s4) = hit_face_geometry(&tf_rot, 0, -Vec3::X);
        assert!((c4 - Vec3::new(0.5, 0.0, 0.0)).length() < 1e-3,
            "90° 后 +X 面中心 x=0.5（AABB 半宽），实得 {c4}");
        assert!((s4 - Vec3::new(2.0, 1.0, 2.0)).length() < 1e-3,
            "90° 后 +X 面尺寸 = (2,1,2)——面仍覆盖整个命中面，实得 {s4}");
    }
}
