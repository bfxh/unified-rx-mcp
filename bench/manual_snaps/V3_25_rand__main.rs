#![windows_subsystem = "windows"]

//! VoxelForge-V3 渲染层（Bevy 0.19）——干净模块 + 拿起/放置/预览/旋转。

use bevy::prelude::*;
use bevy::input::mouse::MouseMotion;
use avian3d::prelude::*;
use vxl_core::assembly::Assembly;
use vxl_core::assembly::{PhysicsBodyState, PhysicsState, SaveData};
use vxl_core::module::{Category, Face, ModuleDef, MountMask, MountPoint, Shape, Vec3i};
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::PathBuf;

mod vehicle_physics;
use vehicle_physics::{drive_command, drive_fraction, drive_mode, suspension_force, DrivingInput, MAX_SUSPENSION_DISTANCE};

#[derive(Component)]
struct ModuleEntity(u32);

/// 同一 VehicleId 只对应一个物理实体，Collider 是整车全部占用格的 compound。
#[derive(Component)]
struct VehicleBody(u32);

#[derive(Component)]
struct VehicleBodyShape {
    cells: Vec<Vec3i>,
    center: Vec3,
    mass: f32,
}

struct VehicleCompound {
    center: Vec3,
    parts: Vec<(Vec3, Quat, Collider)>,
    cells: Vec<Vec3i>,
    mass: f32,
}

#[derive(Component)]
struct PreviewEntity;

#[derive(Component)]
struct PreviewGrid;

#[derive(Component)]
struct PreviewGridEdges;

#[derive(Default)]
struct PreviewCaches {
    mesh: Option<(Vec<Vec3i>, Handle<Mesh>)>,
    grid: Option<(Vec3, Handle<Mesh>)>,
    grid_edges: Option<(Vec3, Handle<Mesh>)>,
    grid_mats: [Option<Handle<StandardMaterial>>; 2],
    ghost_mat: Option<(Category, Handle<StandardMaterial>)>,
}

#[derive(Component)]
struct MountHighlight {
    key: (Vec3i, Face),
}

#[derive(Component)]
struct ModuleShape {
    cells: Vec<Vec3i>,
    center: Vec3,
}

#[derive(Resource, Default)]
struct HoverSnap {
    cell: Option<Vec3i>,
    rot: u8,
    targets: Vec<(Vec3i, Face)>,
    fixed_targets: std::collections::HashSet<(Vec3i, Face)>,
    free_cell: Option<Vec3i>,
    float_offset: Vec3,
    visual_pos: Option<Vec3>,
}

#[derive(Resource, Default)]
struct HudState {
    selected: String,
    status: String,
}

#[derive(Resource)]
struct CamState { yaw: f32, pitch: f32, dist: f32 }
impl Default for CamState {
    fn default() -> Self { Self { yaw: 0.7, pitch: 0.5, dist: 11.0 } }
}

#[derive(Resource)]
struct AsmRes(Assembly);

#[derive(Resource, Default)]
struct PendingPhysics(Option<PhysicsState>);

#[derive(Resource, Default)]
struct Picked {
    module_id: Option<u32>,
    def_id: String,
    rot: u8,
    vehicle: u32,
    origin: Option<Vec3i>,
    hold_start: Option<f32>,
    was_root: bool,
}

/// 数字键“新模块放置模式”的占位 id（不是装配中的模块）
const PLACING_NEW: u32 = u32::MAX;
/// 候选吸附的屏幕距离半径；真正吸附还要满足世界落点接近。
const SNAP_SCREEN_RADIUS: f32 = 48.0;
/// 候选切换迟滞：避免边界处来回瞬移
const SNAP_HYSTERESIS: f32 = 12.0;
/// 鼠标周围连接点表面区块半径（世界格）
const SURFACE_PATCH_RADIUS: i32 = 5;
/// 手持模块中心周围显示目标载具外露连接点的世界距离。
const MOUNT_VISIBLE_RADIUS: f32 = 5.0;
/// 只有进入候选位置一格内才自动吸附，远处始终自由跟随。
const SNAP_APPROACH_RADIUS: i32 = 1;
/// 左键至少按住这么久才允许松开放置，避免误点
const MIN_HOLD_SECS: f32 = 0.12;

#[derive(Resource, Default)]
struct CursorPos {
    pos: Option<Vec2>,
    velocity: Vec2,
}

fn main() {
    let Some(_instance_lock) = acquire_instance_lock() else { return };
    App::new()
        .add_plugins(DefaultPlugins.set(AssetPlugin {
            file_path: format!("{}/../../assets", env!("CARGO_MANIFEST_DIR")).replace('\\', "/"),
            ..default()
        }))
        .add_plugins(PhysicsPlugins::default())
        .insert_resource(Gravity(Vec3::NEG_Y * 9.81))
        .insert_resource(AsmRes(Assembly::new()))
        .insert_resource(PendingPhysics::default())
        .insert_resource(Picked::default())
        .insert_resource(Selection::default())
        .insert_resource(HoverSnap::default())
        .insert_resource(HudState::default())
        .insert_resource(CamState::default())
        .init_resource::<CursorPos>()
        .add_systems(Startup, (setup_scene, setup_assembly))
        .add_systems(Update, (cursor_pos_system, sync_entities, sync_vehicle_bodies, rotate_picked, preview_follow, mount_highlight, connected_module_ripple, pick_place, selection_system, save_load_system, vehicle_move_system, wheel_suspension_system, collision_damage_system, camera_control, hud_render_system, debug_log_system).chain())
        .add_systems(Startup, spawn_hud)
        .run();
}

fn setup_scene(mut commands: Commands, mut meshes: ResMut<Assets<Mesh>>, mut mats: ResMut<Assets<StandardMaterial>>) {
    commands.spawn((
        Camera3d::default(),
        Transform::from_xyz(9.0, 10.0, 11.0).looking_at(Vec3::new(2.0, 0.5, 2.0), Vec3::Y),
    ));
    // V34: 自由地形——程序化高度场（平地+坡度+起伏），非模块非网格
    let (tmesh, tmat) = build_terrain(&mut meshes, &mut mats);
    commands.spawn((Mesh3d(tmesh), MeshMaterial3d(tmat)));
    // 地面物理碰撞体：平铺在 y=-0.5，模块落在 y=0
    commands.spawn((
        RigidBody::Static,
        Collider::cuboid(60.0, 1.0, 60.0),
        Transform::from_xyz(0.0, -0.5, 0.0),
    ));
    commands.spawn((DirectionalLight::default(), Transform::from_xyz(5.0, 12.0, 7.0).looking_at(Vec3::ZERO, Vec3::Y)));
    commands.insert_resource(GlobalAmbientLight { color: Color::srgb(0.75, 0.75, 0.8), brightness: 150.0, ..default() });
}


/// V34: 程序化自由地形——高度场（含平地/坡度/起伏），不占格
fn build_terrain(
    meshes: &mut Assets<Mesh>,
    mats: &mut Assets<StandardMaterial>,
) -> (Handle<Mesh>, Handle<StandardMaterial>) {
    let size = 60i32;
    let scale = 1.0f32;
    fn height(x: f32, z: f32) -> f32 { terrain_height(x, z) }
    let mut positions = Vec::new();
    let mut normals = Vec::new();
    let mut uvs = Vec::new();
    let mut indices = Vec::new();
    let n = size as usize;
    for iz in 0..=n {
        for ix in 0..=n {
            let x = (ix as f32 - size as f32 * 0.5) * scale;
            let z = (iz as f32 - size as f32 * 0.5) * scale;
            let y = height(x, z);
            positions.push([x, y, z]);
            uvs.push([ix as f32 / n as f32, iz as f32 / n as f32]);
            let h1 = height(x + 0.1, z);
            let h2 = height(x, z + 0.1);
            let nrm = Vec3::new(-(h1 - y) / 0.1, 1.0, -(h2 - y) / 0.1).normalize();
            normals.push([nrm.x, nrm.y, nrm.z]);
        }
    }
    for iz in 0..n {
        for ix in 0..n {
            let a = (iz * (n + 1) + ix) as u32;
            let b = (iz * (n + 1) + ix + 1) as u32;
            let c = ((iz + 1) * (n + 1) + ix) as u32;
            let d = ((iz + 1) * (n + 1) + ix + 1) as u32;
            indices.extend_from_slice(&[a, b, c, b, d, c]);
        }
    }
    let mut mesh = Mesh::new(
        bevy::mesh::PrimitiveTopology::TriangleList,
        bevy::asset::RenderAssetUsages::RENDER_WORLD,
    );
    mesh.insert_attribute(Mesh::ATTRIBUTE_POSITION, positions);
    mesh.insert_attribute(Mesh::ATTRIBUTE_NORMAL, normals);
    mesh.insert_attribute(Mesh::ATTRIBUTE_UV_0, uvs);
    mesh.insert_indices(bevy::mesh::Indices::U32(indices));
    let mat = mats.add(StandardMaterial { base_color: Color::srgb(0.35, 0.42, 0.3), perceptual_roughness: 0.9, ..default() });
    (meshes.add(mesh), mat)
}

/// 地形高度：干净平地（世界 Y=0）。模块按世界 Y 轴落地，不悬空。
fn terrain_height(x: f32, z: f32) -> f32 {
    let _ = (x, z);
    0.0
}

fn def(id: &str, cat: Category, dims: [u32; 3]) -> ModuleDef {
    ModuleDef {
        id: id.into(), name: id.into(), corp: "nexus".into(), category: cat,
        mass: 10.0, hp: 100, shape: Shape::Block { dims },
        mount_points: Face::ALL.iter().map(|&f| MountPoint {
            cell: Vec3i(0,0,0), face: f, accepts: MountMask::Any, strength: 100.0, layer: 0,
        }).collect(),
        tags: vec![],
    }
}

fn build_demo_assembly(a: &mut Assembly) -> Result<(), String> {
    for d in load_module_defs() { a.register(d); }
    let root_def = if a.defs.contains_key("nexus.cab") {
        "nexus.cab".to_string()
    } else {
        a.defs.keys().next().cloned().unwrap_or_else(|| "nexus.b".into())
    };
    a.place_root(&root_def, Vec3i(0,0,0), 0).map_err(|e| format!("根模块: {e:?}"))?;
    for (id, pos) in [("nexus.b", Vec3i(1,0,0)), ("nexus.b", Vec3i(2,0,0)), ("nexus.w", Vec3i(0,0,-1)), ("nexus.w", Vec3i(2,0,-1))] {
        a.place(id, pos, 0).map_err(|e| format!("{id} {:?}: {e:?}", pos))?;
    }
    Ok(())
}

struct InstanceLock { _file: File, path: PathBuf }

impl Drop for InstanceLock {
    fn drop(&mut self) { let _ = std::fs::remove_file(&self.path); }
}

/// 防止启动器重复拉起多个窗口；锁文件由进程持有到退出。
fn acquire_instance_lock() -> Option<InstanceLock> {
    let path = PathBuf::from("vxl_app.instance.lock");
    if let Ok(mut file) = OpenOptions::new().write(true).create_new(true).open(&path) {
        let _ = write!(file, "{}", std::process::id());
        return Some(InstanceLock { _file: file, path });
    }
    let mut old_pid = String::new();
    let _ = File::open(&path).and_then(|mut file| file.read_to_string(&mut old_pid));
    let alive = old_pid.trim().parse::<u32>().is_ok_and(|pid| {
        std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH"])
            .output()
            .map(|out| String::from_utf8_lossy(&out.stdout)
                .lines()
                .any(|line| line.split_whitespace().any(|token| token == pid.to_string())))
            .unwrap_or(true)
    });
    if alive { return None; }
    let _ = std::fs::remove_file(&path);
    let mut file = OpenOptions::new().write(true).create_new(true).open(&path).ok()?;
    let _ = write!(file, "{}", std::process::id());
    Some(InstanceLock { _file: file, path })
}

fn setup_assembly(mut asm: ResMut<AsmRes>, mut exit: MessageWriter<AppExit>) {
    let a = &mut asm.0;
    if let Err(error) = build_demo_assembly(a) {
        eprintln!("demo assembly failed: {error}");
        exit.write(AppExit::from_code(1));
    }
}

/// 从 RON 模块库读取；失败回退内置 demo（保证 exe 独立可跑）
fn load_module_defs() -> Vec<ModuleDef> {
    if let Ok(text) = std::fs::read_to_string("data/modules.ron")
        && let Ok(defs) = ron::from_str::<Vec<ModuleDef>>(&text) {
        return defs;
    }
    vec![
        def("nexus.b", Category::Structure, [1,1,1]),
        def("nexus.cab", Category::Cab, [1,1,1]),
        def("nexus.w", Category::Wheel, [1,1,1]),
    ]
}

fn cat_color(cat: Category) -> Color {
    match cat {
        Category::Structure => Color::srgb(0.55, 0.62, 0.72),
        Category::Cab => Color::srgb(0.82, 0.52, 0.3),
        Category::Wheel => Color::srgb(0.2, 0.2, 0.22),
        Category::Engine => Color::srgb(0.75, 0.4, 0.2),
        _ => Color::srgb(0.7, 0.45, 0.4),
    }
}

fn cursor_pos_system(mut cursor: ResMut<CursorPos>, window: Query<&Window>) {
    let Ok(window) = window.single() else { return };
    let Some(pos) = window.cursor_position() else { return };
    if let Some(prev) = cursor.pos {
        cursor.velocity = pos - prev;
    }
    cursor.pos = Some(pos);
}

/// 射线-AABB 求交（slab 法）——鼠标精确拾取模块
fn ray_aabb(ray: bevy::math::Ray3d, lo: Vec3, hi: Vec3) -> Option<f32> {
    let inv = Vec3::new(1.0 / ray.direction.x, 1.0 / ray.direction.y, 1.0 / ray.direction.z);
    let mut t0 = (lo - ray.origin) * inv;
    let mut t1 = (hi - ray.origin) * inv;
    if inv.x < 0.0 { std::mem::swap(&mut t0.x, &mut t1.x); }
    if inv.y < 0.0 { std::mem::swap(&mut t0.y, &mut t1.y); }
    if inv.z < 0.0 { std::mem::swap(&mut t0.z, &mut t1.z); }
    let tmin = t0.x.max(t0.y).max(t0.z);
    let tmax = t1.x.min(t1.y).min(t1.z);
    if tmax >= tmin && tmax >= 0.0 { Some(tmin.max(0.0)) } else { None }
}

/// 屏幕点在地面（世界 Y=0）上的投影
fn ground_point(cam: &Camera, cam_tf: &GlobalTransform, screen: Vec2) -> Option<Vec3> {
    let Ok(ray) = cam.viewport_to_world(cam_tf, screen) else { return None };
    if ray.direction.y.abs() < 1e-6 { return None; }
    let t = -ray.origin.y / ray.direction.y;
    if t < 0.0 { return None; }
    Some(ray.origin + ray.direction * t)
}

/// 射线命中的最近模块及命中点（逐占用格判定，避免相邻 AABB 误选）
fn ray_hit_module(asm: &Assembly, ray: bevy::math::Ray3d) -> Option<(u32, Vec3)> {
    let mut best: Option<(f32, u32, Vec3)> = None;
    for (mid, md) in asm.modules.iter() {
        for &c in &md.cells {
            let lo = Vec3::new(c.0 as f32, c.1 as f32, c.2 as f32);
            let hi = lo + Vec3::ONE;
            if let Some(t) = ray_aabb(ray, lo, hi)
                && best.as_ref().map(|(bt, _, _)| t < *bt).unwrap_or(true) {
                best = Some((t, *mid, ray.origin + ray.direction * t));
            }
        }
    }
    best.map(|(_, mid, p)| (mid, p))
}

/// 按实际渲染 Transform 拾取，散落模块被物理移动后仍能在画面所在位置再次拿起。
fn ray_hit_rendered_module(
    asm: &Assembly,
    ray: bevy::math::Ray3d,
    rendered: &std::collections::HashMap<u32, Vec3>,
) -> Option<(u32, Vec3)> {
    let mut best: Option<(f32, u32, Vec3)> = None;
    for (mid, md) in &asm.modules {
        let logical_center = module_aabb(md).2;
        let offset = rendered.get(mid).copied().unwrap_or(logical_center) - logical_center;
        for &cell in &md.cells {
            let lo = Vec3::new(cell.0 as f32, cell.1 as f32, cell.2 as f32) + offset;
            if let Some(t) = ray_aabb(ray, lo, lo + Vec3::ONE)
                && best.as_ref().is_none_or(|(old, _, _)| t < *old) {
                best = Some((t, *mid, ray.origin + ray.direction * t));
            }
        }
    }
    best.map(|(_, mid, point)| (mid, point))
}

/// 把格坐标沿射线外推到命中模块表面外，避免 ghost/落点钻进模块内部
fn push_out_of_module(md: &vxl_core::assembly::ModuleData, p: Vec3, dir: Vec3) -> Vec3i {
    let occ: std::collections::HashSet<Vec3i> = md.cells.iter().copied().collect();
    let dir = dir.normalize_or_zero();
    let mut c = Vec3i(p.x.floor() as i32, p.y.floor() as i32, p.z.floor() as i32);
    let mut guard = 0;
    while occ.contains(&c) && guard < 8 {
        let q = p + dir * (0.6 + guard as f32 * 0.7);
        c = Vec3i(q.x.floor() as i32, q.y.floor() as i32, q.z.floor() as i32);
        guard += 1;
    }
    c
}

/// 占用格最小/尺寸/中心（单位格世界坐标）。
fn cells_aabb(cells: &[Vec3i]) -> (Vec3i, Vec3i, Vec3) {
    let mut lo = Vec3i(i32::MAX, i32::MAX, i32::MAX);
    let mut hi = Vec3i(i32::MIN, i32::MIN, i32::MIN);
    for &c in cells {
        lo.0 = lo.0.min(c.0); lo.1 = lo.1.min(c.1); lo.2 = lo.2.min(c.2);
        hi.0 = hi.0.max(c.0); hi.1 = hi.1.max(c.1); hi.2 = hi.2.max(c.2);
    }
    let dims = hi - lo + Vec3i(1, 1, 1);
    let center = Vec3::new(
        lo.0 as f32 + dims.0 as f32 * 0.5,
        lo.1 as f32 + dims.1 as f32 * 0.5,
        lo.2 as f32 + dims.2 as f32 * 0.5,
    );
    (lo, dims, center)
}

/// 模块世界 AABB（基于已旋转的占用格，保证拾取/渲染与装配一致）
fn module_aabb(md: &vxl_core::assembly::ModuleData) -> (Vec3, Vec3, Vec3) {
    let (lo, dims, center) = cells_aabb(&md.cells);
    let lo_f = Vec3::new(lo.0 as f32, lo.1 as f32, lo.2 as f32);
    let hi_ex = lo_f + Vec3::new(dims.0 as f32, dims.1 as f32, dims.2 as f32);
    (lo_f, hi_ex, center)
}

/// 手持高度的参考点：优先 root/驾驶舱中线，其次目标载具质心
fn held_reference(asm: &Assembly, vehicle: u32) -> Option<Vec3> {
    if let Some(rid) = asm.root && let Some(md) = asm.modules.get(&rid) {
        return Some(module_aabb(md).2);
    }
    if let Some((x, y, z)) = asm.vehicle_center(vehicle) {
        return Some(Vec3::new(x, y, z));
    }
    None
}

/// 模块定义（含旋转）的实际占用格
fn rotated_cells(def: &ModuleDef, rot: u8) -> Vec<Vec3i> {
    vxl_core::rotation::rotated_local_cells(&def.shape.local_cells(), rot)
}

/// 模块定义（含旋转）的 AABB 尺寸，用于预览外框
fn def_dims(def: &ModuleDef, rot: u8) -> Vec3 {
    let (_, dims, _) = cells_aabb(&rotated_cells(def, rot));
    Vec3::new(dims.0 as f32, dims.1 as f32, dims.2 as f32)
}

/// 模块定义（含旋转 + 放置原点）的 AABB 中心
fn def_center(def: &ModuleDef, rot: u8, origin: Vec3i) -> Vec3 {
    let (_, _, center) = cells_aabb(&rotated_cells(def, rot));
    Vec3::new(origin.0 as f32, origin.1 as f32, origin.2 as f32) + center
}

/// 每个占用格中心相对 AABB 中心的偏移
fn cell_centers_for(cells: &[Vec3i], center: Vec3) -> Vec<Vec3> {
    cells.iter()
        .map(|&c| Vec3::new(c.0 as f32 + 0.5, c.1 as f32 + 0.5, c.2 as f32 + 0.5) - center)
        .collect()
}

/// 把若干个小立方体合并成一个网格（half 是每个立方体的半边长）
fn boxes_mesh(local_centers: &[Vec3], half: f32) -> Mesh {
    let faces: [([Vec3; 4], Vec3); 6] = [
        ([Vec3::new(half, -half, -half), Vec3::new(half, half, -half), Vec3::new(half, half, half), Vec3::new(half, -half, half)], Vec3::X),
        ([Vec3::new(-half, -half, half), Vec3::new(-half, half, half), Vec3::new(-half, half, -half), Vec3::new(-half, -half, -half)], Vec3::NEG_X),
        ([Vec3::new(-half, half, -half), Vec3::new(half, half, -half), Vec3::new(half, half, half), Vec3::new(-half, half, half)], Vec3::Y),
        ([Vec3::new(-half, -half, half), Vec3::new(half, -half, half), Vec3::new(half, -half, -half), Vec3::new(-half, -half, -half)], Vec3::NEG_Y),
        ([Vec3::new(-half, -half, half), Vec3::new(half, -half, half), Vec3::new(half, half, half), Vec3::new(-half, half, half)], Vec3::Z),
        ([Vec3::new(half, -half, -half), Vec3::new(-half, -half, -half), Vec3::new(-half, half, -half), Vec3::new(half, half, -half)], Vec3::NEG_Z),
    ];
    let mut positions = Vec::with_capacity(local_centers.len() * 24);
    let mut normals = Vec::with_capacity(local_centers.len() * 24);
    let mut uvs = Vec::with_capacity(local_centers.len() * 24);
    let mut indices = Vec::with_capacity(local_centers.len() * 36);
    for (ci, &c) in local_centers.iter().enumerate() {
        let base = (ci as u32) * 24;
        for (fi, (verts, n)) in faces.iter().enumerate() {
            let fbase = base + (fi as u32) * 4;
            for v in verts {
                positions.push(c + *v);
                normals.push(*n);
            }
            uvs.extend_from_slice(&[
                Vec2::new(0.0, 0.0),
                Vec2::new(1.0, 0.0),
                Vec2::new(1.0, 1.0),
                Vec2::new(0.0, 1.0),
            ]);
            indices.extend_from_slice(&[fbase, fbase + 1, fbase + 2, fbase, fbase + 2, fbase + 3]);
        }
    }
    let mut mesh = Mesh::new(
        bevy::mesh::PrimitiveTopology::TriangleList,
        bevy::asset::RenderAssetUsages::RENDER_WORLD,
    );
    mesh.insert_attribute(Mesh::ATTRIBUTE_POSITION, positions);
    mesh.insert_attribute(Mesh::ATTRIBUTE_NORMAL, normals);
    mesh.insert_attribute(Mesh::ATTRIBUTE_UV_0, uvs);
    mesh.insert_indices(bevy::mesh::Indices::U32(indices));
    mesh
}

/// 把占用格合并成真实体素形状网格（不再用 AABB 整方块代替）
fn cells_mesh(local_centers: &[Vec3]) -> Mesh {
    boxes_mesh(local_centers, 0.5)
}

/// 只保留 AABB 八个顶角的框架：没有面，正常白、错误红
fn corner_frame_mesh(dims: Vec3, dot: f32) -> Mesh {
    let h = dims * 0.5;
    let corners = [
        Vec3::new(-h.x, -h.y, -h.z),
        Vec3::new(h.x, -h.y, -h.z),
        Vec3::new(-h.x, h.y, -h.z),
        Vec3::new(h.x, h.y, -h.z),
        Vec3::new(-h.x, -h.y, h.z),
        Vec3::new(h.x, -h.y, h.z),
        Vec3::new(-h.x, h.y, h.z),
        Vec3::new(h.x, h.y, h.z),
    ];
    boxes_mesh(&corners, dot * 0.5)
}

/// 框架 12 条直线边（无斜线），与 Blender 插件 occ_outline_edges 一致
fn edge_frame_mesh(dims: Vec3) -> Mesh {
    let h = dims * 0.5;
    let c = [
        Vec3::new(-h.x, -h.y, -h.z),
        Vec3::new(h.x, -h.y, -h.z),
        Vec3::new(-h.x, h.y, -h.z),
        Vec3::new(h.x, h.y, -h.z),
        Vec3::new(-h.x, -h.y, h.z),
        Vec3::new(h.x, -h.y, h.z),
        Vec3::new(-h.x, h.y, h.z),
        Vec3::new(h.x, h.y, h.z),
    ];
    let edges = [
        (0, 1), (1, 3), (3, 2), (2, 0),
        (4, 5), (5, 7), (7, 6), (6, 4),
        (0, 4), (1, 5), (3, 7), (2, 6),
    ];
    let mut positions = Vec::with_capacity(24);
    for (a, b) in edges {
        positions.push(c[a]);
        positions.push(c[b]);
    }
    let indices: Vec<u32> = (0..24).collect();
    let mut mesh = Mesh::new(
        bevy::mesh::PrimitiveTopology::LineList,
        bevy::asset::RenderAssetUsages::RENDER_WORLD,
    );
    mesh.insert_attribute(Mesh::ATTRIBUTE_POSITION, positions);
    mesh.insert_indices(bevy::mesh::Indices::U32(indices));
    mesh
}

struct SnapChoice {
    cell: Vec3i,
    rot: u8,
}

/// 泰拉科技式候选吸附：遍历候选位置，取最近且稳定的落点
#[allow(clippy::too_many_arguments)]
fn pick_snap_feedback(
    asm: &Assembly,
    def_id: &str,
    vehicle: u32,
    cursor_cell: Vec3i,
    cpos: Vec2,
    cam: &Camera,
    cam_tf: &GlobalTransform,
    prev: (Option<Vec3i>, u8),
) -> Option<SnapChoice> {
    let def = asm.defs.get(def_id)?;
    let mut cands: Vec<(f32, Vec3i, u8)> = Vec::new();
    let mut close3 = false;
    for (cell, rot) in asm.snap_candidates(def_id) {
        let cheb = (cell.0 - cursor_cell.0).abs()
            .max((cell.1 - cursor_cell.1).abs())
            .max((cell.2 - cursor_cell.2).abs());
        if cheb > SURFACE_PATCH_RADIUS { continue; }
        if cheb <= SNAP_APPROACH_RADIUS { close3 = true; }
        if !matches!(asm.probe_place_with_vehicle(def_id, cell, rot, vehicle), Ok(vxl_core::assembly::ProbeResult::Snap)) {
            continue;
        }
        let center = def_center(def, rot, cell);
        if let Ok(screen) = cam.world_to_viewport(cam_tf, center) {
            let d = screen.distance(cpos);
            if d < SNAP_SCREEN_RADIUS { cands.push((d, cell, rot)); }
        }
    }
    // 默认 3 格才有吸附；贴近未松手时在同载具扩大到 5 格
    if !close3 || cands.is_empty() { return None; }
    cands.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    let best = cands[0];
    // 迟滞：上一候选仍在附近且不比最佳差太多时保持，避免边界来回瞬移
    let chosen = if let Some(pc) = prev.0
        && let Some(prev_item) = cands.iter().find(|(_, c, r)| *c == pc && *r == prev.1)
        && prev_item.0 <= best.0 + SNAP_HYSTERESIS {
        (prev_item.1, prev_item.2)
    } else {
        (best.1, best.2)
    };
    Some(SnapChoice { cell: chosen.0, rot: chosen.1 })
}

/// 只收集 5 米内目标载具的外露连接点；不显示手持模块内部构造。
fn nearby_target_mounts(
    asm: &Assembly,
    held_def: &ModuleDef,
    center: Vec3,
    vehicle: u32,
) -> Vec<(Vec3i, Face)> {
    let mut out = std::collections::HashSet::new();
    for md in asm.modules.values().filter(|md| md.vehicle != 0 && (vehicle == 0 || md.vehicle == vehicle)) {
        let target_def = &asm.defs[&md.def_id];
        for mount in &md.mounts {
            let compatible = mount.accepts.accepts(held_def.category)
                && held_def.mount_points.iter().any(|held| {
                    held.accepts.accepts(target_def.category)
                        && (held.layer == 0 || mount.layer == 0 || held.layer == mount.layer)
                });
            if !compatible { continue; }
            let d = mount.face.dir();
            let position = Vec3::new(
                mount.cell.0 as f32 + 0.5 + d.0 as f32 * 0.5,
                mount.cell.1 as f32 + 0.5 + d.1 as f32 * 0.5,
                mount.cell.2 as f32 + 0.5 + d.2 as f32 * 0.5,
            );
            if position.distance(center) <= MOUNT_VISIBLE_RADIUS {
                out.insert((mount.cell, mount.face));
            }
        }
    }
    out.into_iter().collect()
}

/// 返回整车 compound 的世界中心和相对此中心的子碰撞体位置。
/// 质心按模块质量加权：Σ(m·p)/Σm，每个占用格均摊所属模块的质量。
fn vehicle_compound_parts(asm: &Assembly, vehicle: u32) -> Option<VehicleCompound> {
    let mut cells: Vec<Vec3i> = Vec::new();
    let mut weighted: Vec<(Vec3, f32)> = Vec::new();
    for md in asm.modules.values().filter(|md| md.vehicle == vehicle) {
        let def_mass = asm.defs.get(&md.def_id).map(|d| d.mass).unwrap_or(1.0);
        let per_cell = def_mass / md.cells.len().max(1) as f32;
        for &c in &md.cells {
            cells.push(c);
            weighted.push((Vec3::new(c.0 as f32 + 0.5, c.1 as f32 + 0.5, c.2 as f32 + 0.5), per_cell));
        }
    }
    if cells.is_empty() { return None; }
    cells.sort_unstable();
    let total_mass: f32 = weighted.iter().map(|(_, m)| m).sum();
    let center = weighted.iter().fold(Vec3::ZERO, |acc, (p, m)| acc + *p * *m) / total_mass;
    let parts = weighted.iter()
        .map(|&(p, _)| (p - center, Quat::IDENTITY, Collider::cuboid(1.0, 1.0, 1.0)))
        .collect();
    Some(VehicleCompound { center, parts, cells, mass: total_mass.max(0.1) })
}

/// 装配 → 实体（干净模块本体，不渲染连接点）
#[allow(clippy::type_complexity)]
fn sync_entities(
    asm: Res<AsmRes>,
    mut commands: Commands,
    mut meshes: ResMut<Assets<Mesh>>,
    mut mats: ResMut<Assets<StandardMaterial>>,
    mut q: Query<(Entity, &mut Transform, &ModuleEntity, Option<&mut ModuleShape>, Option<&RigidBody>, Option<&Collider>), Without<VehicleBody>>,
    bodies: Query<(&VehicleBody, &Transform, &VehicleBodyShape), With<VehicleBody>>,
) {
    let a = &asm.0;
    let body_offsets: std::collections::HashMap<u32, (Vec3, Vec3)> = bodies.iter()
        .map(|(body, tf, shape)| (body.0, (tf.translation, shape.center)))
        .collect();
    let mut seen = std::collections::HashSet::new();
    let mut existing: std::collections::HashMap<u32, Entity> = std::collections::HashMap::new();
    for (e, _, m, _, _, _) in q.iter() { existing.insert(m.0, e); }
    for (mid, md) in a.modules.iter() {
        seen.insert(*mid);
        let Some(def) = a.defs.get(&md.def_id) else { continue };
        let center = module_aabb(md).2;
        // 整车移动/加载后同步实体位置，保证视觉零延迟跟随装配
        if let Some(&e) = existing.get(mid) {
            // 只同步载具模块（Static，由装配控制）；散落模块交给物理引擎
            if md.vehicle != 0
                && let Ok((_, mut tf, _, shape, _, _)) = q.get_mut(e) {
                let (body_position, body_center) = body_offsets.get(&md.vehicle)
                    .copied()
                    .unwrap_or((center, center));
                // 模块实体跟随整车刚体，但保留自己相对整车中心的偏移。
                tf.translation = body_position + (center - body_center);
                let need_rebuild = shape.as_ref().is_none_or(|s| {
                    s.cells != md.cells || s.center.distance(center) > 1e-4
                });
                if need_rebuild {
                    let cell_centers = cell_centers_for(&md.cells, center);
                    let mesh = meshes.add(cells_mesh(&cell_centers));
                    commands.entity(e).insert(Mesh3d(mesh));
                    if let Some(mut s) = shape {
                        s.cells = md.cells.clone();
                        s.center = center;
                    }
                }
            }
            continue;
        }
        // A fragment is no longer represented by the old VehicleBody.  This
        // also covers the frame in which damage changes vehicle to zero.
        let cell_centers = cell_centers_for(&md.cells, center);
        let mesh = meshes.add(cells_mesh(&cell_centers));
        let mut module = commands.spawn((
            ModuleEntity(*mid),
            ModuleShape { cells: md.cells.clone(), center },
            Mesh3d(mesh),
            MeshMaterial3d(mats.add(StandardMaterial { base_color: cat_color(def.category), perceptual_roughness: 0.45, metallic: 0.1, ..default() })),
            Transform::from_translation(center),
        ));
        // 散落模块保留独立动态刚体；载具模块只渲染，碰撞统一交给 VehicleBody。
        if md.vehicle == 0 {
            let collider = Collider::compound(
                cell_centers.iter().map(|&p| (p, Quat::IDENTITY, Collider::cuboid(1.0, 1.0, 1.0))).collect(),
            );
            module.insert((RigidBody::Dynamic, LockedAxes::from_bits(0b101_111), CollisionEventsEnabled, collider));
        }
        let module_e = module.id();
        // 常态模块不显示连接点；连接点只在吸附目标处显示（MountHighlight）
        let _ = module_e;
    }
    for (e, _, m, _, rigid_body, collider) in q.iter() {
        if let Some(md) = a.modules.get(&m.0)
            && md.vehicle == 0
            && (rigid_body.is_none() || collider.is_none()) {
            let center = module_aabb(md).2;
            let cells = cell_centers_for(&md.cells, center);
            commands.entity(e).insert((
                RigidBody::Dynamic,
                LockedAxes::from_bits(0b101_111),
                CollisionEventsEnabled,
                Collider::compound(cells.into_iter().map(|p| (p, Quat::IDENTITY, Collider::cuboid(1.0, 1.0, 1.0))).collect()),
            ));
        }
        if !seen.contains(&m.0) { commands.entity(e).despawn(); }
    }
}

/// 拿起/放置：左键按下=拿起，松开=放置
#[allow(clippy::too_many_arguments)]
fn pick_place(
    mut asm: ResMut<AsmRes>,
    mut picked: ResMut<Picked>,
    hover: Res<HoverSnap>,
    mut hud: ResMut<HudState>,
    buttons: Res<ButtonInput<MouseButton>>,
    keys: Res<ButtonInput<KeyCode>>,
    cursor: Res<CursorPos>,
    camera: Query<(&Camera, &GlobalTransform)>,
    entities: Query<(&Transform, &ModuleEntity)>,
    time: Res<Time>,
) {
    let Some(cpos) = cursor.pos else { return };
    let Ok((cam, cam_tf)) = camera.single() else { return };

    // ESC：取消拿起（放回原位）或退出新模块放置模式
    if keys.just_pressed(KeyCode::Escape) && picked.module_id.is_some() {
        if picked.module_id != Some(PLACING_NEW) {
            if let Some(origin) = picked.origin {
                let def_id = picked.def_id.clone();
                let rot = picked.rot;
                let vehicle = picked.vehicle;
                let result = if picked.was_root {
                    asm.0.place_root(&def_id, origin, rot)
                } else {
                    asm.0.place_free_with_vehicle(&def_id, origin, rot, vehicle)
                };
                // 原位被占则保留在手上，不丢模块
                if result.is_ok() {
                    picked.module_id = None;
                    picked.hold_start = None;
                    picked.was_root = false;
                }
            }
        } else {
            picked.module_id = None;
            picked.hold_start = None;
            picked.was_root = false;
        }
        hud.status = String::new();
        return;
    }

    // 右键：手持时取消拿起/放回；空闲时由 camera_control 旋转视角
    if buttons.just_pressed(MouseButton::Right) && picked.module_id.is_some() {
        if picked.module_id != Some(PLACING_NEW) {
            let def_id = picked.def_id.clone();
            let rot = picked.rot;
            let vehicle = picked.vehicle;
            let cell = hover.cell.or(picked.origin);
            if let Some(c) = cell {
                let mut result = if picked.was_root {
                    asm.0.place_root(&def_id, c, rot)
                } else {
                    asm.0.place_free_with_vehicle(&def_id, c, rot, vehicle)
                };
                if result.is_err() {
                    'search: for r in 1i32..=2 {
                        for dz in -r..=r {
                            for dy in -r..=r {
                                for dx in -r..=r {
                                    if dx.abs() != r && dy.abs() != r && dz.abs() != r { continue; }
                                    let c2 = c + Vec3i(dx, dy, dz);
                                    result = if picked.was_root {
                                        asm.0.place_root(&def_id, c2, rot)
                                    } else {
                                        asm.0.place_free_with_vehicle(&def_id, c2, rot, vehicle)
                                    };
                                    if result.is_ok() { break 'search; }
                                }
                            }
                        }
                    }
                }
            }
        }
        picked.module_id = None;
        picked.hold_start = None;
        picked.was_root = false;
        hud.status = "已取消拿起".into();
        return;
    }

    if picked.module_id.is_none() && buttons.just_pressed(MouseButton::Left) {
        // 拿起：按实际渲染位置逐占用格命中，物理移动后的散落模块也可直接再次拿起。
        let Ok(ray) = cam.viewport_to_world(cam_tf, cpos) else { return };
        let rendered: std::collections::HashMap<u32, Vec3> = entities.iter()
            .map(|(tf, module)| (module.0, tf.translation))
            .collect();
        if let Some((mid, _)) = ray_hit_rendered_module(&asm.0, ray, &rendered) {
            let md = &asm.0.modules[&mid];
            let def_id = md.def_id.clone();
            let is_root = Some(mid) == asm.0.root;
            let vehicle = md.vehicle;
            // 散落模块物理落地后，拿起前把逻辑坐标对齐实际位置（Y）
            if vehicle == 0
                && let Some((tf, _)) = entities.iter().find(|(_, m)| m.0 == mid) {
                let dims = if let Some(md) = asm.0.modules.get(&mid) {
                    let (lo, hi, _) = module_aabb(md);
                    hi - lo
                } else {
                    Vec3::ONE
                };
                let origin = Vec3i(
                    (tf.translation.x - dims.x * 0.5).round() as i32,
                    (tf.translation.y - dims.y * 0.5).round() as i32,
                    (tf.translation.z - dims.z * 0.5).round() as i32,
                );
                let _ = asm.0.set_module_origin(mid, origin);
            }
            let origin = asm.0.modules.get(&mid).map(|m| m.origin);
            let module_rot = asm.0.modules.get(&mid).map(|m| m.rotation).unwrap_or(0);
            let _ = asm.0.remove(mid);
            picked.module_id = Some(mid);
            picked.def_id = def_id;
            picked.vehicle = vehicle;
            picked.origin = origin;
            picked.was_root = is_root;
            // 拿起即用模块当前旋转，ghost/网格立刻一致
            picked.rot = module_rot;
            picked.hold_start = Some(time.elapsed_secs());
        }
    }

    if buttons.just_released(MouseButton::Left) {
        // 短按只负责“拿起”，不立刻放回，避免误触/瞬移
        if let Some(start) = picked.hold_start
            && time.elapsed_secs() - start < MIN_HOLD_SECS {
            picked.hold_start = None;
            hud.status = "按住左键拿着，松开才放置".into();
            return;
        }
        picked.hold_start = None;
        // 放置：射线与 y=0 平面交点 → 格
        // B1 修复：放置失败不丢模块——成功则更新为新 id（remove+place 产生新 id），
        //          失败则保留在 picked（玩家可换位置再放，模块一直在手上）
        if picked.module_id.is_some() {
            // 甩出手感：松开时按鼠标速度把落点往前带一点
            let throw_screen = cpos + cursor.velocity * 0.9;
            // 预测点飞出屏幕/失效时回退到当前鼠标位置，绝不吞掉这次放置
            let mut hit = match ground_point(cam, cam_tf, throw_screen) {
                Some(h) => h,
                None => match ground_point(cam, cam_tf, cpos) {
                    Some(h) => h,
                    None => return,
                },
            };
            // 限制甩出距离，避免一键甩到地图外
            if let Some(base) = ground_point(cam, cam_tf, cpos) {
                let off = hit - base;
                let len = off.length();
                if len > 10.0 {
                    hit = base + off / len * 10.0;
                }
            }
            // 落地高度取地形表面，模块贴合场景而不是浮在 y=0
            let mut cell = Vec3i(
                hit.x.floor() as i32,
                terrain_height(hit.x, hit.z).floor() as i32,
                hit.z.floor() as i32,
            );
            // 防穿模：鼠标指向已有模块时，落点外推到模块表面外
            if let Ok(ray) = cam.viewport_to_world(cam_tf, cpos)
                && let Some((mid, p)) = ray_hit_module(&asm.0, ray)
                && let Some(md) = asm.0.modules.get(&mid) {
                cell = push_out_of_module(md, p, *ray.direction);
            }
            let def_id = picked.def_id.clone();
            let vehicle = picked.vehicle;
            let was_root = picked.was_root;
            let a = &mut asm.0;
            // 泰拉科技式：优先吸附最近候选挂点；无候选才落地面
            let (mut cell, rot) = if let Some(c) = hover.cell {
                (c, hover.rot)
            } else {
                (cell, picked.rot)
            };
            // 漏洞2修复：按 probe 三态放置——Snap 才 place，Free 才 place_free，Err 不放
            let mut result = a.probe_place_with_vehicle(&def_id, cell, rot, vehicle);
            // 落点被占时在周围找可散落放置的格子，让“放地上/贴方块”更宽容
            if result.is_err() {
                'search: for r in 1i32..=2 {
                    for dz in -r..=r {
                        for dy in -r..=r {
                            for dx in -r..=r {
                                if dx.abs() != r && dy.abs() != r && dz.abs() != r { continue; }
                                let c2 = cell + Vec3i(dx, dy, dz);
                                if matches!(a.probe_place_with_vehicle(&def_id, c2, rot, vehicle), Ok(vxl_core::assembly::ProbeResult::Free)) {
                                    cell = c2;
                                    result = Ok(vxl_core::assembly::ProbeResult::Free);
                                    break 'search;
                                }
                            }
                        }
                    }
                }
            }
            let result = if was_root {
                a.place_root(&def_id, cell, rot)
            } else {
                match result {
                    Ok(vxl_core::assembly::ProbeResult::Snap) => a.place_with_vehicle(&def_id, cell, rot, vehicle),
                    Ok(vxl_core::assembly::ProbeResult::Free) => a.place_free_with_vehicle(&def_id, cell, rot, vehicle),
                    Err(_) => Err(vxl_core::assembly::PlaceError::Overlap { cell }),
                }
            };
            match result {
                Ok(_) => {
                    // 松开放置成功 → 结束拿起
                    picked.module_id = None;
                    picked.hold_start = None;
                    picked.was_root = false;
                    hud.status = String::new();
                }
                Err(err) => {
                    // 放不下（重叠等）：保留在 picked，模块不丢（B1 修复）
                    hud.status = format!("无法放置: {:?}（ESC 取消拿起）", err);
                }
            }
        }
    }
}

/// 旋转（R）：拿起中旋转预览
fn rotate_picked(mut picked: ResMut<Picked>, keys: Res<ButtonInput<KeyCode>>) {
    if picked.module_id.is_some() {
        if keys.just_pressed(KeyCode::KeyR) {
            picked.rot = vxl_core::rotation::rotate_y_90_idx(picked.rot);
        } else if keys.just_pressed(KeyCode::KeyT) {
            picked.rot = vxl_core::rotation::rotate_x_90_idx(picked.rot);
        } else if keys.just_pressed(KeyCode::KeyF) {
            picked.rot = vxl_core::rotation::rotate_z_90_idx(picked.rot);
        }
    }
}

/// 预览：拿起时半透明 ghost 跟随鼠标
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
fn preview_follow(
    asm: Res<AsmRes>,
    mut commands: Commands,
    picked: Res<Picked>,
    mut hover: ResMut<HoverSnap>,
    mut hud: ResMut<HudState>,
    cursor: Res<CursorPos>,
    time: Res<Time>,
    camera: Query<(&Camera, &GlobalTransform)>,
    mut existing: Query<(Entity, &mut Transform), (With<PreviewEntity>, Without<PreviewGrid>)>,
    mut grids: Query<(Entity, &mut Transform), (With<PreviewGrid>, Without<PreviewEntity>)>,
    mut edge_grids: Query<(Entity, &mut Transform), (With<PreviewGridEdges>, Without<PreviewEntity>, Without<PreviewGrid>)>,
    mut meshes: ResMut<Assets<Mesh>>,
    mut mats: ResMut<Assets<StandardMaterial>>,
    mut caches: Local<PreviewCaches>,
) {
    let mut cur: Option<(Entity, Mut<Transform>)> = None;
    if let Some(item) = existing.iter_mut().next() { cur = Some(item); }
    let mut grid_cur: Option<(Entity, Mut<Transform>)> = None;
    if let Some(item) = grids.iter_mut().next() { grid_cur = Some(item); }
    let mut edge_cur: Option<(Entity, Mut<Transform>)> = None;
    if let Some(item) = edge_grids.iter_mut().next() { edge_cur = Some(item); }
    if picked.module_id.is_none() {
        if let Some((e, _)) = cur { commands.entity(e).despawn(); }
        if let Some((e, _)) = grid_cur { commands.entity(e).despawn(); }
        if let Some((e, _)) = edge_cur { commands.entity(e).despawn(); }
        hover.cell = None;
        hover.free_cell = None;
        hover.targets.clear();
        hover.fixed_targets.clear();
        hover.float_offset = Vec3::ZERO;
        hover.visual_pos = None;
        hud.status = String::new();
        return;
    }
    let Some(def) = asm.0.defs.get(&picked.def_id).cloned() else { return };
    let Some(cpos) = cursor.pos else { return };
    let Ok((cam, cam_tf)) = camera.single() else { return };
    let Some(hit) = ground_point(cam, cam_tf, cpos) else { return };
    // 落地高度取地形表面，与放置完全一致
    let cell = Vec3i(
        hit.x.floor() as i32,
        terrain_height(hit.x, hit.z).floor() as i32,
        hit.z.floor() as i32,
    );
    // 候选吸附优先：鼠标靠近载具挂点时就吸附到候选位置
    // 默认始终自由跟随鼠标；只有进入候选位置一格内才自动吸附，不需要辅助按键。
    let snap = pick_snap_feedback(&asm.0, &picked.def_id, picked.vehicle, cell, cpos, cam, cam_tf, (hover.cell, hover.rot));
    hover.cell = snap.as_ref().map(|s| s.cell);
    hover.rot = snap.as_ref().map(|s| s.rot).unwrap_or(picked.rot);
    hover.targets.clear();
    hover.fixed_targets.clear();
    let (cell, rot) = if let Some(s) = &snap {
        hover.free_cell = None;
        (s.cell, s.rot)
    } else {
        // 防穿模：鼠标指向已有模块时，ghost 显示在模块表面外
        let mut free = if let Ok(ray) = cam.viewport_to_world(cam_tf, cpos)
            && let Some((mid, p)) = ray_hit_module(&asm.0, ray)
            && let Some(md) = asm.0.modules.get(&mid) {
            push_out_of_module(md, p, *ray.direction)
        } else {
            cell
        };
        // 自由落点迟滞：上一格仍可放且只差 1 格时保持，避免边界瞬移
        if let Some(prev) = hover.free_cell
            && prev != free
            && (prev.0 - free.0).abs().max((prev.1 - free.1).abs()).max((prev.2 - free.2).abs()) <= 1
            && asm.0.probe_place_with_vehicle(&picked.def_id, prev, picked.rot, picked.vehicle).is_ok() {
            free = prev;
        }
        hover.free_cell = Some(free);
        (free, picked.rot)
    };
    // 候选没识别到但当前落点确实可吸附时补回 hover.cell，保证松手按真实落点放
    if hover.cell.is_none()
        && matches!(asm.0.probe_place_with_vehicle(&picked.def_id, cell, rot, picked.vehicle), Ok(vxl_core::assembly::ProbeResult::Snap)) {
        hover.cell = Some(cell);
        hover.rot = rot;
    }
    // 连接点显示目标载具 5 米内的外露边缘；不展示手持模块内部挂点。
    hover.targets = nearby_target_mounts(&asm.0, &def, def_center(&def, rot, cell), picked.vehicle);
    hover.fixed_targets = if let Some(c) = hover.cell {
        asm.0.snap_targets(&def.id, c, hover.rot, picked.vehicle).into_iter().collect()
    } else {
        std::collections::HashSet::new()
    };
    let rot_cells = rotated_cells(&def, rot);
    let dims = def_dims(&def, rot);
    let snapped_pos = def_center(&def, rot, cell);
    // 自由状态使用鼠标在地面上的连续坐标，不再被整数格锁住。
    let base_pos = def_center(&def, rot, cell);
    let free_pos = base_pos + Vec3::new(
        hit.x - hit.x.floor(),
        terrain_height(hit.x, hit.z) - cell.1 as f32,
        hit.z - hit.z.floor(),
    );
    let pos = if snap.is_some() { snapped_pos } else { free_pos };
    // 手持物品不贴地：按载具中线 + 拉远距离动态抬高，真正吸附时才固定
    let float_offset = if hover.cell.is_some() {
        Vec3::ZERO
    } else {
        let lift = if let Some(refc) = held_reference(&asm.0, picked.vehicle) {
            let dx = pos.x - refc.x;
            let dz = pos.z - refc.z;
            let dist = (dx * dx + dz * dz).sqrt();
            (refc.y + 0.8 + dist.clamp(0.0, 20.0) * 0.12).max(pos.y + 0.8)
        } else {
            pos.y + 1.2
        };
        Vec3::new(0.0, lift - pos.y, 0.0)
    };
    hover.float_offset = float_offset;
    let target_visual = pos + float_offset;
    // 50ms 左右完成跟随，减少拖动滞后；放置仍使用 hover.cell 的规则坐标。
    let blend = 1.0 - (-time.delta_secs() / 0.05).exp();
    let visual_pos = hover.visual_pos.map(|old| old.lerp(target_visual, blend)).unwrap_or(target_visual);
    hover.visual_pos = Some(visual_pos);
    // ghost 始终保持模块原色，状态只由框架/连接点表达
    let ghost_mat = if caches.ghost_mat.as_ref().map(|(c, _)| *c != def.category).unwrap_or(true) {
        let c = cat_color(def.category);
        let c = c.to_srgba();
        let m = mats.add(StandardMaterial {
            base_color: Color::srgba(c.red, c.green, c.blue, 0.82),
            unlit: true,
            alpha_mode: AlphaMode::Blend,
            ..default()
        });
        caches.ghost_mat.replace((def.category, m.clone()));
        m
    } else {
        caches.ghost_mat.as_ref().unwrap().1.clone()
    };
    let grid_idx = match asm.0.probe_place_with_vehicle(&picked.def_id, cell, rot, picked.vehicle) {
        Ok(vxl_core::assembly::ProbeResult::Snap) => {
            hud.status = "可吸附".into();
            0
        }
        Ok(vxl_core::assembly::ProbeResult::Free) => {
            hud.status = "可散落放置".into();
            0
        }
        Err(_) => {
            hud.status = "不可放置".into();
            1
        }
    };
    let mat = ghost_mat;
    if let Some((e, mut tf)) = cur {
        tf.translation = visual_pos;
        if caches.mesh.as_ref().map(|(cells, _)| *cells != rot_cells).unwrap_or(true) {
            let center = cells_aabb(&rot_cells).2;
            let local = cell_centers_for(&rot_cells, center);
            let mesh = meshes.add(cells_mesh(&local));
            commands.entity(e).insert(Mesh3d(mesh.clone()));
            caches.mesh.replace((rot_cells.clone(), mesh));
        }
        commands.entity(e).insert(MeshMaterial3d(mat));
    } else {
        let mesh = if let Some((_, m)) = caches.mesh.as_ref().filter(|(cells, _)| *cells == rot_cells) {
            m.clone()
        } else {
            let center = cells_aabb(&rot_cells).2;
            let local = cell_centers_for(&rot_cells, center);
            let m = meshes.add(cells_mesh(&local));
            caches.mesh.replace((rot_cells.clone(), m.clone()));
            m
        };
        commands.spawn((PreviewEntity, Mesh3d(mesh), MeshMaterial3d(mat), Transform::from_translation(visual_pos)));
    }
    // 模块框架：八个顶角 + 12 条直线边（无斜线），始终包围本体跟随移动
    let grid_pos = visual_pos;
    let grid_dims = dims + Vec3::splat(0.08);
    // 框架颜色跟随放置状态：可放白色，不可放红色
    let grid_mat = match grid_idx {
        1 => caches.grid_mats[1].get_or_insert_with(|| mats.add(StandardMaterial { base_color: Color::srgb(1.0, 0.2, 0.2), unlit: true, ..default() })).clone(),
        _ => caches.grid_mats[0].get_or_insert_with(|| mats.add(StandardMaterial { base_color: Color::srgb(1.0, 1.0, 1.0), unlit: true, ..default() })).clone(),
    };
    if let Some((ge, mut gtf)) = grid_cur {
        gtf.translation = grid_pos;
        if caches.grid.as_ref().map(|(d, _)| *d != grid_dims).unwrap_or(true) {
            let mesh = meshes.add(corner_frame_mesh(grid_dims, 0.14));
            commands.entity(ge).insert(Mesh3d(mesh.clone()));
            caches.grid.replace((grid_dims, mesh));
        }
        commands.entity(ge).insert(MeshMaterial3d(grid_mat.clone()));
    } else {
        let mesh = if let Some((_, m)) = caches.grid.as_ref().filter(|(d, _)| *d == grid_dims) {
            m.clone()
        } else {
            let m = meshes.add(corner_frame_mesh(grid_dims, 0.14));
            caches.grid.replace((grid_dims, m.clone()));
            m
        };
        commands.spawn((PreviewGrid, Mesh3d(mesh), MeshMaterial3d(grid_mat.clone()), Transform::from_translation(grid_pos)));
    }
    if let Some((ee, mut etf)) = edge_cur {
        etf.translation = grid_pos;
        if caches.grid_edges.as_ref().map(|(d, _)| *d != grid_dims).unwrap_or(true) {
            let mesh = meshes.add(edge_frame_mesh(grid_dims));
            commands.entity(ee).insert(Mesh3d(mesh.clone()));
            caches.grid_edges.replace((grid_dims, mesh));
        }
        commands.entity(ee).insert(MeshMaterial3d(grid_mat));
    } else {
        let mesh = if let Some((_, m)) = caches.grid_edges.as_ref().filter(|(d, _)| *d == grid_dims) {
            m.clone()
        } else {
            let m = meshes.add(edge_frame_mesh(grid_dims));
            caches.grid_edges.replace((grid_dims, m.clone()));
            m
        };
        commands.spawn((PreviewGridEdges, Mesh3d(mesh), MeshMaterial3d(grid_mat), Transform::from_translation(grid_pos)));
    }
}

/// 装配变化时重建整车 compound；每辆车始终只有一个 Kinematic 刚体和一个 Collider。
fn sync_vehicle_bodies(
    asm: Res<AsmRes>,
    mut pending: ResMut<PendingPhysics>,
    mut commands: Commands,
    mut bodies: Query<(
        Entity,
        &VehicleBody,
        &mut Transform,
        &mut VehicleBodyShape,
        &mut LinearVelocity,
        &mut AngularVelocity,
    )>,
) {
    let vehicles: std::collections::HashSet<u32> = asm.0.modules.values()
        .map(|md| md.vehicle)
        .filter(|&vehicle| vehicle != 0)
        .collect();
    let mut existing = std::collections::HashMap::new();
    for (entity, body, _, _, _, _) in bodies.iter() { existing.insert(body.0, entity); }

    for vehicle in &vehicles {
        let Some(compound) = vehicle_compound_parts(&asm.0, *vehicle) else { continue };
        if let Some(&entity) = existing.get(vehicle) {
            let Ok((_, _, mut tf, mut shape, mut linear, mut angular)) = bodies.get_mut(entity) else { continue };
            if let Some(state) = pending.0.as_ref().and_then(|physics| physics.bodies.iter().find(|state| state.vehicle_id == *vehicle)) {
                tf.translation = Vec3::from_array(state.position);
                linear.0 = Vec3::from_array(state.linear_velocity);
                angular.0 = Vec3::from_array(state.angular_velocity);
            }
            commands.entity(entity).insert(RigidBody::Dynamic);
            // 只有核心装配中心变化时才瞬移刚体；物理引擎产生的下落位置不能每帧被覆盖。
            if shape.center.distance(compound.center) > 1e-4 {
                tf.translation = compound.center;
            }
            if shape.cells != compound.cells {
                commands.entity(entity).insert(Collider::compound(compound.parts));
                shape.cells = compound.cells;
            }
            shape.center = compound.center;
            shape.mass = compound.mass;
        } else {
            let physics = pending.0.as_ref().cloned().unwrap_or_default();
            let body_state = physics.bodies.iter().find(|state| state.vehicle_id == *vehicle);
            let position = if physics.position == [0.0; 3] { compound.center } else { Vec3::from_array(physics.position) };
            let position = body_state.map_or(position, |state| Vec3::from_array(state.position));
            commands.spawn((
                VehicleBody(*vehicle),
                VehicleBodyShape { cells: compound.cells, center: compound.center, mass: compound.mass },
                // 整车使用 Dynamic compound；core 只负责拓扑，位置和速度交给物理世界。
                RigidBody::Dynamic,
                LockedAxes::ROTATION_LOCKED,
                Collider::compound(compound.parts),
                Mass(compound.mass),
                LinearVelocity(body_state.map_or(Vec3::from_array(physics.linear_velocity), |state| Vec3::from_array(state.linear_velocity))),
                AngularVelocity(body_state.map_or(Vec3::from_array(physics.angular_velocity), |state| Vec3::from_array(state.angular_velocity))),
                LinearDamping(2.5),
                CollisionEventsEnabled,
                Transform::from_translation(position),
            ));
        }
    }
    for (entity, body, _, _, _, _) in bodies.iter() {
        if !vehicles.contains(&body.0) { commands.entity(entity).despawn(); }
    }
    if pending.0.is_some() { pending.0 = None; }
}

/// 目标载具连接点只画空心边缘：未拼接时小，对准吸附时平滑放大，不遮挡模块。
#[allow(clippy::too_many_arguments)]
fn mount_highlight(
    picked: Res<Picked>,
    hover: Res<HoverSnap>,
    time: Res<Time>,
    mut existing: Query<(Entity, &MountHighlight, &mut Transform, &mut MeshMaterial3d<StandardMaterial>)>,
    mut commands: Commands,
    mut meshes: ResMut<Assets<Mesh>>,
    mut hl_mesh: Local<Option<Handle<Mesh>>>,
    mut hl_mat_blue: Local<Option<Handle<StandardMaterial>>>,
    mut hl_mat_red: Local<Option<Handle<StandardMaterial>>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
) {
    let wanted: std::collections::HashSet<(Vec3i, Face)> = if picked.module_id.is_some() {
        hover.targets.iter().copied().collect()
    } else {
        std::collections::HashSet::new()
    };
    let mut alive: std::collections::HashSet<(Vec3i, Face)> = std::collections::HashSet::new();
    for (e, hl, mut tf, mut material) in existing.iter_mut() {
        if wanted.contains(&hl.key) {
            let (wc, face) = hl.key;
            let d = face.dir();
            tf.translation = Vec3::new(
                wc.0 as f32 + 0.5 + d.0 as f32 * 0.42,
                wc.1 as f32 + 0.5 + d.1 as f32 * 0.42,
                wc.2 as f32 + 0.5 + d.2 as f32 * 0.42,
            );
            let target_scale = if hover.fixed_targets.contains(&hl.key) { 1.25 } else { 0.55 };
            material.0 = if hover.fixed_targets.contains(&hl.key) {
                hl_mat_blue.get_or_insert_with(|| materials.add(StandardMaterial { base_color: Color::srgb(0.2, 0.7, 1.0), emissive: Color::srgb(0.1, 0.5, 1.0).into(), unlit: true, alpha_mode: AlphaMode::Blend, ..default() })).clone()
            } else {
                hl_mat_red.get_or_insert_with(|| materials.add(StandardMaterial { base_color: Color::srgb(1.0, 0.2, 0.15), emissive: Color::srgb(0.7, 0.05, 0.02).into(), unlit: true, alpha_mode: AlphaMode::Blend, ..default() })).clone()
            };
            let blend = 1.0 - (-14.0 * time.delta_secs()).exp();
            tf.scale = tf.scale.lerp(Vec3::splat(target_scale), blend);
            alive.insert(hl.key);
        } else {
            commands.entity(e).despawn();
        }
    }
    if wanted.is_empty() { return; }
    let mesh = hl_mesh.get_or_insert_with(|| meshes.add(edge_frame_mesh(Vec3::splat(0.46)))).clone();
    for key in wanted {
        if alive.contains(&key) { continue; }
        let (wc, face) = key;
        let d = face.dir();
        let p = Vec3::new(
            wc.0 as f32 + 0.5 + d.0 as f32 * 0.42,
            wc.1 as f32 + 0.5 + d.1 as f32 * 0.42,
            wc.2 as f32 + 0.5 + d.2 as f32 * 0.42,
        );
        let mat = if hover.fixed_targets.contains(&key) {
            hl_mat_blue.get_or_insert_with(|| materials.add(StandardMaterial { base_color: Color::srgb(0.2, 0.7, 1.0), emissive: Color::srgb(0.1, 0.5, 1.0).into(), unlit: true, alpha_mode: AlphaMode::Blend, ..default() })).clone()
        } else {
            hl_mat_red.get_or_insert_with(|| materials.add(StandardMaterial { base_color: Color::srgb(1.0, 0.2, 0.15), emissive: Color::srgb(0.7, 0.05, 0.02).into(), unlit: true, alpha_mode: AlphaMode::Blend, ..default() })).clone()
        };
        commands.spawn((
            MountHighlight { key },
            Mesh3d(mesh.clone()),
             MeshMaterial3d(mat),
            Transform::from_translation(p).with_scale(Vec3::splat(0.55)),
        ));
    }
}

/// 只让与手持模块实际接触的目标模块播放吸附脉冲；手持模块和同车其它模块固定不动。
fn connected_module_ripple(
    asm: Res<AsmRes>,
    picked: Res<Picked>,
    hover: Res<HoverSnap>,
    time: Res<Time>,
    mut modules: Query<(&ModuleEntity, &mut Transform)>,
) {
    let center = hover.cell.and_then(|cell| {
        let def = asm.0.defs.get(&picked.def_id)?;
        Some(def_center(def, hover.rot, cell))
    });
    let target_modules: std::collections::HashSet<u32> = if center.is_some() {
        hover.fixed_targets.iter().filter_map(|&(cell, _)| {
            let id = *asm.0.occupancy.get(&cell)?;
            (asm.0.modules.get(&id)?.vehicle != 0).then_some(id)
        }).collect()
    } else {
        std::collections::HashSet::new()
    };

    for (entity, mut tf) in modules.iter_mut() {
        if !target_modules.contains(&entity.0) {
            tf.scale = Vec3::ONE;
            continue;
        }
        let center = center.unwrap();
        let distance = tf.translation.distance(center);
        // 距离只改变相位，波峰从手持模块中心依次到达各个实际吸附目标。
        let phase = time.elapsed_secs() * 5.0 - distance * 1.8;
        let scale = 1.0 + phase.sin().max(0.0) * 0.08;
        tf.scale = Vec3::splat(scale);
    }
}

#[derive(Resource, Default)]
struct Selection { def_id: String }

#[derive(Component)]
struct HudText;

/// 数字键 1-4 选择模块类型
fn selection_system(
    asm: Res<AsmRes>,
    mut sel: ResMut<Selection>,
    mut picked: ResMut<Picked>,
    mut hud: ResMut<HudState>,
    keys: Res<ButtonInput<KeyCode>>,
) {
    let mut ids: Vec<String> = asm.0.defs.keys().cloned().collect();
    ids.sort();
    let mut changed = false;
    let mut new_def: Option<String> = None;
    if keys.just_pressed(KeyCode::Digit1) { new_def = ids.first().cloned(); }
    if keys.just_pressed(KeyCode::Digit2) { new_def = ids.get(1).cloned(); }
    if keys.just_pressed(KeyCode::Digit3) { new_def = ids.get(2).cloned(); }
    if keys.just_pressed(KeyCode::Digit4) { new_def = ids.get(3).cloned(); }
    if keys.just_pressed(KeyCode::Digit5) { new_def = ids.get(4).cloned(); }
    if keys.just_pressed(KeyCode::Digit6) { new_def = ids.get(5).cloned(); }
    if keys.just_pressed(KeyCode::Digit7) { new_def = ids.get(6).cloned(); }
    if keys.just_pressed(KeyCode::Digit8) { new_def = ids.get(7).cloned(); }
    if keys.just_pressed(KeyCode::Digit9) { new_def = ids.get(8).cloned(); }
    if let Some(did) = new_def {
        // 手上已有装配模块时不覆盖（先 ESC/放置）
        if picked.module_id.is_none() || picked.module_id == Some(PLACING_NEW) {
            sel.def_id = did.clone();
            picked.module_id = Some(PLACING_NEW);
            picked.def_id = did;
            picked.rot = 0;
            picked.vehicle = 0;
            picked.origin = None;
            picked.hold_start = None;
            picked.was_root = false;
            changed = true;
        }
    }
    if changed {
        hud.selected = sel.def_id.clone();
    }
}

/// S 保存 / L 加载（RON 存档，文件在当前目录）
fn save_load_system(
    mut asm: ResMut<AsmRes>,
    mut picked: ResMut<Picked>,
    keys: Res<ButtonInput<KeyCode>>,
    mut texts: Query<&mut Text, With<HudText>>,
    bodies: Query<(&VehicleBody, &Transform, &LinearVelocity, &AngularVelocity)>,
    mut pending: ResMut<PendingPhysics>,
) {
    if keys.just_pressed(KeyCode::KeyS)
        && let Ok(text) = ron::ser::to_string(&save_with_physics(&asm.0, &bodies))
        && std::fs::write("save.ron", text).is_ok() {
        for mut t in texts.iter_mut() {
            t.0 = "已保存 save.ron（S保存 / L加载）".into();
        }
    }
    if keys.just_pressed(KeyCode::KeyL) {
        // 加载前：手上的装配模块先放回，避免加载后消失
        if picked.module_id.is_some() && picked.module_id != Some(PLACING_NEW) {
            if let Some(origin) = picked.origin {
                let _ = asm.0.place_free_with_vehicle(&picked.def_id, origin, picked.rot, picked.vehicle);
            }
            picked.module_id = None;
            picked.hold_start = None;
            picked.was_root = false;
        }
        if let Ok(text) = std::fs::read_to_string("save.ron")
            && let Ok(data) = ron::from_str::<SaveData>(&text) {
            pending.0 = Some(data.physics.clone());
            match asm.0.load(data) {
                Ok(()) => {
                    for mut t in texts.iter_mut() {
                        t.0 = "已加载 save.ron（S保存 / L加载）".into();
                    }
                }
                Err(e) => {
                    for mut t in texts.iter_mut() {
                        t.0 = format!("加载失败: {}", e);
                    }
                }
            }
        }
    }
}

/// M3 整车驾驶：WASD 施加连续线速度，方向键施加转向速度。
/// 装配拓扑仍由 core 管理；物理位置不再被格子平移每帧覆盖。
fn vehicle_move_system(
    asm: Res<AsmRes>,
    cam: Res<CamState>,
    keys: Res<ButtonInput<KeyCode>>,
    time: Res<Time>,
    mut bodies: Query<(&VehicleBody, &Transform, &mut LinearVelocity, &mut AngularVelocity)>,
) {
    let Some(root) = asm.0.root else { return };
    let vehicle = asm.0.modules.get(&root).map(|m| m.vehicle).unwrap_or(0);
    if vehicle == 0 { return; }
    // 轮子驱动：没有轮子开不动，轮子越多速度越快
    let wheel_count = asm.0.modules.values().filter(|m| {
        asm.0.defs.get(&m.def_id).map(|d| d.category == Category::Wheel).unwrap_or(false)
    }).count();
    if wheel_count == 0 { return; }
    let speed = 2.5 + (wheel_count as f32 - 1.0).max(0.0) * 0.4;
    let forward = Vec3::new(cam.yaw.cos(), 0.0, cam.yaw.sin());
    let lateral = Vec3::new(-forward.z, 0.0, forward.x);
    let mut desired = Vec3::ZERO;
    if keys.pressed(KeyCode::KeyW) { desired += forward; }
    if keys.pressed(KeyCode::KeyS) { desired -= forward; }
    if keys.pressed(KeyCode::KeyA) { desired -= lateral; }
    if keys.pressed(KeyCode::KeyD) { desired += lateral; }
    if desired.length_squared() > 1.0 { desired = desired.normalize(); }
    let throttle = if keys.pressed(KeyCode::KeyW) { 1.0 } else if keys.pressed(KeyCode::KeyS) { -1.0 } else { 0.0 };
    let steering = if keys.pressed(KeyCode::ArrowLeft) { 1.0 } else if keys.pressed(KeyCode::ArrowRight) { -1.0 } else { 0.0 };
    let brake = if keys.pressed(KeyCode::Space) { 1.0 } else { 0.0 };
    for (body, tf, mut velocity, mut angular) in &mut bodies {
        if body.0 != vehicle { continue; }
        let blend = 1.0 - (-8.0 * time.delta_secs()).exp();
        let command = drive_command(
            DrivingInput { throttle, brake, steering },
            velocity.0.dot(*tf.forward()),
            speed,
            speed * 2.0,
            speed * 4.0,
        );
        let target = if desired.length_squared() > 0.0 { desired * command.target_speed } else { Vec3::ZERO };
        let current_velocity = velocity.0;
        velocity.0 = current_velocity + (target - current_velocity) * blend;
        if brake > 0.0 {
            velocity.0 -= current_velocity * (command.traction.abs() / (speed * 4.0)).clamp(0.0, 1.0) * blend;
        }
        angular.y = command.steering_angle * 3.0;
    }
}

/// 轮子悬挂与驱动：每个轮子独立向下探测地面，在轮位施加弹簧/阻尼力。
/// 前轮/后轮按纵向位置分配驱动力；四个及以上轮子自动四驱。
fn wheel_suspension_system(
    asm: Res<AsmRes>,
    keys: Res<ButtonInput<KeyCode>>,
    spatial: SpatialQuery,
    mut bodies: Query<(&VehicleBody, &Transform, &VehicleBodyShape, Forces)>,
) {
    let forward_input = if keys.pressed(KeyCode::KeyW) { 1.0 } else if keys.pressed(KeyCode::KeyS) { -1.0 } else { 0.0 };
    if forward_input == 0.0 { return; }
    for (body, tf, shape, mut forces) in &mut bodies {
        let wheels: Vec<Vec3> = asm.0.modules.values()
            .filter(|md| md.vehicle == body.0 && asm.0.defs.get(&md.def_id).is_some_and(|d| d.category == Category::Wheel))
            .flat_map(|md| md.cells.iter().copied())
            .map(|c| Vec3::new(c.0 as f32 + 0.5, c.1 as f32 + 0.5, c.2 as f32 + 0.5) - shape.center + tf.translation)
            .collect();
        if wheels.is_empty() { continue; }
        let front = wheels.iter().map(|p| p.z).fold(f32::NEG_INFINITY, f32::max);
        let rear = wheels.iter().map(|p| p.z).fold(f32::INFINITY, f32::min);
        let mode = drive_mode(wheels.len());
        for point in wheels {
            let Some(hit) = spatial.cast_ray(point, Dir3::NEG_Y, MAX_SUSPENSION_DISTANCE, true, &SpatialQueryFilter::default()) else { continue };
            let compression = (1.0 - hit.distance / MAX_SUSPENSION_DISTANCE).clamp(0.0, 1.0);
            let vertical_speed = forces.linear_velocity().y;
            let spring_force = suspension_force(compression, vertical_speed);
            let longitudinal = drive_fraction(mode, point.z, front, rear);
            let drive_force = forward_input * longitudinal * 850.0;
            let drive_dir = Vec3::new(1.0, 0.0, 0.0);
            forces.apply_force_at_point(Vec3::Y * spring_force + drive_dir * drive_force, point);
        }
    }
}

fn save_with_physics(
    asm: &Assembly,
    bodies: &Query<(&VehicleBody, &Transform, &LinearVelocity, &AngularVelocity)>,
) -> SaveData {
    let mut data = asm.save();
    let mut body_states = Vec::new();
    for (body, tf, linear, angular) in bodies.iter() {
        body_states.push(PhysicsBodyState::new(
            body.0,
            None,
            tf.translation.to_array(),
            linear.0.to_array(),
            angular.0.to_array(),
        ));
    }
    if let Some(first) = body_states.first() {
        data.physics = PhysicsState {
            position: first.position,
            linear_velocity: first.linear_velocity,
            angular_velocity: first.angular_velocity,
            fragment_ids: asm.detect_fragments(),
            driver_id: asm.root,
            bodies: body_states,
        };
    }
    data
}

/// M3 碰撞伤害：按参与碰撞的模块/载具代表模块施加伤害。
/// 载具碰撞使用 compound body，因而从 vehicle=1 的公开模块表选代表模块；
/// 同车模块之间则优先损伤连接强度。断裂出的 fragment 会在这里归零 vehicle。
fn collision_damage_system(
    mut asm: ResMut<AsmRes>,
    mut hud: ResMut<HudState>,
    mut events: MessageReader<CollisionStart>,
    modules: Query<(Entity, &ModuleEntity)>,
    vehicles: Query<(Entity, &VehicleBody)>,
    velocities: Query<&LinearVelocity>,
) {
    for ev in events.read() {
        let module_id = |entity| modules.iter().find(|(e, _)| *e == entity).map(|(_, m)| m.0);
        let vehicle_id = |entity| vehicles.iter().find(|(e, _)| *e == entity).map(|(_, b)| b.0);
        let mass_for = |entity| module_id(entity)
            .and_then(|id| asm.0.modules.get(&id))
            .and_then(|md| asm.0.defs.get(&md.def_id))
            .map(|def| def.mass)
            .or_else(|| vehicle_id(entity).map(|id| asm.0.vehicle_mass(id)));
        let (Some(mass1), Some(mass2)) = (mass_for(ev.collider1), mass_for(ev.collider2)) else { continue };
        let v1 = velocities.get(ev.collider1).map(|v| v.0.length()).unwrap_or(0.0);
        let v2 = velocities.get(ev.collider2).map(|v| v.0.length()).unwrap_or(0.0);
        let dmg = vxl_core::assembly::Assembly::impact_damage((v1 - v2).abs(), mass1, mass2);
        if dmg > 1.0 {
            let m1 = module_id(ev.collider1).or_else(|| vehicle_id(ev.collider1).and_then(|v| {
                asm.0.modules.iter().find(|(_, md)| md.vehicle == v).map(|(id, _)| *id)
            }));
            let m2 = module_id(ev.collider2).or_else(|| vehicle_id(ev.collider2).and_then(|v| {
                asm.0.modules.iter().find(|(_, md)| md.vehicle == v).map(|(id, _)| *id)
            }));
            let mut fragments = Vec::new();
            if let (Some(a), Some(b)) = (m1, m2) {
                let same_vehicle = asm.0.modules.get(&a).map(|m| m.vehicle != 0)
                    .unwrap_or(false)
                    && asm.0.modules.get(&a).map(|m| m.vehicle)
                        == asm.0.modules.get(&b).map(|m| m.vehicle);
                let result: Result<Vec<Vec<u32>>, ()> = if same_vehicle {
                    // 只有确实存在边时才走连接伤害；碰撞事件可能只命中
                    // 同一载具的 compound 代表，不能因此吞掉模块伤害。
                    match asm.0.damage_connection(a, b, dmg) {
                        Ok(out) => Ok(out),
                        Err(()) => {
                            let mut out = asm.0.damage_module(a, dmg).unwrap_or_default();
                            out.extend(asm.0.damage_module(b, dmg).unwrap_or_default());
                            Ok(out)
                        }
                    }
                } else {
                    let mut out = asm.0.damage_module(a, dmg).unwrap_or_default();
                    if b != a {
                        out.extend(asm.0.damage_module(b, dmg).unwrap_or_default());
                    }
                    Ok(out)
                };
                if let Ok(out) = result { fragments.extend(out); }
            }
            // damage_* returns module ids for detached connected components.
            // Marking through the public module map keeps fragments independent
            // without requiring a core-side vehicle reclassification helper.
            for fragment in fragments {
                for id in fragment {
                    if let Some(md) = asm.0.modules.get_mut(&id) { md.vehicle = 0; }
                }
            }
            hud.status = format!("碰撞伤害: {:.0}（{} vs {}）", dmg, mass1, mass2);
        }
    }
}

/// 启动 HUD
fn spawn_hud(mut commands: Commands, asset_server: Res<AssetServer>) {
    let font: Handle<Font> = asset_server.load("fonts/simhei.ttf");
    commands.spawn((
        HudText,
        Text::new(""),
        TextFont { font: FontSource::Handle(font), font_size: FontSize::Px(18.0), ..default() },
        TextColor(Color::WHITE),
        Node { position_type: PositionType::Absolute, top: Val::Px(8.0), left: Val::Px(8.0), ..default() },
    ));
}

/// 相机控制：跟随驾驶舱（root），右键拖拽旋转、Q/E 微调、+/- 距离
fn camera_control(
    asm: Res<AsmRes>,
    picked: Res<Picked>,
    mut cam: ResMut<CamState>,
    mut camera: Query<&mut Transform, With<Camera3d>>,
    buttons: Res<ButtonInput<MouseButton>>,
    keys: Res<ButtonInput<KeyCode>>,
    mut motion: MessageReader<MouseMotion>,
) {
    let Ok(mut tf) = camera.single_mut() else { return };
    // 右键拖拽旋转视角
    if picked.module_id.is_none() && buttons.pressed(MouseButton::Right) {
        for mv in motion.read() {
            cam.yaw -= mv.delta.x * 0.01;
            cam.pitch = (cam.pitch + mv.delta.y * 0.01).clamp(-0.25, 1.35);
        }
    } else {
        motion.clear();
    }
    if keys.pressed(KeyCode::KeyQ) { cam.yaw += 0.04; }
    if keys.pressed(KeyCode::KeyE) { cam.yaw -= 0.04; }
    let dist_step = if keys.pressed(KeyCode::Equal) { -0.7 } else if keys.pressed(KeyCode::Minus) { 0.7 } else { 0.0 };
    cam.dist = (cam.dist + dist_step).clamp(4.0, 32.0);

    // 目标点：root（驾驶舱）中心；无 root 时回到出生坐标
    let target = if let Some(rid) = asm.0.root && let Some(md) = asm.0.modules.get(&rid) {
        let (_, _, c) = module_aabb(md);
        c
    } else {
        Vec3::new(2.0, 0.5, 2.0)
    };
    let dir = Vec3::new(cam.yaw.cos() * cam.pitch.cos(), cam.pitch.sin(), cam.yaw.sin() * cam.pitch.cos());
    let pos = target + dir * cam.dist;
    tf.translation = pos;
    tf.look_to(target - pos, Vec3::Y);
}

/// 高压测试辅助日志：设置 VXF_DEBUG=1 后写入拿起/连接点/ghost 状态
#[allow(clippy::too_many_arguments)]
fn debug_log_system(
    picked: Res<Picked>,
    hover: Res<HoverSnap>,
    hud: Res<HudState>,
    buttons: Res<ButtonInput<MouseButton>>,
    time: Res<Time>,
    mut last: Local<f32>,
    preview: Query<&Transform, With<PreviewEntity>>,
    grids: Query<&Transform, With<PreviewGrid>>,
) {
    if std::env::var_os("VXF_DEBUG").is_none() { return; }
    let now = time.elapsed_secs();
    if now - *last < 0.15 { return; }
    *last = now;
    let p = preview.iter().next().map(|t| {
        format!("{:.2},{:.2},{:.2}", t.translation.x, t.translation.y, t.translation.z)
    }).unwrap_or_else(|| "-".into());
    let g = grids.iter().next().map(|t| {
        format!("{:.2},{:.2},{:.2}", t.translation.x, t.translation.y, t.translation.z)
    }).unwrap_or_else(|| "-".into());
    let line = format!(
        "t={:.2} picked={:?} def={} targets={} fixed={} snap={:?} status={} ghost={} frame={} lmb_press={} lmb_rel={} lmb_held={}\n",
        now, picked.module_id, picked.def_id, hover.targets.len(), hover.fixed_targets.len(), hover.cell, hud.status, p, g,
        buttons.just_pressed(MouseButton::Left),
        buttons.just_released(MouseButton::Left),
        buttons.pressed(MouseButton::Left),
    );
    use std::io::Write;
    let _ = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open("vxl_debug.log")
        .and_then(|mut f| f.write_all(line.as_bytes()));
}

/// HUD 分层渲染：操作提示 / 选中模块 / 放置状态
fn hud_render_system(hud: Res<HudState>, mut texts: Query<&mut Text, With<HudText>>) {
    let sel = if hud.selected.is_empty() { "未选择".to_string() } else { hud.selected.clone() };
    let status = if hud.status.is_empty() { "-".to_string() } else { hud.status.clone() };
    for mut t in texts.iter_mut() {
        t.0 = format!(
            "操作: WASD移动整车 / 方向键转向 / 数字1-9选择 / 左键拿起甩出 / R旋转 / ESC取消 / S保存 / L加载 / 右键转相机 / QE微调 / +/-缩放\n选中: {}\n状态: {}",
            sel, status
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn module_library_ron_parses() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../data/modules.ron");
        let text = std::fs::read_to_string(path).expect("data/modules.ron 不存在");
        let defs: Vec<ModuleDef> = ron::from_str(&text).expect("RON 模块库解析失败");
        assert!(!defs.is_empty());
        for d in &defs {
            d.validate().expect("模块定义校验失败");
        }
    }

    #[test]
    fn terrain_height_is_flat_ground() {
        assert_eq!(terrain_height(0.0, 0.0), 0.0);
        assert_eq!(terrain_height(20.0, 0.0), 0.0);
        assert_eq!(terrain_height(-15.0, 9.0), 0.0);
    }

    #[test]
    fn vehicle_compound_contains_all_vehicle_cells() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../data/modules.ron");
        let defs: Vec<ModuleDef> = ron::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let mut asm = Assembly::new();
        for def in defs { asm.register(def); }
        asm.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
        asm.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();

        let compound = vehicle_compound_parts(&asm, 1).unwrap();
        assert_eq!(compound.center, Vec3::new(1.0, 0.5, 0.5));
        assert_eq!(compound.parts.len(), 2);
        assert_eq!(compound.mass, 20.0);
        assert_eq!(compound.cells, vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0)]);
        assert!(vehicle_compound_parts(&asm, 99).is_none());
    }

    #[test]
    fn vehicle_compound_center_is_mass_weighted() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../data/modules.ron");
        let defs: Vec<ModuleDef> = ron::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let mut asm = Assembly::new();
        for def in defs { asm.register(def); }
        asm.place_root("nexus.cab", Vec3i(0, 0, 0), 0).unwrap();
        asm.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
        let compound = vehicle_compound_parts(&asm, 1).unwrap();
        assert!((compound.center.x - 0.75).abs() < 1e-5);
        assert_eq!(compound.mass, 40.0);
        assert_eq!(compound.parts.len(), 2);
    }

    #[test]
    fn demo_assembly_contains_cab_structure_and_wheels() {
        let mut asm = Assembly::new();
        build_demo_assembly(&mut asm).unwrap();
        assert_eq!(asm.modules.len(), 5);
        assert_eq!(asm.modules.values().filter(|m| m.vehicle != 0).count(), 5);
        assert_eq!(asm.occupancy.len(), 5);
    }

}
