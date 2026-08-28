//! 交互系统：拿起/放置/回位/旋转 + 预览跟随（Bevy 系统层）。

use bevy::prelude::*;
use crate::render_bridge::AsmRes;
use vf2_core::module::Vec3i;
use vf2_core::rotation::{RotMat, rotations_24};

use crate::input::{
    PickPreview, Picked, PickedModule, hit_module, mouse_ray, placement_target, resolve_placement_cell,
};
use crate::render_bridge::{ModuleRef, shape_dims};

/// 交互插件
pub struct InteractPlugin;

impl Plugin for InteractPlugin {
    fn build(&self, app: &mut App) {
        app.init_resource::<Picked>().add_systems(
            Update,
            // left_click 在前：拿起/放置单系统互斥（同帧不竞争——修复
            // "拿起即放"竞态：并行系统时 pick 拿起后 place 同帧又放置）
            // rotate_picked 先于 preview_follow：R 键改旋转当帧即见
            // （2026-08-18：旋转放在 preview 后=预览总读旧旋转=“旋转不了”）
            (left_click, putback, rotate_picked, preview_follow).chain(),
        );
    }
}

/// 左键完整状态机（单系统——拿起与放置天然互斥）：
///   Idle + 点击模块 → 拿起（记录原位置防丢）
///   Picked + 点击 → 放置到目标格（失败保持拿起）
/// Bevy 系统参数无法组合——9 个参数为系统签名固有（clippy: too_many_arguments）
#[allow(clippy::too_many_arguments)]
fn left_click(
    mouse: Res<ButtonInput<MouseButton>>,
    mut picked: ResMut<Picked>,
    mut asm: ResMut<AsmRes>,
    mut commands: Commands,
    window: Query<&Window>,
    camera: Query<(&Camera, &GlobalTransform), With<Camera3d>>,
    markers: Query<(Entity, &Transform, &ModuleRef), Without<PickPreview>>,
    lib_pick: Res<crate::inventory::LibraryPick>,
    fonts: Option<Res<crate::ui::font::UiFonts>>,
    ui_state: Res<crate::resources::inventory_state::InventoryState>,
    remove_mode: Res<crate::input_map::RemoveMode>,
    falling: Query<(), With<crate::physics::FallingBody>>,
    frag_entities: Query<(Entity, &ModuleRef)>,
) {
    // debugging-wizard P1（UI 穿透）：光标在 UI 上——点击不触发场景拿起
    if ui_state.cursor_over_ui {
        return;
    }
    // 库拿起（物品栏选中模块）时左键由 lib_place_or_cancel 处理——场景拿起让位
    if lib_pick.0.is_some() {
        return;
    }
    if !mouse.just_pressed(MouseButton::Left) {
        return;
    }
    let Ok(window) = window.single() else { return };
    let Ok((cam, cam_tf)) = camera.single() else { return };
    let Some(cursor) = window.cursor_position() else { return };
    let Some(ray) = mouse_ray(window, cam, cam_tf, cursor) else { return };
    // 拆除模式（X）：左键点击模块 = 直接移除（碎片坠落）
    if remove_mode.0 {
        let Some((mr, _, _)) = hit_module(ray, &markers) else { return };
        if let Ok(fragments) = asm.0.remove(mr.0) {
            info!("拆除模块 {:?}（{} 组碎片）", mr.0, fragments.len());
            commands.write_message(crate::audio::SfxMessage::Pick);
            // 碎片打 FallingBody 标记 → 受重力坠落
            crate::physics::mark_fragments(
                &mut commands, &asm, &fragments, &frag_entities, &falling,
            );
            if let Some(fonts) = fonts.as_ref() {
                crate::ui::island::spawn_island(&mut commands, fonts, "已拆除模块".to_string());
            }
        }
        return;
    }
    // 拿起中：左键 = 放置
    if let Some(pm) = picked.module.clone() {
        let hit = hit_module(ray, &markers);
        let Some((cell, outward)) = placement_target(ray, &asm.0, hit) else { return };
        // Overlap 候选格回退：目标格与装配重叠时沿命中面法线方向外移重试
        let cell = resolve_placement_cell(&asm.0, &pm.def_id, pm.rotation, cell, outward);
        match asm.0.place(&pm.def_id, cell, pm.rotation) {
            Ok(mid) => {
                info!("放置 {} @ {:?}", pm.def_id, cell);
                commands.write_message(crate::audio::SfxMessage::Place);
                // 放置闪光（生成动画由 sync_entities 统一插入 BuildFx）
                let dims = asm
                    .0
                    .defs
                    .get(&pm.def_id)
                    .map(|d| shape_dims(&d.shape))
                    .unwrap_or([1, 1, 1]);
                let rot = &rotations_24()[pm.rotation as usize];
                let mut center = crate::render_bridge::module_transform(cell, rot, dims).translation;
                // 2026-08-22 防穿模：闪光与模块同基高（module_render_base
                // 单点）——旧行为闪光在网格高度，模块贴地形 → 闪光错位
                center.y += crate::render_bridge::module_render_base(&asm.0, mid);
                crate::fx::spawn_place_light(&mut commands, center);
                // 灵动岛通知（放置反馈）
                if let Some(fonts) = fonts.as_ref() {
                    crate::ui::island::spawn_island(&mut commands, fonts, format!("已放置 · {}", pm.def_id));
                }
                picked.module = None;
            }
            Err(e) => {
                info!("放置失败（保持拿起）：{e:?}");
                commands.write_message(crate::audio::SfxMessage::PlaceFail);
            }
        }
        return;
    }

    // 空闲：点击模块 → 拿起
    let Some((mr, _, _)) = hit_module(ray, &markers) else { return };
    let Some(md) = asm.0.modules.get(mr.0) else { return };
    let pm = PickedModule {
        def_id: md.def_id.clone(),
        origin: md.origin,
        rotation: md.rotation,
        is_root: Some(mr.0) == asm.0.root,
    };
    if let Ok(fragments) = asm.0.remove(mr.0) {
        info!("拿起 {}（碎片 {} 组）", pm.def_id, fragments.len());
        commands.write_message(crate::audio::SfxMessage::Pick);
        // 断开成碎片的模块受重力坠落（拆桥拿起中间块→一侧掉落）
        crate::physics::mark_fragments(&mut commands, &asm, &fragments, &frag_entities, &falling);
        picked.module = Some(pm);
    }
}

/// 右键 / Esc：回原位（防丢铁律——root 用 place_root）
fn putback(
    mouse: Res<ButtonInput<MouseButton>>,
    keys: Res<ButtonInput<KeyCode>>,
    mut picked: ResMut<Picked>,
    mut asm: ResMut<AsmRes>,
    lib_pick: Res<crate::inventory::LibraryPick>,
    mut commands: Commands,
) {
    // 库拿起时右键/Esc 由 lib_place_or_cancel 取消——场景回位让位
    if lib_pick.0.is_some() {
        return;
    }
    let putback = mouse.just_pressed(MouseButton::Right) || keys.just_pressed(KeyCode::Escape);
    if !putback {
        return;
    }
    let Some(pm) = picked.module.clone() else { return };
    let r = if pm.is_root {
        asm.0.place_root(&pm.def_id, pm.origin, pm.rotation)
    } else {
        asm.0.place(&pm.def_id, pm.origin, pm.rotation)
    };
    match r {
        Ok(_) => {
            info!("放回原位 {}", pm.def_id);
            commands.write_message(crate::audio::SfxMessage::Putback);
            picked.module = None;
        }
        Err(e) => {
            warn!("回位失败（保持拿起）：{e:?}");
        }
    }
}

/// 预览跟随（拿起时 spawn 半透明模块，吸附目标格；无目标浮动）
/// Bevy 系统参数无法组合——9 个参数为系统签名固有（clippy: too_many_arguments）
#[allow(clippy::too_many_arguments)]
fn preview_follow(
    picked: Res<Picked>,
    mut commands: Commands,
    asm: Res<AsmRes>,
    mut meshes: ResMut<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
    window: Query<&Window>,
    camera: Query<(&Camera, &GlobalTransform), With<Camera3d>>,
    markers: Query<(Entity, &Transform, &ModuleRef), Without<PickPreview>>,
    mut preview_q: Query<(Entity, &mut Transform), With<PickPreview>>,
) {
    let Some(pm) = picked.module.as_ref() else {
        // 无拿起：清预览
        for (e, _) in preview_q.iter() {
            commands.entity(e).despawn();
        }
        return;
    };
    let Ok(window) = window.single() else { return };
    let Ok((cam, cam_tf)) = camera.single() else { return };
    let Some(cursor) = window.cursor_position() else { return };
    let Some(ray) = mouse_ray(window, cam, cam_tf, cursor) else { return };
    let hit = hit_module(ray, &markers);
    let target = placement_target(ray, &asm.0, hit);
    // 预览吸附格 = Overlap 候选格回退后的格（与点击放置同一逻辑——所见即所得）
    let target_cell = target
        .map(|(cell, outward)| resolve_placement_cell(&asm.0, &pm.def_id, pm.rotation, cell, outward));
    let def = asm.0.defs.get(&pm.def_id);
    let dims = def.map(|d| shape_dims(&d.shape)).unwrap_or([1, 1, 1]);
    let rot = &rotations_24()[pm.rotation as usize];
    // 2026-08-22 防穿模：预览与最终渲染同基高。
    // 放置后模块与整车连通 → 渲染用整车基高（module_render_base）——
    // 预览必须取 max(整车基高, 新模块自己 footprint 基高)，否则预览
    // 与实际放置错位（预览穿地/漂浮）。
    let ghost_base = target_cell
        .map(|_| {
            let own = asm
                .0
                .defs
                .get(&pm.def_id)
                .map(|d| d.shape.local_cells())
                .unwrap_or_default()
                .iter()
                .map(|lc| {
                    let wc = rot.apply_to_coord(*lc) + target_cell.unwrap_or(Vec3i(0, 0, 0));
                    (wc.0, wc.2)
                })
                .collect::<Vec<_>>();
            let mut b = crate::terrain::module_base(&own);
            if let Some(root) = asm.0.root {
                b = b.max(crate::render_bridge::module_render_base(&asm.0, root));
            }
            b
        })
        .unwrap_or(0.0);

    if let Ok((e, mut tf)) = preview_q.single_mut() {
        // 已有预览：更新位置 + 旋转（R 键旋转同步——2026-08-18 修复：
        // 此前只更新 translation，旋转改了预览不动=“旋转不了”）
        if let Some(cell) = target_cell {
            let center = crate::render_bridge::module_transform(cell, rot, dims);
            tf.translation = center.translation;
            tf.translation.y += ghost_base;
            tf.rotation = center.rotation;
        } else {
            // 无吸附目标：浮动在鼠标前方（同步旋转——悬浮时按 R 同样可见）
            let p = ray.0 + ray.1 * 4.0;
            tf.translation = Vec3::new(p.x, p.y.max(0.5), p.z);
            tf.rotation = crate::render_bridge::rot_to_quat(rot);
        }
        let _ = e;
    } else {
        // 首次拿起：spawn 预览（半透明 + 线框感）
        // 绿色投影（2026-08-21 用户裁决）：货物（collectible）悬浮在底座
        // （base tag）上时预览变绿——"可堆在这"提示
        let mesh = meshes.add(Cuboid::new(0.95, 0.95, 0.95));
        let on_base = def
            .map(|d| vf2_core::baseplay::is_collectible(d))
            .unwrap_or(false)
            && hit
                .and_then(|(mr, _, _)| asm.0.modules.get(mr.0))
                .and_then(|md| asm.0.defs.get(&md.def_id))
                .map(|hd| vf2_core::baseplay::is_base(hd))
                .unwrap_or(false);
        let mat = materials.add(StandardMaterial {
            base_color: if on_base {
                Color::srgba(0.4, 0.9, 0.5, 0.5) // 绿色：可堆基底
            } else {
                Color::srgba(0.5, 0.8, 1.0, 0.45) // 默认蓝色
            },
            alpha_mode: bevy::material::AlphaMode::Blend,
            unlit: true,
            ..default()
        });
        let t = crate::render_bridge::module_transform(
            target_cell.unwrap_or(Vec3i(0, 0, 0)),
            rot,
            dims,
        );
        commands.spawn((
            PickPreview,
            Mesh3d(mesh),
            MeshMaterial3d(mat),
            Transform {
                translation: if target_cell.is_some() {
                    t.translation + Vec3::Y * ghost_base
                } else {
                    let p = ray.0 + ray.1 * 4.0;
                    Vec3::new(p.x, p.y.max(0.5), p.z)
                },
                rotation: t.rotation,
                scale: t.scale,
            },
        ));
    }
}

/// R 键：旋转拿起中的模块（绕 Y +90°，与 core 旋转表一致；搜索框持焦时屏蔽）
fn rotate_picked(
    keys: Res<ButtonInput<KeyCode>>,
    mut picked: ResMut<Picked>,
    state: Res<crate::resources::inventory_state::InventoryState>,
) {
    if state.search_active {
        return;
    }
    if !keys.just_pressed(KeyCode::KeyR) {
        return;
    }
    let Some(pm) = picked.module.as_mut() else { return };
    // 绕 Y +90°：用旋转表找（当前 up=Top、fwd 旋转后 = fwd 的 -X 方向？）
    // 简化：rot24 + 3（绕 Y 90° = 索引 +3？不——用矩阵组合：
    // 新旋转 = R_y90 ∘ 当前（先当前后 y90）
    let cur = rotations_24()[pm.rotation as usize];
    let ry = RotMat([[0, 0, 1], [0, 1, 0], [-1, 0, 0]]); // 绕 Y +90°（X→-Z）
    let new = ry.compose(&cur);
    // 找索引
    if let Some(i) = rotations_24().iter().position(|m| *m == new) {
        pm.rotation = i as u8;
        info!("旋转 → rot {}", pm.rotation);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use bevy::input::ButtonState;
    use bevy::input::keyboard::{Key, KeyboardInput};
    use bevy::input::mouse::MouseButtonInput;
    use bevy::ecs::system::SystemState;
    use bevy::window::{CursorMoved, Window, WindowResolution};
    use vf2_core::assembly::Assembly;
    use vf2_core::module::{Category, Face, ModuleDef, MountMask, MountPoint, Shape};

    /// 注入一次左键点击（窗口 + 光标 + 按下/抬起）。
    /// headless 无 winit——手动 set_cursor_position（真实游戏由 winit 更新）
    fn click(app: &mut App, pos: Vec2) {
        let mut wq = app.world_mut().query::<Entity>();
        let win_entity = wq.iter(app.world()).find(|e| {
            app.world().entity(*e).contains::<Window>()
        }).unwrap();
        app.world_mut()
            .entity_mut(win_entity)
            .get_mut::<Window>()
            .unwrap()
            .set_cursor_position(Some(pos));
        app.world_mut().write_message(CursorMoved {
            window: win_entity,
            position: pos,
            delta: None,
        });
        for st in [ButtonState::Pressed, ButtonState::Released] {
            app.world_mut().write_message(MouseButtonInput {
                button: MouseButton::Left,
                state: st,
                window: win_entity,
            });
        }
    }

    /// 相机固定 (10,8,10)→(1,0,0)：世界点 → 屏幕像素
    /// 纯数学投影（headless 无 viewport——手动 1280×720 屏幕）
    fn to_screen(app: &mut App, world: Vec3) -> Vec2 {
        let mut cs = SystemState::<Query<(&Camera, &GlobalTransform)>>::new(app.world_mut());
        let items = cs.get(app.world()).unwrap();
        let (cam, cam_tf) = items.single().unwrap();
        let view = cam_tf.to_matrix().inverse();
        let proj = cam.clip_from_view();
        let clip = proj * view * world.extend(1.0);
        let ndc = clip.truncate() / clip.w;
        Vec2::new((ndc.x * 0.5 + 0.5) * 1280.0, (1.0 - (ndc.y * 0.5 + 0.5)) * 720.0)
    }

    /// 组装测试 app：相机 + 窗口 + 模块实体（渲染位置）+ 装配
    fn test_app(modules: &[(Vec3i, u8)]) -> (App, crate::render_bridge::ModuleRef) {
        let mut app = App::new();
        app.init_resource::<bevy::ecs::message::Messages<crate::audio::SfxMessage>>(); // 音效消息（P0）
        app.add_plugins((
            MinimalPlugins,
            bevy::input::InputPlugin,
            bevy::window::WindowPlugin {
                primary_window: Some(Window {
                    resolution: WindowResolution::new(1280, 720),
                    ..default()
                }),
                ..default()
            },
        ));
        app.init_resource::<Picked>();
        app.init_resource::<crate::inventory::LibraryPick>();
        app.init_resource::<crate::resources::inventory_state::InventoryState>();
        app.init_resource::<crate::input_map::RemoveMode>();
        app.add_systems(Update, (left_click, putback));

        // 装配：root + 模块们（先于首次 update——系统参数校验需要资源存在）
        let mut asm = Assembly::new();
        asm.register(def());
        let mut ids = vec![asm.place_root("nexus.b", modules[0].0, modules[0].1).unwrap()];
        for (c, r) in &modules[1..] {
            ids.push(asm.place("nexus.b", *c, *r).unwrap());
        }
        app.insert_resource(AsmRes(asm));

        // 相机（headless 无 Transform 传播——GlobalTransform 直接由 Transform 构造）
        let cam_e = app.world_mut().spawn((
            Camera3d::default(),
            Camera::default(),
            GlobalTransform::from(
                Transform::from_xyz(10.0, 8.0, 10.0).looking_at(Vec3::new(1.0, 0.0, 0.0), Vec3::Y),
            ),
        )).id();
        // 注入真实 ReverseZ 投影（实机由 camera_system 计算；headless 手动设置）：
        // 锁定"点击命中"与实机同一矩阵路径——回归"射线远平面 z_ndc=0 逆变换
        // w=0 → mouse_ray 永远 None → 左键点不了"bug。
        use bevy::camera::{CameraProjection, PerspectiveProjection};
        // 注意：Bevy 0.18 的 get_clip_from_view 用无限远 ReverseZ 投影（far 被忽略）
        let proj = PerspectiveProjection {
            fov: std::f32::consts::FRAC_PI_4,
            aspect_ratio: 1280.0 / 720.0,
            near: 0.1,
            ..Default::default()
        };
        app.world_mut().entity_mut(cam_e).get_mut::<Camera>().unwrap().computed.clip_from_view =
            proj.get_clip_from_view();
        app.update();

        // 渲染实体（模块位置 = module_transform；ModuleRef 映射各自 id——拿起才准确）
        for ((cell, rot), id) in modules.iter().zip(ids.iter()) {
            let t = crate::render_bridge::module_transform(*cell, &rotations_24()[*rot as usize], [1, 1, 1]);
            app.world_mut().spawn((
                ModuleRef(*id),
                Transform::from_translation(t.translation),
                GlobalTransform::from(Transform::from_translation(t.translation)),
            ));
        }
        app.update();
        (app, ModuleRef(ids[0]))
    }

    /// 回归测试（"拿起即放"竞态）：单击模块 → 拿起成功且同帧不放置
    #[test]
    fn test_left_click_pick_not_placed_same_frame() {
        let (mut app, _) = test_app(&[(Vec3i(0, 0, 0), 0), (Vec3i(1, 0, 0), 0)]);
        let pos = to_screen(&mut app, Vec3::new(1.0, 0.5, 0.5)); // (1,0,0) 模块中心
        click(&mut app, pos);
        app.update();
        let picked = app.world().resource::<Picked>();
        assert!(picked.module.is_some(), "单击模块必须拿起（拿起不被同帧放置）");
        let asm = app.world().resource::<AsmRes>();
        assert_eq!(asm.0.len(), 1, "拿起后装配剩 1（未被同帧放回/放置）");
    }

    /// 拿起 → 再点击相邻空位 → 放置成功（Picked 清空）
    #[test]
    fn test_left_click_place_after_pick() {
        let (mut app, _) = test_app(&[
            (Vec3i(0, 0, 0), 0),
            (Vec3i(1, 0, 0), 0),
            (Vec3i(2, 0, 0), 0),
        ]);
        // 拿起 (1,0,0)
        let pos = to_screen(&mut app, Vec3::new(1.0, 0.5, 0.5));
        click(&mut app, pos);
        app.update();
        assert!(app.world().resource::<Picked>().module.is_some());
        // 拿起后同步实体（模拟 sync_entities——headless 无渲染系统）
        {
            let mut qs = SystemState::<Query<(Entity, &Transform, &ModuleRef), Without<PickPreview>>>::new(app.world_mut());
            let q = qs.get(app.world()).unwrap();
            let mut gone = vec![];
            for (e, t, _) in q.iter() {
                if t.translation == Vec3::new(1.5, 0.5, 0.5) {
                    gone.push(e);
                }
                let _ = t;
            }
            for e in gone {
                app.world_mut().despawn(e);
            }
        }
        // 再点 (1,0.5,0.5)（窗口内）：拿起后该实体已 despawn，射线命中
        // (0,0,0) 模块 → 目标 (0,0,1)（邻 root）→ 放置成功
        let pos2 = to_screen(&mut app, Vec3::new(1.0, 0.5, 0.5));
        click(&mut app, pos2);
        app.update();
        let picked = app.world().resource::<Picked>();
        assert!(picked.module.is_none(), "放置成功后应清空拿起");
        let asm = app.world().resource::<AsmRes>();
        assert_eq!(asm.0.len(), 3, "放置成功：装配恢复 3");
    }

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

    /// 拿起 → 放置失败（无连接格）→ 模块不丢（保持拿起）→ 回位成功
    #[test]
    fn test_pick_place_fail_putback_no_loss() {
        let mut asm = Assembly::new();
        asm.register(def());
        asm.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
        let root = asm.root.unwrap();
        let md = asm.modules[root].clone();
        // 模拟拿起（pick_input 成功路径：remove + 记录）
        let pm = PickedModule {
            def_id: md.def_id.clone(),
            origin: md.origin,
            rotation: md.rotation,
            is_root: true,
        };
        asm.remove(root).unwrap();
        let mut picked = Picked { module: Some(pm) };

        // 放置失败：装配已空（root 被移除）→ place 拒绝（RootExists——1.x 语义）
        let r = asm.place(&picked.module.as_ref().unwrap().def_id, Vec3i(50, 0, 50), 0);
        assert!(matches!(r, Err(vf2_core::assembly::PlaceError::RootExists)), "空装配放置应拒绝：{r:?}");
        assert!(picked.module.is_some(), "放置失败后必须保持拿起（防丢）");
        assert_eq!(asm.len(), 0, "放置失败后装配不变");

        // 回位（防丢铁律：右键/Esc → 回原位；root 用 place_root）
        let pm = picked.module.take().unwrap();
        let r = if pm.is_root {
            asm.place_root(&pm.def_id, pm.origin, pm.rotation)
        } else {
            asm.place(&pm.def_id, pm.origin, pm.rotation)
        };
        r.unwrap();
        assert_eq!(asm.len(), 1, "回位后装配恢复");
        assert!(asm.modules.iter().any(|(_, m)| m.origin == Vec3i(0, 0, 0)));
    }

    /// 拿起 → 放置成功（相邻格）→ 模块落地
    #[test]
    fn test_pick_place_success() {
        let mut asm = Assembly::new();
        asm.register(def());
        asm.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
        // 场景：载具有两个模块（root + 相邻），拿起一个放旁边
        asm.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
        // 拿起 (1,0,0) 的模块
        let mid2 = asm
            .modules
            .iter()
            .find(|(_, m)| m.origin == Vec3i(1, 0, 0))
            .map(|(id, _)| id)
            .unwrap();
        let md2 = asm.modules[mid2].clone();
        let pm2 = PickedModule {
            def_id: md2.def_id.clone(),
            origin: md2.origin,
            rotation: md2.rotation,
            is_root: false,
        };
        asm.remove(mid2).unwrap();
        // 放置到 (2,0,0)（邻 root 的 (1,0,0) 已空——(2,0,0) 邻 (1,0,0)？空——邻 (0,0,0)？否——
        // 放 (1,0,0) 原位重新放回（相邻 root）→ 成功
        asm.place(&pm2.def_id, Vec3i(1, 0, 0), pm2.rotation).unwrap();
        assert_eq!(asm.len(), 2, "放置成功：装配恢复 2 模块");
    }

    /// R 键旋转：拿起后 rot24 +90°（X→-Z 语义）
    #[test]
    fn test_rotate_picked_90deg() {
        let mut app = App::new();
        app.add_plugins((MinimalPlugins, bevy::input::InputPlugin));
        app.init_resource::<Picked>();
        app.init_resource::<crate::resources::inventory_state::InventoryState>();
        app.add_systems(Update, rotate_picked);
        // 手动设置拿起（rot 0）
        app.world_mut().resource_mut::<Picked>().module = Some(PickedModule {
            def_id: "nexus.b".into(),
            origin: Vec3i(0, 0, 0),
            rotation: 0,
            is_root: true,
        });
        // 注入 R 键
        use bevy::input::ButtonState;
            for st in [ButtonState::Released, ButtonState::Pressed] {
            app.world_mut().write_message(KeyboardInput {
                key_code: KeyCode::KeyR,
                logical_key: Key::Character("r".into()),
                state: st,
                text: None,
                window: Entity::PLACEHOLDER,
                repeat: false,
            });
        }
        app.update();
        let rot = app.world().resource::<Picked>().module.as_ref().unwrap().rotation;
        // rot0 的 up=Top fwd=North；绕 Y +90° 后 fwd 应 = West（-X 前）？
        // 验证：新旋转的 fwd = R_y90·(0,0,-1) = (-1,0,0) = West 方向
        let m = rotations_24()[rot as usize];
        let fwd = m.apply_to_coord(Vec3i(0, 0, -1));
        assert_eq!(fwd, Vec3i(-1, 0, 0), "绕 Y +90° 后前向应朝 -X（West）");
    }

    /// 回归（2026-08-18 用户"旋转旋转不了"）：R 旋转后 preview_follow 必须把
    /// 新旋转同步到预览实体——此前只更新 translation，旋转改了预览不动。
    #[test]
    fn test_preview_rotation_follows_picked() {
        let mut app = App::new();
        app.add_plugins((
            MinimalPlugins,
            bevy::input::InputPlugin,
            bevy::asset::AssetPlugin::default(),
            bevy::window::WindowPlugin {
                primary_window: Some(Window {
                    resolution: WindowResolution::new(1280, 720),
                    ..default()
                }),
                ..default()
            },
        ));
        app.init_resource::<Picked>();
        app.init_resource::<crate::resources::inventory_state::InventoryState>();
        app.insert_resource(crate::render_bridge::AsmRes(Assembly::new()));
        use bevy::asset::AssetApp;
        app.init_asset::<Mesh>().init_asset::<StandardMaterial>();
        app.add_systems(Update, (rotate_picked, preview_follow).chain());
        // 相机（headless 手动 GlobalTransform）
        let cam_e = app.world_mut().spawn((
            Camera3d::default(),
            Camera::default(),
            GlobalTransform::from(
                Transform::from_xyz(10.0, 8.0, 10.0).looking_at(Vec3::new(1.0, 0.0, 0.0), Vec3::Y),
            ),
        )).id();
        use bevy::camera::{CameraProjection, PerspectiveProjection};
        let proj = PerspectiveProjection {
            fov: std::f32::consts::FRAC_PI_4,
            aspect_ratio: 1280.0 / 720.0,
            near: 0.1,
            ..Default::default()
        };
        app.world_mut().entity_mut(cam_e).get_mut::<Camera>().unwrap().computed.clip_from_view =
            proj.get_clip_from_view();
        // 拿起模块（rot 0）+ 预置预览实体（rot 1 的旋转——验证被覆盖同步）
        app.world_mut().resource_mut::<Picked>().module = Some(PickedModule {
            def_id: "nexus.b".into(),
            origin: Vec3i(0, 0, 0),
            rotation: 0,
            is_root: false,
        });
        // headless 无 winit——手动设置光标（preview_follow 需要 cursor_position）
        {
            let mut wq = app.world_mut().query::<Entity>();
            let win_entity = wq.iter(app.world()).find(|e| {
                app.world().entity(*e).contains::<Window>()
            }).unwrap();
            app.world_mut()
                .entity_mut(win_entity)
                .get_mut::<Window>()
                .unwrap()
                .set_cursor_position(Some(Vec2::new(640.0, 360.0)));
        }
        app.world_mut().spawn((
            PickPreview,
            Transform::from_rotation(Quat::from_rotation_y(1.0)), // 任意旧旋转
            GlobalTransform::default(),
        ));
        app.update(); // preview_follow 首帧：已有预览 → 同步位置+旋转（rot 0）
        let rot0 = app.world_mut().query_filtered::<&Transform, With<PickPreview>>()
            .single(app.world())
            .unwrap()
            .rotation;
        // 按 R → 旋转到 rot24[3]（绕 Y +90°）
        for st in [ButtonState::Released, ButtonState::Pressed] {
            app.world_mut().write_message(KeyboardInput {
                key_code: KeyCode::KeyR,
                logical_key: Key::Character("r".into()),
                state: st,
                text: None,
                window: Entity::PLACEHOLDER,
                repeat: false,
            });
        }
        app.update(); // rotate_picked 改 rotation → preview_follow 同步
        let rot1 = app.world_mut().query_filtered::<&Transform, With<PickPreview>>()
            .single(app.world())
            .unwrap()
            .rotation;
        let expected = crate::render_bridge::rot_to_quat(&RotMat([[0, 0, 1], [0, 1, 0], [-1, 0, 0]]));
        assert!(
            (rot1 - expected).length() < 1e-3,
            "预览旋转未跟随 R（rot0={rot0:?} rot1={rot1:?} 期望={expected:?}）"
        );
    }
}
