//! 交互层：拿起/放置/旋转/预览（VoxelForge 2.0 核心玩法）。
//!
//! 状态机：
//!   Idle ──左键点模块──► Picked（记录原位置 origin/rot——防丢模块）
//!   Picked ──左键（有效格）──► Placed（Assembly::place 成功）
//!   Picked ──右键/Esc──► 回原位（Assembly::place 回 origin）
//!   Picked ──R──► 旋转预览（绕 Y +90°，与 core 旋转表语义一致）
//!
//! 铁律（1.x 教训）：**任何放置失败都不丢模块**——要么保持拿起（预览
//! 跟随），要么回原位。拿起/放置全程走 core API（不变量测试已锁）。

use bevy::prelude::*;
use vf2_core::assembly::Assembly;
use vf2_core::module::Vec3i;
use vf2_core::rotation::rotations_24;

use crate::render_bridge::ModuleRef;

/// 拿起状态（None = 空闲）
#[derive(Resource, Default)]
pub struct Picked {
    pub module: Option<PickedModule>,
}

/// 拿起中的模块（含原位置——回位/防丢）
#[derive(Debug, Clone)]
pub struct PickedModule {
    pub def_id: String,
    pub origin: Vec3i,
    pub rotation: u8,
    /// 拿起的是否底盘（root）——回位时用 place_root（空装配语义）
    pub is_root: bool,
}

/// 预览实体标记（拿起时 spawn 半透明跟随，放下时 despawn）
#[derive(Component)]
pub struct PickPreview;

/// 鼠标 → 世界射线（纯数学投影逆——不依赖 viewport，headless 测试与
/// 实机同一路径；主相机全窗口，与 viewport_to_world 等价）
pub fn mouse_ray(
    window: &Window,
    camera: &Camera,
    cam_tf: &GlobalTransform,
    cursor: Vec2,
) -> Option<(Vec3, Vec3)> {
    // 窗口尺寸为 0（最小化/未布局）时投影无意义——防御性提前返回
    if window.width() <= 0.0 || window.height() <= 0.0 {
        return None;
    }
    let ndc = Vec2::new(
        (cursor.x / window.width()) * 2.0 - 1.0,
        -((cursor.y / window.height()) * 2.0 - 1.0),
    );
    let view = cam_tf.to_matrix().inverse();
    let clip_from_view = camera.clip_from_view();
    let inv = (clip_from_view * view).inverse();
    // Bevy 默认 ReverseZ：NDC z 近=1、远=0。**远平面（z=0）逆变换后
    // w=0（无穷远点）**——用它构造射线必然失败（实机"左键点不了"根因）。
    // 取视锥内的 z=0.5 解第二点（同一射线，w 非零，方向不变）。
    let near = inv * ndc.extend(1.0).extend(1.0);
    let far = inv * ndc.extend(0.5).extend(1.0);
    // 奇异矩阵（scale=0 相机等）产生 NaN/Inf——任何分量非有限即拒绝，
    // 否则 NaN 射线会在 AABB 命中中误判（tmin>tmax 对 NaN 恒 false）
    if !near.is_finite() || !far.is_finite() || near.w.abs() < 1e-9 || far.w.abs() < 1e-9 {
        return None;
    }
    let a = near.truncate() / near.w;
    let b = far.truncate() / far.w;
    let dir = b - a;
    if dir.length_squared() < 1e-12 {
        return None;
    }
    Some((a, dir.normalize()))
}

/// 射线 vs AABB（slab 法，纯函数可测）
pub fn ray_hits_aabb(origin: Vec3, dir: Vec3, min: Vec3, max: Vec3) -> Option<f32> {
    let mut tmin = f32::NEG_INFINITY;
    let mut tmax = f32::INFINITY;
    for i in 0..3 {
        let (o, d) = (origin[i], dir[i]);
        if d.abs() < 1e-9 {
            if o < min[i] || o > max[i] {
                return None;
            }
        } else {
            let t1 = (min[i] - o) / d;
            let t2 = (max[i] - o) / d;
            let (a, b) = if t1 < t2 { (t1, t2) } else { (t2, t1) };
            tmin = tmin.max(a);
            tmax = tmax.min(b);
            if tmin > tmax {
                return None;
            }
        }
    }
    (tmax >= 0.0).then_some(tmin.max(0.0))
}

/// 旋转感知 AABB 半宽：Σ |R·轴half|（纯函数可测）
pub fn rotated_half_extent(rot: Quat, scale: Vec3) -> Vec3 {
    (rot * Vec3::new(scale.x / 2.0, 0.0, 0.0)).abs()
        + (rot * Vec3::new(0.0, scale.y / 2.0, 0.0)).abs()
        + (rot * Vec3::new(0.0, 0.0, scale.z / 2.0)).abs()
}

/// 命中最近模块（遍历模块实体——模块数少，简单可靠；返回 (module_id, 距离)）
/// 模块是根级实体（Transform == 全局坐标）。
/// **旋转感知 AABB**：half-extent = Σ |R·轴half|（scale=dims 不随旋转交换，
/// 但轴对齐盒必须随实体 Quat 旋转——否则 90° 长块命中盒错 90°）
pub fn hit_module(
    ray: (Vec3, Vec3),
    q: &Query<(Entity, &Transform, &ModuleRef), Without<PickPreview>>,
) -> Option<(ModuleRef, f32)> {
    let mut best: Option<(ModuleRef, f32)> = None;
    for (_, tf, mr) in q.iter() {
        let half = rotated_half_extent(tf.rotation, tf.scale);
        if let Some(t) = ray_hits_aabb(ray.0, ray.1, tf.translation - half, tf.translation + half)
            && best.is_none_or(|(_, b)| t < b)
        {
            best = Some((*mr, t));
        }
    }
    best
}

/// 射线 vs 地面（y=0）：命中点
pub fn hit_ground(ray: (Vec3, Vec3)) -> Option<Vec3> {
    let (o, d) = ray;
    if d.y.abs() < 1e-9 {
        return None;
    }
    let t = -o.y / d.y;
    (t >= 0.0).then_some(o + d * t)
}

/// 放置目标格：命中模块 → 该模块相邻外侧格（面法线方向）；
/// 未命中模块 → 地面命中格（向上取整到层）。
/// 返回 `(目标格, 外移方向)`——外移方向用于 Overlap 时的候选格回退
/// （见 [`resolve_placement_cell`]，预览与放置共用）。
pub fn placement_target(
    ray: (Vec3, Vec3),
    asm: &Assembly,
    hit: Option<ModuleRef>,
) -> Option<(Vec3i, Vec3i)> {
    if let Some(mr) = hit {
        let md = asm.modules.get(mr.0)?;
        // 目标格 = 命中模块的占用格中沿射线方向最外侧 + 1
        let dir = ray.1;
        let ax = if dir.x.abs() >= dir.y.abs() && dir.x.abs() >= dir.z.abs() {
            0
        } else if dir.y.abs() >= dir.z.abs() {
            1
        } else {
            2
        };
        let mut outer = md.cells[0];
        // 命中面法线 = 射线来向（取反）——取该方向最外格
        let outward = -[dir.x, dir.y, dir.z][ax];
        for c in &md.cells {
            let v = [c.0, c.1, c.2];
            let ov = [outer.0, outer.1, outer.2];
            if outward > 0.0 && v[ax] > ov[ax] {
                outer = *c;
            }
            if outward < 0.0 && v[ax] < ov[ax] {
                outer = *c;
            }
        }
        let mut out = outer;
        // 命中面法线 = 射线来向（dir 取反）——射线沿 -X 命中 +X 面 → 目标 +1
        let outward = -[dir.x, dir.y, dir.z][ax];
        let outward_dir = match ax {
            0 => Vec3i(if outward > 0.0 { 1 } else { -1 }, 0, 0),
            1 => Vec3i(0, if outward > 0.0 { 1 } else { -1 }, 0),
            _ => Vec3i(0, 0, if outward > 0.0 { 1 } else { -1 }),
        };
        match ax {
            0 => out.0 += if outward > 0.0 { 1 } else { -1 },
            1 => out.1 += if outward > 0.0 { 1 } else { -1 },
            _ => out.2 += if outward > 0.0 { 1 } else { -1 },
        }
        Some((out, outward_dir))
    } else {
        // 地面命中 → 格（y=0 层：放地上；无重叠问题，外移方向取 -Y 占位）
        let p = hit_ground(ray)?;
        Some((Vec3i(p.x.floor() as i32, 0, p.z.floor() as i32), Vec3i(0, -1, 0)))
    }
}

/// 放置候选格解析：目标格与装配占用格重叠（Overlap）时，沿外移方向逐格外移
/// 重试（最多 8 格），找到首个不重叠的格。**预览跟随与点击放置共用此函数**——
/// 所见即所得（预览吸附格 == 实际放置格）。
/// 无重叠 / 超限时返回原目标格（放置时失败保持拿起，防丢铁律不变）。
pub fn resolve_placement_cell(
    asm: &Assembly,
    def_id: &str,
    rot: u8,
    cell: Vec3i,
    outward: Vec3i,
) -> Vec3i {
    let Some(def) = asm.defs.get(def_id) else {
        return cell;
    };
    let m = rotations_24()[rot as usize];
    // 装配占用集（模块数少，构建集合开销可忽略）
    let occ: std::collections::HashSet<Vec3i> = asm
        .modules
        .values()
        .flat_map(|md| md.cells.iter().copied())
        .collect();
    let locals = def.shape.local_cells();
    let mut c = cell;
    for _ in 0..8 {
        let free = locals
            .iter()
            .all(|&lc| !occ.contains(&(c + m.apply_to_coord(lc))));
        if free {
            return c;
        }
        c = c + outward;
    }
    cell
}

#[cfg(test)]
mod tests {
    use super::*;
    use bevy::window::WindowResolution;
    use vf2_core::module::{Category, Face, ModuleDef, MountMask, MountPoint, Shape};

    fn def() -> ModuleDef {
        ModuleDef {
            schema_version: 4,
            id: "nexus.b".into(),
            name: "b".into(),
            corp: "nexus".into(),
            category: Category::Structure,
            mass: 10.0,
            hp: 100,
            shape: Shape::Block { dims: [1, 1, 1] },
            mount_points: Face::ALL
                .iter()
                .map(|&f| MountPoint {
                    cell: Vec3i(0, 0, 0),
                    face: f,
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

    /// 防御路径锁定：零尺寸窗口 / 奇异相机矩阵（scale=0）→ mouse_ray 返回 None
    /// （回归 security-review LOW：NaN 射线穿透会在 AABB 命中中误判）
    #[test]
    fn test_mouse_ray_rejects_zero_window_and_singular() {
        let cam = Camera::default();
        // 零尺寸窗口 → None（投影无意义）
        let win0 = Window {
            resolution: WindowResolution::new(0, 0),
            ..default()
        };
        assert!(
            mouse_ray(&win0, &cam, &GlobalTransform::IDENTITY, Vec2::new(0.0, 0.0)).is_none(),
            "零尺寸窗口必须返回 None"
        );
        // 奇异矩阵（scale=0 相机 → 逆变换 NaN）→ None（is_finite 拒绝）
        let win = Window {
            resolution: WindowResolution::new(1280, 720),
            ..default()
        };
        let cam_tf = GlobalTransform::from(Transform::from_scale(Vec3::ZERO));
        assert!(
            mouse_ray(&win, &cam, &cam_tf, Vec2::new(640.0, 360.0)).is_none(),
            "奇异相机矩阵必须返回 None（NaN 射线拒绝）"
        );
    }

    #[test]
    fn test_ray_hits_aabb() {        let hit = ray_hits_aabb(Vec3::new(0.0, 3.0, 0.0), Vec3::NEG_Y, Vec3::new(-0.5, -0.5, -0.5), Vec3::new(0.5, 0.5, 0.5));
        assert!(hit.is_some());
        assert!((hit.unwrap() - 2.5).abs() < 1e-4);
        let miss = ray_hits_aabb(Vec3::new(5.0, 3.0, 0.0), Vec3::NEG_Y, Vec3::new(-0.5, -0.5, -0.5), Vec3::new(0.5, 0.5, 0.5));
        assert!(miss.is_none());
        // 背向
        let behind = ray_hits_aabb(Vec3::new(0.0, 3.0, 0.0), Vec3::Y, Vec3::new(-0.5, -0.5, -0.5), Vec3::new(0.5, 0.5, 0.5));
        assert!(behind.is_none(), "背向射线 tmax<0 不命中");
    }

    #[test]
    fn test_placement_target_ground() {
        let asm = Assembly::new();
        let ray = (Vec3::new(5.0, 8.0, 5.0), Vec3::new(0.0, -1.0, 0.0));
        let (cell, _out) = placement_target(ray, &asm, None).expect("地面命中");
        assert_eq!(cell, Vec3i(5, 0, 5), "地面放置目标 = 射线格");
    }

    #[test]
    fn test_placement_target_neighbor_cell() {
        // 装配：root 在 (0,0,0)。命中该模块（+X 面方向）→ 目标 (1,0,0)
        let mut asm = Assembly::new();
        asm.register(def());
        asm.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
        let mid = asm.root.unwrap();
        let mr = ModuleRef(mid);
        // 从 +X 方向射向模块中心 → 命中面 = +X → 目标 (1,0,0)，外移 +X
        let ray = (Vec3::new(3.0, 0.5, 0.5), Vec3::NEG_X);
        let (cell, out_dir) = placement_target(ray, &asm, Some(mr)).expect("命中模块");
        assert_eq!(cell, Vec3i(1, 0, 0), "+X 方向放置目标 = 相邻格");
        assert_eq!(out_dir, Vec3i(1, 0, 0), "外移方向 = 命中面法线（远离模块）");
    }

    /// Overlap 候选格回退：长块 [2,1,1] 目标格 (-1,0,0) 会占 (0,0,0)（与 root 重叠）
    /// → 沿 -X 外移一格到 (-2,0,0)（占 (-2,0,0),(-1,0,0)，不重叠）
    #[test]
    fn test_resolve_placement_overlap_outward() {
        let mut asm = Assembly::new();
        asm.register(def());
        asm.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
        // 长块 def（[2,1,1]）
        let mut long = def();
        long.id = "nexus.long".into();
        long.shape = Shape::Block { dims: [2, 1, 1] };
        asm.register(long);
        // 目标 (-1,0,0)：长块占 (-1,0,0),(0,0,0)——(0,0,0) 重叠 root
        let cell = resolve_placement_cell(&asm, "nexus.long", 0, Vec3i(-1, 0, 0), Vec3i(-1, 0, 0));
        assert_eq!(cell, Vec3i(-2, 0, 0), "Overlap 应沿外移方向回退到不重叠格");
        // 放置该格应成功且连接
        asm.place("nexus.long", cell, 0).unwrap();
        assert_eq!(asm.len(), 2, "回退格放置成功");
    }

    /// 无重叠时 resolve 保持原目标格不变
    #[test]
    fn test_resolve_placement_no_overlap_unchanged() {
        let mut asm = Assembly::new();
        asm.register(def());
        asm.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
        let cell = resolve_placement_cell(&asm, "nexus.b", 0, Vec3i(1, 0, 0), Vec3i(1, 0, 0));
        assert_eq!(cell, Vec3i(1, 0, 0), "无重叠时目标格不变");
    }
}

#[cfg(test)]
mod hit_tests {
    use super::*;
    use bevy::math::Quat;

    /// 旋转感知 AABB：2×1 长块（scale [2,1,1]）旋转 90°（绕 Y，X→-Z）
    /// → 轴对齐 half [0.5, 0.5, 1.0]（x 方向变窄、z 方向变长）
    #[test]
    fn test_rotated_half_extent_90deg() {
        let q = Quat::from_rotation_y(std::f32::consts::FRAC_PI_2);
        let half = rotated_half_extent(q, Vec3::new(2.0, 1.0, 1.0));
        assert!((half.x - 0.5).abs() < 1e-4, "90° x half = {}", half.x);
        assert!((half.y - 0.5).abs() < 1e-4);
        assert!((half.z - 1.0).abs() < 1e-4, "90° z half = {}", half.z);
        // 未旋转：half = [1, 0.5, 0.5]
        let h0 = rotated_half_extent(Quat::IDENTITY, Vec3::new(2.0, 1.0, 1.0));
        assert!((h0.x - 1.0).abs() < 1e-4 && (h0.z - 0.5).abs() < 1e-4);
        // 180°：同未旋转
        let q180 = Quat::from_rotation_y(std::f32::consts::PI);
        let h180 = rotated_half_extent(q180, Vec3::new(2.0, 1.0, 1.0));
        assert!((h180.x - 1.0).abs() < 1e-4 && (h180.z - 0.5).abs() < 1e-4);
    }

    /// 旋转感知 AABB 的命中验证：旋转 90° 的 2×1 长块，
    /// 中心 (0,0,0)、half [0.5,0.5,1]——(0.4,0,0) 命中（|x|<0.5），
    /// (0.9,0,0) 不命中（>0.5），(0,0,1.4) 不命中（>1.0）
    #[test]
    fn test_rotated_aabb_ray_hits() {
        let q = Quat::from_rotation_y(std::f32::consts::FRAC_PI_2);
        let half = rotated_half_extent(q, Vec3::new(2.0, 1.0, 1.0));
        let min = -half;
        let max = half;
        // (0.4, 0, 0) → 命中（|x|=0.4 < 0.5）
        let hit = ray_hits_aabb(Vec3::new(0.4, 5.0, 0.0), Vec3::NEG_Y, min, max);
        assert!(hit.is_some(), "盒内点应命中");
        // (0.9, 0, 0) → 不命中（旋转后 x 半宽=0.5）
        let miss = ray_hits_aabb(Vec3::new(0.9, 5.0, 0.0), Vec3::NEG_Y, min, max);
        assert!(miss.is_none(), "旋转后 x 半宽=0.5，(0.9,0,0) 应不命中");
        // (0, 0, 1.4) → 不命中（旋转后 z 半宽=1——1.4 > 1）
        let miss_z = ray_hits_aabb(Vec3::new(0.0, 5.0, 1.4), Vec3::NEG_Y, min, max);
        assert!(miss_z.is_none(), "旋转后 z 半宽=1，(0,0,1.4) 应不命中");
    }
}
