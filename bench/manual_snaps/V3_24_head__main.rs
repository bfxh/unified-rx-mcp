#![windows_subsystem = "windows"]

//! VoxelForge-V3 渲染层（Bevy 0.19）——入口与系统装配。
//! 职责分模块：resources 共享类型 / setup 场景启动 / sync 装配→实体 /
//! pick 拾取放置预览 / vehicle 载具驾驶物理伤害 / debug 诊断日志。

mod debug;
mod geom;
mod pick;
mod resources;
mod setup;
mod sync;
mod terrain;
mod ui;
mod vehicle;
mod vehicle_physics;

pub(crate) use debug::*;
pub(crate) use geom::*;
pub(crate) use pick::*;
pub(crate) use resources::*;
pub(crate) use setup::*;
pub(crate) use sync::*;
pub(crate) use terrain::*;
pub(crate) use ui::*;
pub(crate) use vehicle::*;

use avian3d::prelude::*;
use bevy::prelude::*;
use vxl_core::assembly::Assembly;
fn main() {
    let Some(_instance_lock) = acquire_instance_lock() else {
        return;
    };
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
        .insert_resource(DamageAccumulator::default())
        .insert_resource(ToastState::default())
        .insert_resource(CategoryFilter::default())
        .insert_resource(CorpFilter::default())
        .insert_resource(ClassifyMode::Category)
        .insert_resource(SearchText::default())
        .insert_resource(SearchFocus::default())
        .insert_resource(TerrainToggle::default())
        .insert_resource(SettingsOpen::default())
        .insert_resource(ActiveVehicle::default())
        .insert_resource(VehicleMenu::default())
        .insert_resource(DebrisModules::default())
        .insert_resource(RockOccupied::default())
        .init_resource::<CursorPos>()
        .add_systems(
            Startup,
            (setup_scene, build_sky_and_light, setup_assembly, spawn_ui),
        )
        .add_systems(
            Update,
            (
                (
                    cursor_pos_system,
                    sync_entities,
                    sync_vehicle_bodies,
                    rotate_picked,
                    preview_follow,
                    mount_highlight,
                    connected_module_ripple,
                    pick_place,
                    selection_system,
                )
                    .chain(),
                (
                    save_load_system,
                    vehicle_freeze_system,
                    vehicle_move_system,
                    wheel_suspension_system,
                    wheel_spin_system,
                    collision_damage_system,
                    camera_control,
                    terrain_switch_system,
                    refresh_rock_terrain,
                    debug_log_system,
                )
                    .chain(),
                (
                    ui_search_focus_system,
                    ui_status_system,
                    ui_inventory_system,
                    ui_category_list_system,
                    ui_classify_switch_system,
                    ui_category_system,
                    ui_corp_system,
                    ui_search_system,
                    ui_item_click_system,
                    ui_item_scroll_system,
                    ui_tooltip_system,
                    ui_vehicle_menu_system,
                    ui_settings_system,
                    ui_terrain_buttons_system,
                    ui_vehicle_system,
                    ui_toast_system,
                ),
            ),
        )
        .run();
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::setup::{build_demo_assembly, load_module_defs};
    use crate::sync::vehicle_compound_parts;
    use vxl_core::module::{ModuleDef, Vec3i};

    #[test]
    fn module_library_ron_parses() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../data/modules.ron");
        let text = std::fs::read_to_string(path).expect("data/modules.ron 不存在");
        let defs: Vec<ModuleDef> =
            ron::from_str(text.trim_start_matches('\u{feff}')).expect("RON 模块库解析失败");
        assert!(!defs.is_empty());
        for d in &defs {
            d.validate().expect("模块定义校验失败");
        }
    }

    #[test]
    fn terrain_height_is_flat_ground() {
        // 默认起伏地形：高度非零且连续（平滑可驾驶）
        let h = terrain_height(0.0, 0.0);
        assert!(h.abs() < 6.0, "默认地形应在起伏范围内");
        // 相邻点坡度可控（约 8-12°，即斜率 < 0.25）
        let dx = terrain_height(1.0, 0.0) - terrain_height(0.0, 0.0);
        let dz = terrain_height(0.0, 1.0) - terrain_height(0.0, 0.0);
        assert!(
            dx.abs() < 0.3 && dz.abs() < 0.3,
            "坡度不可驾驶: dx={dx} dz={dz}"
        );
    }

    /// 读真实 data/modules.ron 的测试必须串行：并发的 fs/env 探测偶发触发
    /// load_module_defs 的 CWD 回退日志与断言污染（单跑恒过、全量偶挂的元凶）
    static DATA_TESTS_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    #[test]
    fn vehicle_compound_contains_all_vehicle_cells() {
        // 测试固定走仓库根 data/modules.ron（与下个测试同因）
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../data/modules.ron");
        let text = std::fs::read_to_string(path).expect("data/modules.ron 不存在");
        let defs: Vec<ModuleDef> =
            ron::from_str(text.trim_start_matches('\u{feff}')).expect("RON 模块库解析失败");
        let mut asm = Assembly::new();
        for def in defs {
            asm.register(def);
        }
        asm.place_root("nexus.b", Vec3i(0, 0, 0), 0)
            .expect("place_root nexus.b");
        asm.place("nexus.b", Vec3i(1, 0, 0), 0)
            .expect("place nexus.b");

        let _guard = DATA_TESTS_LOCK.lock();
        let compound = vehicle_compound_parts(&asm, 1).expect("compound parts v1");
        assert_eq!(compound.center, Vec3::new(1.0, 0.5, 0.5));
        assert_eq!(compound.parts.len(), 2);
        assert_eq!(compound.mass, 20.0);
        assert_eq!(compound.cells, vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0)]);
        assert!(vehicle_compound_parts(&asm, 99).is_none());
    }

    #[test]
    fn vehicle_compound_center_is_mass_weighted() {
        // 测试固定走仓库根 data/modules.ron（load_module_defs 的 CWD/exe 探测在 cargo test 下不一定命中）
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../data/modules.ron");
        let text = std::fs::read_to_string(path).expect("data/modules.ron 不存在");
        let defs: Vec<ModuleDef> =
            ron::from_str(text.trim_start_matches('\u{feff}')).expect("RON 模块库解析失败");
        let mut asm = Assembly::new();
        for def in defs {
            asm.register(def);
        }
        asm.place_root("nexus.cab", Vec3i(0, 0, 0), 0)
            .expect("place_root nexus.cab");
        asm.place("nexus.b", Vec3i(1, 0, 0), 0)
            .expect("place nexus.b");
        let _guard = DATA_TESTS_LOCK.lock();
        let compound = vehicle_compound_parts(&asm, 1).expect("compound parts v2");
        // cab30@(0.5)+b10@(1.5) → x=0.75；modules.ron 质量被改时这里会失败——正是测试要抓的回归
        assert!(
            (compound.center.x - 0.75).abs() < 1e-5,
            "center.x={}",
            compound.center.x
        );
        assert_eq!(compound.mass, 40.0);
        assert_eq!(compound.parts.len(), 2);
    }

    #[test]
    fn save_load_roundtrip_preserves_modules_and_edges() {
        let mut asm = Assembly::new();
        build_demo_assembly(&mut asm).unwrap();
        let module_count = asm.modules.len();
        let edge_count = asm.edges.len();
        let data = asm.save();
        // 存档 → RON 序列化 → 反序列化（模拟磁盘往返）
        let text = ron::to_string(&data).unwrap();
        let restored_data: vxl_core::assembly::SaveData = ron::from_str(&text).unwrap();
        let mut restored = Assembly::new();
        for d in load_module_defs() {
            restored.register(d);
        }
        restored.load(restored_data).unwrap();
        assert_eq!(restored.modules.len(), module_count);
        assert_eq!(restored.edges.len(), edge_count);
        assert_eq!(restored.root, asm.root);
        // 每个模块 def/vehicle 一致
        for (id, m) in &asm.modules {
            let rm = restored
                .modules
                .get(id)
                .unwrap_or_else(|| panic!("模块 {} 丢失", id));
            assert_eq!(rm.def_id, m.def_id);
            assert_eq!(rm.vehicle, m.vehicle);
            assert_eq!(rm.origin, m.origin);
        }
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
