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

/// 射线 vs AABB（slab 法，纯函数可测）。
/// 返回 `(t, 入口面轴)`——t 为最近命中距离，axis 为 **tmin 由哪个轴的 slab
/// 贡献**（精确命中面——斜射/多格模块也成立；debugging-wizard P1/P2 根治）。
pub fn ray_hits_aabb(origin: Vec3, dir: Vec3, min: Vec3, max: Vec3) -> Option<(f32, usize)> {
    let mut tmin = f32::NEG_INFINITY;
    let mut tmax = f32::INFINITY;
    let mut entry_axis = 0usize;
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
            if a > tmin {
                tmin = a;
                entry_axis = i;
            }
            tmax = tmax.min(b);
            if tmin > tmax {
                return None;
            }
        }
    }
    (tmax >= 0.0).then_some((tmin.max(0.0), entry_axis))
}

/// 旋转感知 AABB 半宽：Σ |R·轴half|（纯函数可测）
pub fn rotated_half_extent(rot: Quat, scale: Vec3) -> Vec3 {
    (rot * Vec3::new(scale.x / 2.0, 0.0, 0.0)).abs()
        + (rot * Vec3::new(0.0, scale.y / 2.0, 0.0)).abs()
        + (rot * Vec3::new(0.0, 0.0, scale.z / 2.0)).abs()
}

/// 占用格集合 → 整体外框 12 条边（按最大边缘，2026-08-22 用户定案：
/// "那个框框是按最大的边缘来算的；不要给我搞这么多条缝 或者线太卡了"——
/// 大模块逐格 12 条边/格会画上千条线，改一个外框 12 条线）。
/// 与 Blender 端 occ_outline_edges 同语义（min 格 → max+1）。
/// 返回 [(a, b); 12]——每对为一条边（世界格坐标，origin 已含）。
pub fn outline_edges(cells: &[Vec3i]) -> [(Vec3, Vec3); 12] {
    debug_assert!(!cells.is_empty());
    let (mut min, mut max) = (cells[0], cells[0]);
    for c in cells.iter().skip(1) {
        min.0 = min.0.min(c.0);
        min.1 = min.1.min(c.1);
        min.2 = min.2.min(c.2);
        max.0 = max.0.max(c.0);
        max.1 = max.1.max(c.1);
        max.2 = max.2.max(c.2);
    }
    let (x0, y0, z0) = (min.0 as f32, min.1 as f32, min.2 as f32);
    let (x1, y1, z1) = (max.0 as f32 + 1.0, max.1 as f32 + 1.0, max.2 as f32 + 1.0);
    let v = |x: f32, y: f32, z: f32| Vec3::new(x, y, z);
    [
        (v(x0, y0, z0), v(x1, y0, z0)),
        (v(x1, y0, z0), v(x1, y1, z0)),
        (v(x1, y1, z0), v(x0, y1, z0)),
        (v(x0, y1, z0), v(x0, y0, z0)),
        (v(x0, y0, z1), v(x1, y0, z1)),
        (v(x1, y0, z1), v(x1, y1, z1)),
        (v(x1, y1, z1), v(x0, y1, z1)),
        (v(x0, y1, z1), v(x0, y0, z1)),
        (v(x0, y0, z0), v(x0, y0, z1)),
        (v(x1, y0, z0), v(x1, y0, z1)),
        (v(x1, y1, z0), v(x1, y1, z1)),
        (v(x0, y1, z0), v(x0, y1, z1)),
    ]
}

/// 模块命中面几何（**模块级 AABB** → 面中心/外法线/局部 scale）。
///
/// **为什么从模块级 AABB 算**：sync_entities 把模块渲染成**逐格 cube 实体**
///（scale=0.95、含 base）。旧 `hit_face_geometry` 从命中单格的 `tf`（scale=0.95）
/// 算半宽 → 绿面只有 1 格，与橙框（`outline_edges` 按整模块外接 AABB）对不齐。
/// 本函数用 `cells_world`（已是 world 整数格）+ `base` 算模块 AABB——
/// **与 `outline_edges` 同坐标系**，绿面严格坐落在橙框对应面上。
///
/// **入口面轴从整模块 AABB 用 `ray_hits_aabb` 反算**（不再传命中单格 cube 的
/// `ax`）——多格模块边角/斜视时，单格 cube 的入口面轴可能与整模块入口面轴
/// 不一致（绿面落错模块面），整模块 AABB 才是正确的"指针指向的那一面"。
///
/// - face_center = AABB 中心 + 外向 × AABB half[ax]
/// - face_scale   = (AABB half[ua]*2, 1, AABB half[va]*2)
/// - 外向 = `-ray_dir[ax].signum()`：ray 沿 +x 进入 -X 面时外向=-X，朝射线源
pub fn module_hit_face(
    cells_world: &[Vec3i],
    base: f32,
    ray_origin: Vec3,
    ray_dir: Vec3,
) -> Option<(Vec3, Vec3, Vec3)> {
    debug_assert!(!cells_world.is_empty(), "cells_world must be non-empty");
    let (mut lo, mut hi) = (
        Vec3i(i32::MAX, i32::MAX, i32::MAX),
        Vec3i(i32::MIN, i32::MIN, i32::MIN),
    );
    for c in cells_world {
        lo.0 = lo.0.min(c.0);
        lo.1 = lo.1.min(c.1);
        lo.2 = lo.2.min(c.2);
        hi.0 = hi.0.max(c.0);
        hi.1 = hi.1.max(c.1);
        hi.2 = hi.2.max(c.2);
    }
    // AABB in world：cell 是 1×1×1（hi 侧 +1），y 含 base
    let aabb_min = Vec3::new(lo.0 as f32, base + lo.1 as f32, lo.2 as f32);
    let aabb_max = Vec3::new(
        (hi.0 + 1) as f32,
        base + (hi.1 + 1) as f32,
        (hi.2 + 1) as f32,
    );
    // 入口面轴从整模块 AABB 反算（指针指向的那一面；射线不命中模块 → None）
    let (_, ax) = ray_hits_aabb(ray_origin, ray_dir, aabb_min, aabb_max)?;
    let center = (aabb_min + aabb_max) * 0.5;
    let half = (aabb_max - aabb_min) * 0.5;
    let outward_sign = -ray_dir[ax].signum();
    let outward = match ax {
        0 => Vec3::new(outward_sign, 0.0, 0.0),
        1 => Vec3::new(0.0, outward_sign, 0.0),
        _ => Vec3::new(0.0, 0.0, outward_sign),
    };
    let face_center = center + outward * half[ax];
    let (ua, va) = match ax {
        0 => (1, 2),
        1 => (0, 2),
        _ => (0, 1),
    };
    let face_scale = Vec3::new(half[ua] * 2.0, 1.0, half[va] * 2.0);
    Some((face_center, outward, face_scale))
}

/// 命中最近模块（遍历模块实体——模块数少，简单可靠；返回 (module_id, 距离, 入口面轴)）
/// 模块是根级实体（Transform == 全局坐标）。
/// **旋转感知 AABB**：half-extent = Σ |R·轴half|（scale=dims 不随旋转交换，
/// 但轴对齐盒必须随实体 Quat 旋转——否则 90° 长块命中盒错 90°）
pub fn hit_module<F: bevy::ecs::query::QueryFilter>(
    ray: (Vec3, Vec3),
    q: &Query<(Entity, &Transform, &ModuleRef), F>,
) -> Option<(ModuleRef, f32, usize)> {
    let mut best: Option<(ModuleRef, f32, usize)> = None;
    for (_, tf, mr) in q.iter() {
        // debugging-wizard P1：BuildHold 动画期 scale≈0.001 → 命中盒≈0（放置后
        // 0.5s 内不可命中）——用稳定下限 0.95（动画期也可命中拼接）
        let stable = tf.scale.max(Vec3::splat(0.95));
        let half = rotated_half_extent(tf.rotation, stable);
        if let Some((t, ax)) = ray_hits_aabb(ray.0, ray.1, tf.translation - half, tf.translation + half)
            && best.is_none_or(|(_, b, _)| t < b)
        {
            best = Some((*mr, t, ax));
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

/// 放置目标格：命中模块 → 该模块相邻外侧格（**AABB 入口面轴精确判定**——斜射也准）；
/// 未命中模块 → 地面命中格（向上取整到层）。
/// 返回 `(目标格, 外移方向)`——外移方向用于 Overlap 时的候选格回退
/// （见 [`resolve_placement_cell`]，预览与放置共用）。
/// `hit` = (命中模块, 射线参数 t, 入口面轴)——t/axis 来自 hit_module 的 AABB 精确命中。
pub fn placement_target(
    ray: (Vec3, Vec3),
    asm: &Assembly,
    hit: Option<(ModuleRef, f32, usize)>,
) -> Option<(Vec3i, Vec3i)> {
    if let Some((mr, t, ax)) = hit {
        // 防御（debugging-wizard P3/P4）：t 必须有限且非负，否则无有效命中面
        if !t.is_finite() || t < 0.0 {
            return None;
        }
        let md = asm.modules.get(mr.0)?;
        if md.cells.is_empty() {
            return None; // 防御（P6）
        }
        // 命中面 = AABB 入口面轴；外移方向 = 射线来向（-dir[ax] 符号）
        let dir = ray.1;
        let outward = -[dir.x, dir.y, dir.z][ax];
        // 目标格 = 该方向最外侧格 + 1（远离模块）
        let mut outer = md.cells[0];
        for cell in &md.cells {
            let v = [cell.0, cell.1, cell.2];
            let ov = [outer.0, outer.1, outer.2];
            if outward > 0.0 && v[ax] > ov[ax] {
                outer = *cell;
            }
            if outward < 0.0 && v[ax] < ov[ax] {
                outer = *cell;
            }
        }
        let mut out = outer;
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
        // 地面命中 → 格（y=0 层：放地上）。外移方向取水平 +Z——地面格重叠
        // 时回退沿水平方向找空位（若取 -Y 会把候选格推入地下 y<0）
        let p = hit_ground(ray)?;
        Some((
            Vec3i(p.x.floor() as i32, 0, p.z.floor() as i32),
            Vec3i(0, 0, 1),
        ))
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
    if rot >= 24 {
        // 防御：非法旋转直接返回原格（place_inner 会以 InvalidRotation 拒绝）
        return cell;
    }
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
        assert!((hit.unwrap().0 - 2.5).abs() < 1e-4);
        let miss = ray_hits_aabb(Vec3::new(5.0, 3.0, 0.0), Vec3::NEG_Y, Vec3::new(-0.5, -0.5, -0.5), Vec3::new(0.5, 0.5, 0.5));
        assert!(miss.is_none());
        // 背向
        let behind = ray_hits_aabb(Vec3::new(0.0, 3.0, 0.0), Vec3::Y, Vec3::new(-0.5, -0.5, -0.5), Vec3::new(0.5, 0.5, 0.5));
        assert!(behind.is_none(), "背向射线 tmax<0 不命中");
    }

    /// 入口面轴精确性（debugging-wizard P1/P2 回归锁）：2x1x1 长块 AABB
    /// [0,2]×[0,1]×[0,1]——三个正交入射都返回正确入口面轴（斜射/多格也准）
    #[test]
    fn test_ray_hits_aabb_entry_axis() {
        let min = Vec3::new(0.0, 0.0, 0.0);
        let max = Vec3::new(2.0, 1.0, 1.0);
        // 场景 A：从 -X 正射（偏上命中 x=0 面）→ 入口轴 0
        let (t, ax) = ray_hits_aabb(Vec3::new(-1.0, 0.8, 0.5), Vec3::X, min, max).expect("命中");
        assert_eq!(ax, 0, "从 -X 正射应命中 X 轴面");
        assert!((t - 1.0).abs() < 1e-4, "t 应为 1.0（x=0 面）");
        // 场景 B：从上方正射（命中 y=1 面）→ 入口轴 1
        let (_, ax) = ray_hits_aabb(Vec3::new(1.6, 2.0, 0.5), Vec3::NEG_Y, min, max).expect("命中");
        assert_eq!(ax, 1, "从上方正射应命中 Y 轴面");
        // 场景 C：从 +X 正射（命中 x=2 面）→ 入口轴 0
        let (_, ax) = ray_hits_aabb(Vec3::new(3.0, 0.5, 0.5), Vec3::NEG_X, min, max).expect("命中");
        assert_eq!(ax, 0, "从 +X 正射应命中 X 轴面");
        // 场景 D：从 +Z 正射（命中 z=1 面）→ 入口轴 2
        let (_, ax) = ray_hits_aabb(Vec3::new(1.0, 0.5, 2.0), Vec3::NEG_Z, min, max).expect("命中");
        assert_eq!(ax, 2, "从 +Z 正射应命中 Z 轴面");
    }

    /// 多格模块斜射放置目标（debugging-wizard 场景 A）：2x1 长块 -X 面偏上命中
    /// → 目标格应为其左侧 (-1,0,0)（而非头顶——旧实现错判）
    #[test]
    fn test_placement_target_multicell_side_face() {
        let mut asm = Assembly::new();
        let mut long = def();
        long.id = "nexus.long".into();
        long.shape = Shape::Block { dims: [2, 1, 1] };
        asm.register(long);
        asm.place_root("nexus.long", Vec3i(0, 0, 0), 0).unwrap();
        let mid = asm.root.unwrap();
        // 从 -X 正射（射线 +X），入口轴 0（x=0 面），t=1.0
        let ray = (Vec3::new(-1.0, 0.8, 0.5), Vec3::X);
        let (cell, out) =
            placement_target(ray, &asm, Some((ModuleRef(mid), 1.0, 0))).expect("命中");
        assert_eq!(cell, Vec3i(-1, 0, 0), "-X 面应放置到左侧（旧实现错判为头顶）");
        assert_eq!(out, Vec3i(-1, 0, 0), "外移方向应远离模块");
        // 防御：NaN t 应返回 None（不静默错判）
        assert!(
            placement_target(ray, &asm, Some((ModuleRef(mid), f32::NAN, 0))).is_none(),
            "NaN t 应拒绝"
        );
        assert!(
            placement_target(ray, &asm, Some((ModuleRef(mid), -1.0, 0))).is_none(),
            "负 t 应拒绝"
        );
    }

    #[test]
    fn test_placement_target_ground() {
        let asm = Assembly::new();
        let ray = (Vec3::new(5.0, 8.0, 5.0), Vec3::new(0.0, -1.0, 0.0));
        let (cell, out_dir) = placement_target(ray, &asm, None).expect("地面命中");
        assert_eq!(cell, Vec3i(5, 0, 5), "地面放置目标 = 射线格");
        assert_eq!(out_dir, Vec3i(0, 0, 1), "地面外移方向 = 水平 +Z（不埋地）");
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
        let (cell, out_dir) = placement_target(ray, &asm, Some((mr, 2.5, 0))).expect("命中模块");
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

    /// 非法旋转（rot>=24）防御：返回原格（place_inner 会以 InvalidRotation 拒绝）
    #[test]
    fn test_resolve_placement_invalid_rot_defensive() {
        let mut asm = Assembly::new();
        asm.register(def());
        asm.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
        let cell = resolve_placement_cell(&asm, "nexus.b", 24, Vec3i(1, 0, 0), Vec3i(1, 0, 0));
        assert_eq!(cell, Vec3i(1, 0, 0), "rot>=24 防御：返回原格不 panic");
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

    /// 占用格外框（2026-08-22 用户定案：按最大边缘一个外框，不逐格）：
    /// 单格 → [0,1]³ 12 条边；2×1×1 → x∈[0,2]；L 形 → 最大边缘；
    /// 偏移起点 → 外框跟随 min（负坐标/偏移）
    #[test]
    fn test_outline_edges_max_extent() {
        // 单格 (0,0,0) → 12 条边，范围 [0,1]³
        let e = outline_edges(&[Vec3i(0, 0, 0)]);
        assert_eq!(e.len(), 12, "外框恒 12 条边");
        let mut min = Vec3::splat(f32::MAX);
        let mut max = Vec3::splat(f32::MIN);
        for (a, b) in &e {
            for p in [a, b] {
                min = min.min(*p);
                max = max.max(*p);
            }
        }
        assert!((min - Vec3::ZERO).length() < 1e-4, "min={min}");
        assert!((max - Vec3::ONE).length() < 1e-4, "max={max}");
        // 2×1×1（(0,0,0),(1,0,0)）→ x∈[0,2]（不随格数增加边数）
        let e2 = outline_edges(&[Vec3i(0, 0, 0), Vec3i(1, 0, 0)]);
        assert_eq!(e2.len(), 12, "2 格外框仍 12 条边");
        let mut max2 = Vec3::splat(f32::MIN);
        for (a, b) in &e2 {
            for p in [a, b] {
                max2 = max2.max(*p);
            }
        }
        assert!((max2 - Vec3::new(2.0, 1.0, 1.0)).length() < 1e-4, "max2={max2}");
        // L 形（(0,0,0),(1,0,0),(0,0,1)）→ 最大边缘 [0,2]×[0,1]×[0,2]
        let eL = outline_edges(&[Vec3i(0, 0, 0), Vec3i(1, 0, 0), Vec3i(0, 0, 1)]);
        let mut maxL = Vec3::splat(f32::MIN);
        let mut minL = Vec3::splat(f32::MAX);
        for (a, b) in &eL {
            for p in [a, b] {
                minL = minL.min(*p);
                maxL = maxL.max(*p);
            }
        }
        assert!((maxL - Vec3::new(2.0, 1.0, 2.0)).length() < 1e-4, "maxL={maxL}");
        assert!((minL - Vec3::ZERO).length() < 1e-4, "minL={minL}");
        // 偏移起点（负坐标）→ 外框跟随 min
        let eN = outline_edges(&[Vec3i(-1, 2, 3)]);
        let mut minN = Vec3::splat(f32::MAX);
        let mut maxN = Vec3::splat(f32::MIN);
        for (a, b) in &eN {
            for p in [a, b] {
                minN = minN.min(*p);
                maxN = maxN.max(*p);
            }
        }
        assert!((minN - Vec3::new(-1.0, 2.0, 3.0)).length() < 1e-4, "minN={minN}");
        assert!((maxN - Vec3::new(0.0, 3.0, 4.0)).length() < 1e-4, "maxN={maxN}");
    }

    /// 绿面与橙框对齐契约（2026-08-22 修"绿面只 1 格 vs 橙框整模块"）：
    /// module_hit_face 的面中心/scale 与 outline_edges 同轴面的范围一致——
    /// 绿面严格坐落在橙框对应面上；入口面轴由整模块 AABB 反算。
    #[test]
    fn test_module_hit_face_aligns_with_outline_2x1x1() {
        let cells = vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0)]; // 2×1×1
        let base = 0.5_f32;
        // -X 面：ray 从 -X 射来 (origin 在 -X，dir.x>0)
        let (fc, out, fs) = module_hit_face(
            &cells,
            base,
            Vec3::new(-10.0, base + 0.5, 0.5),
            Vec3::new(1.0, 0.0, 0.0),
        )
        .unwrap();
        assert_eq!(out, Vec3::new(-1.0, 0.0, 0.0));
        assert!((fc.x - 0.0).abs() < 1e-5, "fc.x={}", fc.x);
        assert!((fc.y - (base + 0.5)).abs() < 1e-5, "fc.y={}", fc.y);
        assert!((fc.z - 0.5).abs() < 1e-5, "fc.z={}", fc.z);
        assert_eq!(fs, Vec3::new(1.0, 1.0, 1.0));
        // +X 面：origin 在 +X，dir.x<0
        let (_, out2, _) = module_hit_face(
            &cells,
            base,
            Vec3::new(10.0, base + 0.5, 0.5),
            Vec3::new(-1.0, 0.0, 0.0),
        )
        .unwrap();
        assert_eq!(out2, Vec3::new(1.0, 0.0, 0.0));
        // +Z 面（2 格沿 X）：face_scale.x 应=2（与橙框 +Z 面边长一致）
        let (fc3, out3, fs3) = module_hit_face(
            &cells,
            base,
            Vec3::new(0.5, base + 0.5, 10.0),
            Vec3::new(0.0, 0.0, -1.0),
        )
        .unwrap();
        assert_eq!(out3, Vec3::new(0.0, 0.0, 1.0));
        assert!((fc3.z - 1.0).abs() < 1e-5);
        assert_eq!(fs3, Vec3::new(2.0, 1.0, 1.0), "+Z 面应覆盖 2 格 X×1 格 Y");
    }

    /// base 应用：face_center.y 必须含基高（坡上整车基高 ≠ 平地基高）。
    #[test]
    fn test_module_hit_face_applies_base() {
        let cells = vec![Vec3i(0, 0, 0)];
        let base = 3.75_f32;
        let (fc, _, _) = module_hit_face(
            &cells,
            base,
            Vec3::new(-10.0, base + 0.5, 0.5),
            Vec3::new(1.0, 0.0, 0.0),
        )
        .unwrap();
        assert!(
            (fc.y - (base + 0.5)).abs() < 1e-5,
            "face_center.y 必须含 base：fc.y={} 应={}",
            fc.y,
            base + 0.5
        );
    }

    /// **非对称模块（3×1×1）长侧面必须等于整面，不能只 1 格**：
    /// 这是用户"绿面只渲染 1/3 个面"的核心回归（2×2×2 对称测试掩盖不了）。
    #[test]
    fn test_module_hit_face_3x1x1_long_side_full() {
        let cells = vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0), Vec3i(2, 0, 0)]; // 沿 X 长 3
        let base = 0.0_f32;
        // 命中 +Z 长侧面：ray 从 +Z 射来
        let (fc, out, fs) = module_hit_face(
            &cells,
            base,
            Vec3::new(1.5, 0.5, 10.0),
            Vec3::new(0.0, 0.0, -1.0),
        )
        .unwrap();
        assert_eq!(out, Vec3::new(0.0, 0.0, 1.0), "外向应为 +Z");
        assert_eq!(fs, Vec3::new(3.0, 1.0, 1.0), "长侧面应覆盖 3×1，得 {:?}", fs);
        assert!((fc.z - 1.0).abs() < 1e-5, "fc.z={}", fc.z);
        // 命中 -X 端帽（应是 1×1，不是 3×1）
        let (_, out2, fs2) = module_hit_face(
            &cells,
            base,
            Vec3::new(-10.0, 0.5, 0.5),
            Vec3::new(1.0, 0.0, 0.0),
        )
        .unwrap();
        assert_eq!(out2, Vec3::new(-1.0, 0.0, 0.0));
        assert_eq!(fs2, Vec3::new(1.0, 1.0, 1.0), "端帽应为 1×1，得 {:?}", fs2);
    }

    /// 非对称 1×3×1（沿 Y 高 3）——竖向模块长侧面
    /// 注意：face_scale 是**平面局部坐标**（local-X 经旋转映射世界 Y），
    /// 不是世界坐标。+X 面的 face_scale 局部为 (3,1,1)：local-X(=3) 经
    /// from_rotation_arc(Y,+X) 映射到世界 Y，故渲染后世界 Y 跨 3、Z 跨 1——整面。
    #[test]
    fn test_module_hit_face_1x3x1_vertical_full() {
        let cells = vec![Vec3i(0, 0, 0), Vec3i(0, 1, 0), Vec3i(0, 2, 0)]; // 沿 Y 高 3
        let base = 0.0_f32;
        // 命中 +X 侧面：ray 从 +X 射来
        let (_, out, fs) = module_hit_face(
            &cells,
            base,
            Vec3::new(10.0, 1.5, 0.5),
            Vec3::new(-1.0, 0.0, 0.0),
        )
        .unwrap();
        assert_eq!(out, Vec3::new(1.0, 0.0, 0.0));
        // 局部 scale：local-X→世界 Y（高 3），local-Z→世界 Z（深 1）
        assert_eq!(fs, Vec3::new(3.0, 1.0, 1.0), "+X 侧面局部 scale 应 (3,1,1)，得 {:?}", fs);
        // 命中 +Y 顶帽（应是 1×1）
        let (_, out2, fs2) = module_hit_face(
            &cells,
            base,
            Vec3::new(0.5, 10.0, 0.5),
            Vec3::new(0.0, -1.0, 0.0),
        )
        .unwrap();
        assert_eq!(out2, Vec3::new(0.0, 1.0, 0.0));
        assert_eq!(fs2, Vec3::new(1.0, 1.0, 1.0), "顶帽应为 1×1，得 {:?}", fs2);
    }


    /// 射线不命中模块 → None（防御；入口面轴由整模块 AABB 反算，
    /// 射线背离模块时 ray_hits_aabb 返回 None）。
    #[test]
    fn test_module_hit_face_miss_returns_none() {
        let cells = vec![Vec3i(0, 0, 0)];
        // 射线背离模块（dir=-X 且 origin 在模块左侧更左）→ 不命中 → None
        assert!(module_hit_face(
            &cells,
            0.0,
            Vec3::new(-10.0, 0.5, 0.5),
            Vec3::new(-1.0, 0.0, 0.0)
        )
        .is_none());
    }

    /// 决定性对照：绿面尺寸/位置 == 橙框对应面（同坐标系）。
    /// 用 outline_edges 反推橙框每个面的真实 in-plane 尺寸与中心，
    /// 与 module_hit_face 的 (face_center, face_size) 逐轴比对——
    /// 直接证伪"绿面太小/不贴合"。
    #[test]
    fn test_module_hit_face_matches_outline_geometry() {
        // 2×2×2 模块（覆盖所有面尺寸组合）
        let cells: Vec<Vec3i> = (0..2)
            .flat_map(|x| (0..2).flat_map(move |y| (0..2).map(move |z| Vec3i(x, y, z))))
            .collect();
        let base = 0.75_f32;

        // 橙框 AABB（与 outline_edges 同义，不含 base）
        let (mut mnx, mut mny, mut mnz) = (i32::MAX, i32::MAX, i32::MAX);
        let (mut mxx, mut mxy, mut mxz) = (i32::MIN, i32::MIN, i32::MIN);
        for c in &cells {
            mnx = mnx.min(c.0); mny = mny.min(c.1); mnz = mnz.min(c.2);
            mxx = mxx.max(c.0); mxy = mxy.max(c.1); mxz = mxz.max(c.2);
        }
        let ext_x = (mxx - mnx + 1) as f32; // 2
        let ext_y = (mxy - mny + 1) as f32; // 2
        let ext_z = (mxz - mnz + 1) as f32; // 2
        let cx = ((mnx + mxx + 1) as f32) * 0.5;
        let cy = ((mny + mxy + 1) as f32) * 0.5 + base;
        let cz = ((mnz + mxz + 1) as f32) * 0.5;

        // (ray_origin, ray_dir, outward, 橙框面中心, (face_size.x, face_size.z))
        // ax=0(X): in-plane=(Y,Z) → (ext_y, ext_z)
        // ax=1(Y): in-plane=(X,Z) → (ext_x, ext_z)
        // ax=2(Z): in-plane=(X,Y) → (ext_x, ext_y)
        let cases: Vec<(
            (f32, f32, f32),
            (f32, f32, f32),
            Vec3,
            (f32, f32, f32),
            (f32, f32),
        )> = vec![
            ((-10.0, cy, cz), (1.0, 0.0, 0.0), Vec3::new(-1.0, 0.0, 0.0),
                (mnx as f32, cy, cz), (ext_y, ext_z)),
            ((10.0, cy, cz), (-1.0, 0.0, 0.0), Vec3::new(1.0, 0.0, 0.0),
                (mxx as f32 + 1.0, cy, cz), (ext_y, ext_z)),
            ((cx, base - 10.0, cz), (0.0, 1.0, 0.0), Vec3::new(0.0, -1.0, 0.0),
                (cx, mny as f32 + base, cz), (ext_x, ext_z)),
            ((cx, base + 10.0, cz), (0.0, -1.0, 0.0), Vec3::new(0.0, 1.0, 0.0),
                (cx, (mxy + 1) as f32 + base, cz), (ext_x, ext_z)),
            ((cx, cy, -10.0), (0.0, 0.0, 1.0), Vec3::new(0.0, 0.0, -1.0),
                (cx, cy, mnz as f32), (ext_x, ext_y)),
            ((cx, cy, 10.0), (0.0, 0.0, -1.0), Vec3::new(0.0, 0.0, 1.0),
                (cx, cy, mxz as f32 + 1.0), (ext_x, ext_y)),
        ];

        for (o, d, exp_out, exp_center, (exp_fx, exp_fz)) in cases {
            let (fc, out, fs) = module_hit_face(
                &cells, base,
                Vec3::new(o.0, o.1, o.2),
                Vec3::new(d.0, d.1, d.2),
            )
            .unwrap_or_else(|| panic!("face {:?} must be hit", exp_out));
            assert_eq!(out, exp_out, "outward");
            // 尺寸：绿面 in-plane 必须 == 橙框面尺寸
            assert!(
                (fs.x - exp_fx).abs() < 1e-4,
                "{:?} face_size.x={} 期望橙框 {}", exp_out, fs.x, exp_fx
            );
            assert!(
                (fs.z - exp_fz).abs() < 1e-4,
                "{:?} face_size.z={} 期望橙框 {}", exp_out, fs.z, exp_fz
            );
            // 位置：绿面中心必须落在橙框对应面中心（base 已含）
            assert!(
                (fc.x - exp_center.0).abs() < 1e-3,
                "{:?} fc.x={} 期望 {}", exp_out, fc.x, exp_center.0
            );
            assert!(
                (fc.y - exp_center.1).abs() < 1e-3,
                "{:?} fc.y={} 期望 {}", exp_out, fc.y, exp_center.1
            );
            assert!(
                (fc.z - exp_center.2).abs() < 1e-3,
                "{:?} fc.z={} 期望 {}", exp_out, fc.z, exp_center.2
            );
        }
    }
}
