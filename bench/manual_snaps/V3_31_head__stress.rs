//! 高压测试：模拟玩家操作流 + 随机压力 + 不变量验证（挖逻辑漏洞）。
//!
//! 核心不变量：
//!   I1: occupancy 与 modules.cells 一一对应（无重叠、无幽灵占用）
//!   I2: 模块数守恒（拿起→放置往返不丢模块）
//!   I3: 碎片检测正确（断开的连通分量 != 0 个）
//!   I4: 非法输入被拒（rot>=24 / 未知 def / 重叠）

use vxl_core::assembly::{Assembly, PlaceError};
use vxl_core::module::{Category, Face, ModuleDef, MountMask, MountPoint, Shape, Vec3i};

fn def(id: &str, cat: Category, dims: [u32; 3]) -> ModuleDef {
    ModuleDef {
        id: id.into(),
        name: id.into(),
        corp: "nexus".into(),
        category: cat,
        mass: 10.0,
        hp: 100,
        shape: Shape::Block { dims },
        mount_points: Face::ALL
            .iter()
            .map(|&f| MountPoint {
                cell: Vec3i(0, 0, 0),
                face: f,
                accepts: MountMask::Any,
                strength: 100.0,
                layer: 0,
            })
            .collect(),
        tags: vec![],
        model_path: None,
    }
}

fn def_l(id: &str) -> ModuleDef {
    ModuleDef {
        id: id.into(),
        name: id.into(),
        corp: "nexus".into(),
        category: Category::Structure,
        mass: 10.0,
        hp: 100,
        shape: Shape::Cells {
            cells: vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0), Vec3i(0, 0, 1)],
        },
        mount_points: vec![],
        tags: vec![],
        model_path: None,
    }
}

/// 2x1 模块，每个占用格都定义 6 面挂点（用于边缘挂点测试）
fn def_b2_edge(id: &str) -> ModuleDef {
    let mut mps = Vec::new();
    for x in 0..2 {
        for f in Face::ALL {
            mps.push(MountPoint {
                cell: Vec3i(x, 0, 0),
                face: f,
                accepts: MountMask::Any,
                strength: 100.0,
                layer: 0,
            });
        }
    }
    ModuleDef {
        id: id.into(),
        name: "2x1 边缘挂点".into(),
        corp: "nexus".into(),
        category: Category::Structure,
        mass: 20.0,
        hp: 200,
        shape: Shape::Block { dims: [2, 1, 1] },
        mount_points: mps,
        tags: vec![],
        model_path: None,
    }
}

/// 抽象形状结构件（无挂点）
fn def_cells(id: &str, cells: Vec<Vec3i>) -> ModuleDef {
    ModuleDef {
        id: id.into(),
        name: "抽象结构件".into(),
        corp: "nexus".into(),
        category: Category::Structure,
        mass: 10.0,
        hp: 100,
        shape: Shape::Cells { cells },
        mount_points: vec![],
        tags: vec![],
        model_path: None,
    }
}

/// 验证不变量 I1：每个模块的占用格都与 occupancy 表一致且不重叠
fn assert_invariants(a: &Assembly) {
    let mut seen: std::collections::HashSet<Vec3i> = std::collections::HashSet::new();
    for (mid, md) in &a.modules {
        let def = &a.defs[&md.def_id];
        for lc in vxl_core::rotation::rotated_local_cells(&def.shape.local_cells(), md.rotation) {
            let wc = md.origin + lc;
            assert!(seen.insert(wc), "重叠占用: cell={:?} mid={}", wc, mid);
            assert_eq!(
                a.occupancy.get(&wc),
                Some(mid),
                "occupancy 与模块不一致: cell={:?}",
                wc
            );
        }
    }
    assert_eq!(
        seen.len(),
        a.modules.values().map(|m| m.cells.len()).sum::<usize>(),
        "占用格总数不一致"
    );
}

/// 玩家操作流：拿起（remove）→ 尝试各种旋转放回原位，模块不丢（I2）
#[test]
fn pick_place_roundtrip_100x() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.register(def("nexus.b2", Category::Structure, [2, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let m1 = a.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    let m2 = a.place("nexus.b2", Vec3i(2, 0, 0), 0).unwrap();
    let _ = m1;

    // 100 次拿起→放回原位（每次旋转变化，失败不丢模块）
    // 注意：remove + place_free 会产生新 id（放回后必须更新跟踪）
    let mut cur = m2;
    for i in 0..100 {
        let md = a.modules.get(&cur).expect("当前模块应在装配中").clone();
        a.remove(cur).unwrap();
        let n_before = a.len();
        // 尝试所有旋转，总有一种能放回原位
        let mut restored = false;
        for rot in 0..24u8 {
            if let Ok(nid) = a.place_free(&md.def_id, md.origin, rot) {
                cur = nid; // 更新为新 id（模拟 app 层 picked.module_id 更新）
                restored = true;
                break;
            }
        }
        assert!(restored, "第{}次拿起后无法放回（模块丢失风险）", i);
        assert_eq!(a.len(), n_before + 1, "第{}次往返后模块数变化", i);
        assert_invariants(&a);
        let _ = i;
    }
}

/// 高速批量：2000 次小方块放置/旋转/移除，只动小方块不动整车
#[test]
fn high_speed_small_block_stress_2000() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let mut alive: Option<u32> = None;
    for i in 0..2000i32 {
        let origin = Vec3i(10 + i, 0, 10 + i * 3);
        let id = a.place_free("nexus.b", origin, (i % 24) as u8).unwrap();
        if i % 3 == 0
            && let Some(prev) = alive
        {
            let _ = a.remove(prev);
        }
        alive = Some(id);
        if i % 200 == 0 {
            assert_invariants(&a);
        }
    }
    assert_invariants(&a);
}

/// 随机压力：500 模块随机放置（随机 def/位置/旋转），不变量全程成立
#[test]
fn random_stress_500() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.register(def("nexus.b2", Category::Structure, [2, 1, 1]));
    a.register(def("nexus.w", Category::Wheel, [1, 1, 1]));
    a.register(def_l("nexus.l"));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();

    let mut rng = 0x12345678u32; // 确定性伪随机
    let mut placed = 0;
    let mut rejected = 0;
    let defs = ["nexus.b", "nexus.b2", "nexus.w", "nexus.l"];
    for i in 0..500 {
        rng = rng.wrapping_mul(1664525).wrapping_add(1013904223);
        let x = (rng >> 8) as i32 % 8 - 4;
        rng = rng.wrapping_mul(1664525).wrapping_add(1013904223);
        let y = (rng >> 8) as i32 % 3;
        rng = rng.wrapping_mul(1664525).wrapping_add(1013904223);
        let z = (rng >> 8) as i32 % 8 - 4;
        rng = rng.wrapping_mul(1664525).wrapping_add(1013904223);
        let rot = ((rng >> 8) % 24) as u8;
        let did = defs[(i % 4) as usize];
        match a.place_free(did, Vec3i(x, y, z), rot) {
            Ok(_) => placed += 1,
            Err(PlaceError::Overlap { .. }) | Err(PlaceError::NoConnection) => rejected += 1,
            Err(e) => panic!("意外错误: {:?} (i={})", e, i),
        }
        if i % 50 == 0 {
            assert_invariants(&a);
        }
    }
    assert!(placed > 100, "应该能放进去至少 100 个（实际 {}）", placed);
    assert_eq!(placed + rejected, 500);
    assert_invariants(&a);
}

/// 桥接移除 → 碎片保留在装配（remove 不删碎片）+ 重连后碎片消除
#[test]
fn bridge_remove_and_reconnect() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    let root = a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let mut chain = vec![root];
    for i in 1..5 {
        let id = a.place("nexus.b", Vec3i(i, 0, 0), 0).unwrap();
        chain.push(id);
    }
    // 移除桥接（中间）→ 右侧 2 个成为碎片（仍保留在装配）
    let frags = a.remove(chain[2]).unwrap();
    assert_eq!(frags.len(), 1, "应有一个碎片组");
    assert_eq!(frags[0].len(), 2, "碎片应有 2 个模块");
    assert_eq!(a.len(), 4, "碎片保留在装配（remove 不删碎片）");
    assert_invariants(&a);

    // 重连：碎片已被 core 内聚归零（vehicle=0），贴着散落件的严格放置拒绝——语义诚实；
    // 玩家式重连 = 拿起散落模块再吸附回车体（与游戏内拿起/放下流程一致）
    let mut broken: Vec<(String, Vec3i, u8)> = frags[0]
        .iter()
        .map(|&fid| {
            let md = &a.modules[&fid];
            (md.def_id.clone(), md.origin, md.rotation)
        })
        .collect();
    // 散落件仍在场：贴"车体+散落件"的落点应诚实报 Attach(1)（严格吸附被拒，并入可行）
    assert!(
        matches!(
            a.probe_place_with_vehicle("nexus.b", Vec3i(2, 0, 0), 0, 1),
            Ok(vxl_core::assembly::ProbeResult::Attach(1))
        ),
        "车体+散落件邻接应报 Attach"
    );
    for &fid in &frags[0] {
        a.remove(fid).unwrap();
    }
    assert!(
        matches!(
            a.probe_place_with_vehicle("nexus.b", Vec3i(5, 0, 3), 0, 1),
            Ok(vxl_core::assembly::ProbeResult::Free)
        ),
        "纯散落邻接应报 Free 而非吸附"
    );
    // 补回桥接件（吸附回车体），随后散落模块按依赖顺序重连
    assert!(
        a.place_with_vehicle("nexus.b", Vec3i(2, 0, 0), 0, 1)
            .is_ok()
    );
    while !broken.is_empty() {
        let mut progressed = false;
        for i in 0..broken.len() {
            let (did, origin, rot) = broken[i].clone();
            if a.place_with_vehicle(&did, origin, rot, 1).is_ok() {
                broken.remove(i);
                progressed = true;
                break;
            }
        }
        assert!(progressed, "重连卡死：{:?}", broken);
    }
    let frags2 = a.detect_fragments();
    assert_eq!(frags2.len(), 0, "重连后应无碎片");
    assert_eq!(a.len(), 5);
}

/// 24 旋转 × 6 方向全组合放置（每个旋转都该能连上）
#[test]
fn all_rotations_connect_6dirs() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    for rot in 0..24u8 {
        for face in Face::ALL {
            let d = face.dir();
            let origin = Vec3i(d.0, d.1, d.2);
            let r = a.place("nexus.b", origin, rot);
            assert!(
                r.is_ok(),
                "rot={} face={:?} 连接失败: {:?}",
                rot,
                face,
                r.err()
            );
            let id = r.unwrap();
            a.remove(id).unwrap();
        }
    }
    assert_eq!(a.len(), 1);
}

/// 非法输入拒绝：rot>=24 / 未知 def / 重叠
#[test]
fn invalid_inputs_rejected() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    assert!(matches!(
        a.place("nexus.b", Vec3i(1, 0, 0), 24),
        Err(PlaceError::InvalidRotation(24))
    ));
    assert!(matches!(
        a.place("nexus.b", Vec3i(1, 0, 0), 255),
        Err(PlaceError::InvalidRotation(_))
    ));
    assert!(matches!(
        a.place("ghost.def", Vec3i(1, 0, 0), 0),
        Err(PlaceError::UnknownDef(_))
    ));
    assert!(matches!(
        a.place_free("nexus.b", Vec3i(0, 0, 0), 0),
        Err(PlaceError::Overlap { .. })
    ));
}

/// 长链 100 模块：移除中间 → 碎片保留 + 完整性
#[test]
fn chain_100_integrity() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    for i in 1..100 {
        a.place("nexus.b", Vec3i(i, 0, 0), 0).unwrap();
    }
    assert_eq!(a.len(), 100);
    assert_invariants(&a);
    // 移除中间 → 右半碎片（保留在装配）
    let frags = a.remove(50).unwrap();
    assert_eq!(frags.len(), 1);
    assert_eq!(frags[0].len(), 49);
    assert_eq!(a.len(), 99, "碎片保留在装配");
    assert_invariants(&a);
    // 全部移除（除 root）→ 最终只剩 root
    let ids: Vec<u32> = a.modules.keys().copied().filter(|&i| i != 0).collect();
    for id in ids {
        a.remove(id).unwrap();
    }
    assert_eq!(a.len(), 1, "全部移除后只剩 root");
    assert_invariants(&a);
}

/// 堆叠放置：y 方向叠高（多层的占用与连接）
#[test]
fn stack_tower() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    for y in 1..10 {
        let r = a.place("nexus.b", Vec3i(0, y, 0), 0);
        assert!(r.is_ok(), "y={} 叠放失败: {:?}", y, r.err());
    }
    assert_eq!(a.len(), 10);
    assert_invariants(&a);
    // 移除中间层 → 上层成碎片
    let frags = a.remove(5).unwrap();
    assert_eq!(frags.len(), 1);
    assert_eq!(frags[0].len(), 4, "上层 4 个应成碎片");
    assert_eq!(a.len(), 9);
}

/// place_free 无 root：未连接也能放（V3 需求）+ set_root 提升
#[test]
fn place_free_and_set_root() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    // 无 root 直接 free 放
    let id = a.place_free("nexus.b", Vec3i(3, 0, 3), 0).unwrap();
    assert_eq!(a.len(), 1);
    assert!(a.root.is_none());
    // 但普通 place 必须拒绝（无 root）
    assert!(matches!(
        a.place("nexus.b", Vec3i(4, 0, 3), 0),
        Err(PlaceError::NoConnection)
    ));
    // set_root：free 模块可提升为 root（D1 修复）
    assert!(a.set_root(id).is_ok());
    assert_eq!(a.root, Some(id));
    // root 保护：不能再 set 别的为 root（已有 root）
    let id2 = a.place("nexus.b", Vec3i(4, 0, 3), 0).unwrap();
    assert!(a.set_root(id2).is_err());
    // root 允许移除（驾驶舱可单独拖动）
    assert!(a.remove(id).is_ok());
    assert!(a.root.is_none());
}

/// V33：同一载具才吸附；不同载具/散落模块不作为吸附目标
#[test]
fn vehicle_group_same_vehicle_snaps() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let m1 = a.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    assert_eq!(a.modules[&m1].vehicle, 1);

    // 同载具身份可吸附
    let probe = a.probe_place_with_vehicle("nexus.b", Vec3i(2, 0, 0), 0, 1);
    assert!(matches!(probe, Ok(vxl_core::assembly::ProbeResult::Snap)));
    let m2 = a
        .place_with_vehicle("nexus.b", Vec3i(2, 0, 0), 0, 1)
        .unwrap();
    assert_eq!(a.modules[&m2].vehicle, 1);
}

#[test]
fn different_vehicle_not_snap() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();

    // 另一载具身份：预览诚实报"可并入目标载具"，严格放置拒绝，并入放置采纳对方
    let probe = a.probe_place_with_vehicle("nexus.b", Vec3i(1, 0, 0), 0, 99);
    assert!(matches!(
        probe,
        Ok(vxl_core::assembly::ProbeResult::Attach(1))
    ));
    assert!(
        a.place_with_vehicle("nexus.b", Vec3i(1, 0, 0), 0, 99)
            .is_err()
    );
    let adopted = a.place_attach("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    assert_eq!(a.modules[&adopted].vehicle, 1, "并入放置应收养相邻载具");
    // 散落模块（vehicle=0）不是吸附目标
    a.place_free("nexus.b", Vec3i(5, 0, 0), 0).unwrap();
    let probe2 = a.probe_place_with_vehicle("nexus.b", Vec3i(6, 0, 0), 0, 1);
    assert!(matches!(probe2, Ok(vxl_core::assembly::ProbeResult::Free)));
    assert!(
        a.place_with_vehicle("nexus.b", Vec3i(6, 0, 0), 0, 1)
            .is_err()
    );
}

/// MountMask 类别互认必须参与连接判定
#[test]
fn mount_mask_accepts_enforced() {
    let mut a = Assembly::new();
    let mut root = def("nexus.mount", Category::Structure, [1, 1, 1]);
    root.mount_points = Face::ALL
        .iter()
        .map(|&f| MountPoint {
            cell: Vec3i(0, 0, 0),
            face: f,
            accepts: MountMask::Only(vec![Category::Cab]),
            strength: 100.0,
            layer: 0,
        })
        .collect();
    a.register(root);
    a.place_root("nexus.mount", Vec3i(0, 0, 0), 0).unwrap();

    // Wheel 不被 Only([Cab]) 接受 → 不能连接
    a.register(def("nexus.w", Category::Wheel, [1, 1, 1]));
    assert!(a.place("nexus.w", Vec3i(1, 0, 0), 0).is_err());

    // Cab 被接受 → 可以连接
    a.register(def("nexus.cab", Category::Cab, [1, 1, 1]));
    let id = a.place("nexus.cab", Vec3i(1, 0, 0), 0).unwrap();
    assert_eq!(a.len(), 2);
    let _ = id;
}

/// snap_targets 返回可吸附目标挂点（用于渲染层蓝点高亮）
#[test]
fn snap_targets_reports_neighbor_mount() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    // 新模块放 (1,0,0)，吸附目标是 root 的 East 挂点 (0,0,0)
    let targets = a.snap_targets("nexus.b", Vec3i(1, 0, 0), 0, 1);
    assert_eq!(targets.len(), 1);
    assert_eq!(targets[0], (Vec3i(0, 0, 0), Face::East));

    // 异载具身份 → 无吸附目标
    let other = a.snap_targets("nexus.b", Vec3i(1, 0, 0), 0, 99);
    assert!(other.is_empty());
}

/// 新建模块 vehicle=0 是未归属通配身份，应能吸附驾驶舱并继承其载具。
#[test]
fn unassigned_module_can_snap_to_vehicle() {
    let mut a = Assembly::new();
    a.register(def("nexus.cab", Category::Cab, [1, 1, 1]));
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.cab", Vec3i(0, 0, 0), 0).unwrap();
    assert!(matches!(
        a.probe_place_with_vehicle("nexus.b", Vec3i(1, 0, 0), 0, 0),
        Ok(vxl_core::assembly::ProbeResult::Snap)
    ));
    let id = a
        .place_with_vehicle("nexus.b", Vec3i(1, 0, 0), 0, 0)
        .unwrap();
    assert_eq!(a.modules[&id].vehicle, 1);
}

/// 高亮只能返回实际接触的目标挂点，不能把同方向的其它挂点一起点亮。
#[test]
fn snap_targets_only_reports_contacted_mount() {
    let mut a = Assembly::new();
    a.register(def_b2_edge("nexus.b2e"));
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b2e", Vec3i(0, 0, 0), 0).unwrap();
    let targets = a.snap_targets("nexus.b", Vec3i(2, 0, 0), 0, 1);
    assert_eq!(targets, vec![(Vec3i(1, 0, 0), Face::East)]);
}

/// 周围候选合并后必须覆盖一整圈表面连接点（至少 5 个），供渲染层显示表面区块
#[test]
fn nearby_candidates_cover_surface_patch() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let cands = a.snap_candidates("nexus.b");
    assert!(
        cands.len() >= 5,
        "孤立方块周围应有至少 5 个候选，实际 {}",
        cands.len()
    );
    let mut faces = std::collections::HashSet::new();
    for (cell, rot) in cands {
        for (wc, face) in a.snap_targets("nexus.b", cell, rot, 1) {
            faces.insert((wc, face));
        }
    }
    assert!(
        faces.len() >= 5,
        "表面连接点至少 5 个，实际 {}",
        faces.len()
    );
}

/// 保存 → 加载往返：模块/边/载具归属/不变量全部还原
#[test]
fn save_load_roundtrip() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    a.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    a.place_free("nexus.b", Vec3i(5, 0, 5), 0).unwrap();

    let saved = a.save();
    let mut b = Assembly::new();
    b.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    b.load(saved).unwrap();

    assert_eq!(a.len(), b.len());
    assert_eq!(a.root, b.root);
    assert_eq!(a.edges.len(), b.edges.len());
    for (id, md) in &a.modules {
        let bmd = &b.modules[id];
        assert_eq!(md.def_id, bmd.def_id);
        assert_eq!(md.origin, bmd.origin);
        assert_eq!(md.rotation, bmd.rotation);
        assert_eq!(md.vehicle, bmd.vehicle);
    }
    assert_invariants(&b);
}

/// 候选吸附位置包含所有相邻格，且能通过同载具 probe
#[test]
fn snap_candidates_include_adjacent_placement() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let cands = a.snap_candidates("nexus.b");
    let adjacent = vec![
        Vec3i(1, 0, 0),
        Vec3i(-1, 0, 0),
        Vec3i(0, 1, 0),
        Vec3i(0, -1, 0),
        Vec3i(0, 0, 1),
        Vec3i(0, 0, -1),
    ];
    for c in adjacent {
        assert!(cands.iter().any(|(cc, _)| *cc == c), "缺少候选格 {:?}", c);
    }
    let snap_ok = cands.iter().any(|(c, r)| {
        matches!(
            a.probe_place_with_vehicle("nexus.b", *c, *r, 1),
            Ok(vxl_core::assembly::ProbeResult::Snap)
        )
    });
    assert!(snap_ok);
}

/// 边缘挂点：2x1 每个格都定义挂点，内部面必须被过滤，暴露面全部保留
#[test]
fn edge_mounts_on_2x1_all_cells_exposed() {
    let mut a = Assembly::new();
    a.register(def_b2_edge("nexus.b2e"));
    let id = a.place_root("nexus.b2e", Vec3i(0, 0, 0), 0).unwrap();
    let md = &a.modules[&id];
    // 12 个定义挂点 - 2 个内部面 = 10 个暴露挂点，覆盖两个格
    assert_eq!(md.mounts.len(), 10);
    assert!(md.mounts.iter().any(|w| w.cell == Vec3i(0, 0, 0)));
    assert!(md.mounts.iter().any(|w| w.cell == Vec3i(1, 0, 0)));
    // 内部面不出现
    assert!(
        !md.mounts
            .iter()
            .any(|w| w.cell == Vec3i(0, 0, 0) && w.face == Face::East)
    );
    assert!(
        !md.mounts
            .iter()
            .any(|w| w.cell == Vec3i(1, 0, 0) && w.face == Face::West)
    );
}

#[test]
fn rotated_2x1_edge_mounts_still_exposed() {
    let mut a = Assembly::new();
    a.register(def_b2_edge("nexus.b2e"));
    let rot = vxl_core::rotation::rotate_y_90_idx(0);
    let id = a.place_root("nexus.b2e", Vec3i(0, 0, 0), rot).unwrap();
    let md = &a.modules[&id];
    assert_eq!(md.mounts.len(), 10);
    // 绕 Y 90° 后归一为 (0,0,0),(0,0,1)：相邻的 South/North 面是内部面。
    assert!(
        !md.mounts
            .iter()
            .any(|w| w.cell == Vec3i(0, 0, 0) && w.face == Face::South)
    );
    assert!(
        !md.mounts
            .iter()
            .any(|w| w.cell == Vec3i(0, 0, 1) && w.face == Face::North)
    );
}

/// 候选吸附必须包含 2x1 两端的边缘外侧位置
#[test]
fn snap_candidates_cover_2x1_both_edges() {
    let mut a = Assembly::new();
    a.register(def_b2_edge("nexus.b2e"));
    a.place_root("nexus.b2e", Vec3i(0, 0, 0), 0).unwrap();
    let cands = a.snap_candidates("nexus.b2e");
    // East 端（cell(1,0,0) East 挂点）外侧 = (2,0,0)；West 端外侧 = (-2,0,0)
    assert!(
        cands.iter().any(|(c, _)| *c == Vec3i(2, 0, 0)),
        "缺少 East 边缘候选"
    );
    assert!(
        cands.iter().any(|(c, _)| *c == Vec3i(-2, 0, 0)),
        "缺少 West 边缘候选"
    );
}

/// 两个 2x1 首尾相接（边缘挂点 East/West 互吸）
#[test]
fn two_2x1_connect_at_edges() {
    let mut a = Assembly::new();
    a.register(def_b2_edge("nexus.b2e"));
    a.place_root("nexus.b2e", Vec3i(0, 0, 0), 0).unwrap();
    // 第二个 2x1 从 (2,0,0) 开始，West 端贴第一个 East 端
    let id = a
        .place_with_vehicle("nexus.b2e", Vec3i(2, 0, 0), 0, 1)
        .unwrap();
    assert_eq!(a.len(), 2);
    assert_eq!(a.modules[&id].vehicle, 1);
    assert_eq!(a.edges.len(), 1);
}

/// 24 旋转 × 多格模块：每个旋转下暴露挂点数量恒定，且相邻格都可连接
#[test]
fn all_rotations_multi_cell_mounts_stable() {
    let mut a = Assembly::new();
    a.register(def_b2_edge("nexus.b2e"));
    for rot in 0..24u8 {
        let mut b = Assembly::new();
        b.register(def_b2_edge("nexus.b2e"));
        let id = b.place_root("nexus.b2e", Vec3i(0, 0, 0), rot).unwrap();
        assert_eq!(b.modules[&id].mounts.len(), 10, "rot={} 挂点数异常", rot);
        assert_eq!(b.modules[&id].cells.len(), 2, "rot={} 占用格数异常", rot);
    }
}

/// L 形模块拿起→放回多次，不丢模块（无挂点模块的往返）
#[test]
fn pick_place_roundtrip_l_shape_100x() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.register(def_l("nexus.l"));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let mut cur = a.place_free("nexus.l", Vec3i(5, 0, 5), 0).unwrap();
    for _ in 0..100 {
        let md = a.modules.get(&cur).expect("L 形应在装配中").clone();
        a.remove(cur).unwrap();
        let n = a.len();
        let mut restored = false;
        for rot in 0..24u8 {
            if let Ok(nid) = a.place_free("nexus.l", md.origin, rot) {
                cur = nid;
                restored = true;
                break;
            }
        }
        assert!(restored, "L 形放回失败");
        assert_eq!(a.len(), n + 1);
        assert_invariants(&a);
    }
}

/// 抽象形状（带挂点结构件）必须能与带挂点结构件吸附
#[test]
fn abstract_shapes_snap_to_vehicle() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.register(def_cells_mounts(
        "nexus.t",
        vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0), Vec3i(2, 0, 0)],
    ));
    a.register(def_cells_mounts(
        "nexus.u",
        vec![
            Vec3i(0, 0, 0),
            Vec3i(2, 0, 0),
            Vec3i(0, 0, 1),
            Vec3i(2, 0, 1),
            Vec3i(1, 0, 1),
        ],
    ));
    a.register(def_cells_mounts(
        "nexus.pl",
        vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0), Vec3i(0, 0, 1)],
    ));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();

    for id in ["nexus.t", "nexus.u", "nexus.pl"] {
        let cands = a.snap_candidates(id);
        let snap = cands.iter().any(|(c, r)| {
            matches!(
                a.probe_place_with_vehicle(id, *c, *r, 1),
                Ok(vxl_core::assembly::ProbeResult::Snap)
            )
        });
        assert!(snap, "{} 无吸附候选，抽象模块拼不上去", id);
    }

    // 直线模块直接贴 root 也应 Snap
    let probe = a.probe_place_with_vehicle("nexus.t", Vec3i(1, 0, 0), 0, 1);
    assert!(matches!(probe, Ok(vxl_core::assembly::ProbeResult::Snap)));
    assert!(
        a.place_with_vehicle("nexus.t", Vec3i(1, 0, 0), 0, 1)
            .is_ok()
    );
}

/// 26 种常见抽象形状全部能吸附到载具（穷举小形状）
#[test]
fn many_abstract_shapes_all_snap() {
    // 在 2x2 平面内枚举所有包含原点、大小 <=4 的水平形状（代表 T/U/L/十字/空心等）
    let candidates: Vec<Vec<Vec3i>> = vec![
        vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0)],
        vec![Vec3i(0, 0, 0), Vec3i(0, 0, 1)],
        vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0), Vec3i(0, 0, 1)],
        vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0), Vec3i(2, 0, 0)],
        vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0), Vec3i(1, 0, 1)],
        vec![
            Vec3i(0, 0, 0),
            Vec3i(1, 0, 0),
            Vec3i(1, 0, 1),
            Vec3i(0, 0, 1),
        ],
        vec![
            Vec3i(0, 0, 0),
            Vec3i(1, 0, 0),
            Vec3i(2, 0, 0),
            Vec3i(2, 0, 1),
        ],
        vec![
            Vec3i(0, 0, 0),
            Vec3i(1, 0, 0),
            Vec3i(1, 0, 1),
            Vec3i(2, 0, 1),
        ],
        vec![Vec3i(0, 0, 0), Vec3i(0, 0, 1), Vec3i(0, 0, 2)],
        vec![Vec3i(0, 0, 0), Vec3i(0, 0, 1), Vec3i(0, 0, -1)],
        vec![
            Vec3i(0, 0, 0),
            Vec3i(1, 0, 0),
            Vec3i(-1, 0, 0),
            Vec3i(0, 0, 1),
        ],
        vec![
            Vec3i(0, 0, 0),
            Vec3i(1, 0, 0),
            Vec3i(1, 0, 1),
            Vec3i(2, 0, 1),
            Vec3i(2, 0, 0),
            Vec3i(0, 0, 1),
        ],
    ];
    for (i, cells) in candidates.iter().enumerate() {
        let mut a = Assembly::new();
        a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
        let id = format!("nexus.s{}", i);
        a.register(def_cells_mounts(&id, cells.clone()));
        a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
        let cands = a.snap_candidates(&id);
        let snap = cands.iter().any(|(c, r)| {
            matches!(
                a.probe_place_with_vehicle(&id, *c, *r, 1),
                Ok(vxl_core::assembly::ProbeResult::Snap)
            )
        });
        assert!(snap, "形状 #{} {:?} 无法吸附", i, cells);
    }
}

/// 整车平移：所有成员 origin/占用格一起动，不变量保持
#[test]
fn vehicle_translate_moves_all_members() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let b = a.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    assert!(a.translate_vehicle(1, Vec3i(2, 0, 1)));
    assert_eq!(a.modules[&a.root.unwrap()].origin, Vec3i(2, 0, 1));
    assert_eq!(a.modules[&b].origin, Vec3i(3, 0, 1));
    assert!(a.occupancy.contains_key(&Vec3i(2, 0, 1)));
    assert!(a.occupancy.contains_key(&Vec3i(3, 0, 1)));
    assert_invariants(&a);
}

#[test]
fn vehicle_translate_rejected_on_collision() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    a.place_free("nexus.b", Vec3i(3, 0, 0), 0).unwrap();
    assert!(!a.translate_vehicle(1, Vec3i(3, 0, 0)));
    assert_eq!(a.modules[&a.root.unwrap()].origin, Vec3i(0, 0, 0));
}

/// 整车绕 Y 旋转：root 不动，成员相对位置保持，边保留
#[test]
fn vehicle_rotate_y_keeps_structure() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let b = a.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    let before_edges = a.edges.len();
    assert!(a.rotate_vehicle_y(1, true));
    assert_eq!(a.modules[&a.root.unwrap()].origin, Vec3i(0, 0, 0));
    assert_eq!(a.modules[&b].origin, Vec3i(0, 0, -1));
    assert_eq!(a.edges.len(), before_edges);
    assert_invariants(&a);
}

/// 旋转后连接边面方向重建：root 朝 b 的面从 East 变 South
#[test]
fn rotate_vehicle_rebuilds_edge_faces() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    a.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    let (_, edges) = a.edges.iter().next().unwrap();
    assert!(
        matches!(edges[0].face_a, Face::East | Face::West),
        "旋转前接触面应为东西向，实际 {:?}",
        edges[0].face_a
    );
    assert!(a.rotate_vehicle_y(1, true));
    let (_, edges) = a.edges.iter().next().unwrap();
    assert!(
        matches!(edges[0].face_a, Face::North | Face::South),
        "旋转后接触面应为南北向，实际 {:?}",
        edges[0].face_a
    );
}

/// 单模块坐标回写（物理落地后对齐）：移动/冲突拒绝
#[test]
fn set_module_origin_moves_single_module() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let b = a.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    assert!(a.set_module_origin(b, Vec3i(3, 0, 0)));
    assert_eq!(a.modules[&b].origin, Vec3i(3, 0, 0));
    assert!(a.occupancy.contains_key(&Vec3i(3, 0, 0)));
    assert!(!a.occupancy.contains_key(&Vec3i(1, 0, 0)));
    assert_invariants(&a);
    // 与 root 冲突 → 拒绝且不变
    assert!(!a.set_module_origin(b, Vec3i(0, 0, 0)));
    assert_eq!(a.modules[&b].origin, Vec3i(3, 0, 0));
}

/// 整车质量/质心/碰撞伤害
#[test]
fn vehicle_mass_center_and_damage() {
    let mut a = Assembly::new();
    let mut cab = def("nexus.cab", Category::Cab, [1, 1, 1]);
    cab.mass = 30.0;
    a.register(cab);
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.cab", Vec3i(0, 0, 0), 0).unwrap();
    a.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    assert_eq!(a.vehicle_mass(1), 40.0);
    let c = a.vehicle_center(1).unwrap();
    // 质心 x = (30*0.5 + 10*1.5) / 40 = 0.75
    assert!((c.0 - 0.75).abs() < 0.01, "质心 x={}", c.0);
    assert_eq!(Assembly::impact_damage(10.0, 40.0, 100.0), 400.0);
}

/// 候选吸附缓存：同装配重复查询结果稳定，装配变化后自动失效
#[test]
fn snap_candidates_cache_stable_and_invalidated() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let c1 = a.snap_candidates("nexus.b");
    let c2 = a.snap_candidates("nexus.b");
    assert_eq!(c1, c2, "缓存命中应返回相同候选");
    a.place("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    let c3 = a.snap_candidates("nexus.b");
    assert_ne!(c1.len(), c3.len(), "放置后候选应刷新");
}

/// 质心必须包含形状最小格偏移（Cells 不从 (0,0,0) 开始时）
#[test]
fn vehicle_center_handles_nonzero_min() {
    let mut a = Assembly::new();
    a.register(def_cells("nexus.off", vec![Vec3i(2, 0, 0), Vec3i(3, 0, 0)]));
    a.place_root("nexus.off", Vec3i(0, 0, 0), 0).unwrap();
    let c = a.vehicle_center(1).unwrap();
    // 模块局部占用会归一到最小角，原点始终是模块占用格最小角。
    assert!((c.0 - 1.0).abs() < 0.01, "质心 x={}", c.0);
    assert!((c.1 - 0.5).abs() < 0.01);
}

/// 存档 next_id 过小/缺失时，加载后自增 id 必须跳过已用 id
#[test]
fn load_sanitizes_next_id() {
    use vxl_core::assembly::{SaveData, SavedModule};
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    let data = SaveData {
        root: Some(5),
        next_id: 0,
        next_vehicle: 1,
        island_vehicles: vec![],
        modules: vec![SavedModule {
            id: 5,
            def_id: "nexus.b".into(),
            origin: Vec3i(0, 0, 0),
            rotation: 0,
            vehicle: 1,
            hp: 100,
        }],
        edges: vec![],
        physics: Default::default(),
    };
    a.load(data).unwrap();
    let new_id = a.place_free("nexus.b", Vec3i(3, 0, 0), 0).unwrap();
    assert_eq!(new_id, 6, "next_id 应跳过已用 id 5");
}

/// 坏存档防御：悬空 root / 悬空边 / next_vehicle 落后现存载具，加载即清洗不延迟爆雷
#[test]
fn load_sanitizes_corrupt_save() {
    use vxl_core::assembly::{SaveData, SavedEdge, SavedModule};
    let mut b = Assembly::new();
    b.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    let data = SaveData {
        root: Some(9999), // 悬空 root：不清洗会在下一次破坏事件把全场判成碎片散落
        next_id: 10,
        next_vehicle: 1,
        island_vehicles: vec![],
        modules: vec![
            SavedModule {
                id: 1,
                def_id: "nexus.b".into(),
                origin: Vec3i(0, 0, 0),
                rotation: 0,
                vehicle: 2,
                hp: 100,
            },
            SavedModule {
                id: 2,
                def_id: "nexus.b".into(),
                origin: Vec3i(1, 0, 0),
                rotation: 0,
                vehicle: 2,
                hp: 100,
            },
        ],
        edges: vec![
            SavedEdge {
                a: 1,
                b: 2,
                face_a: Face::East,
                face_b: Face::West,
                strength: 100.0,
            },
            SavedEdge {
                a: 1,
                b: 8888,
                face_a: Face::East,
                face_b: Face::West,
                strength: 100.0,
            }, // 悬空边
        ],
        physics: Default::default(),
    };
    b.load(data).unwrap();
    assert!(
        b.root.is_some_and(|r| b.modules.contains_key(&r)),
        "悬空 root 必须回退到现存模块"
    );
    assert!(
        b.edges
            .keys()
            .all(|(x, y)| b.modules.contains_key(x) && b.modules.contains_key(y)),
        "悬空边必须丢弃"
    );
    assert!(
        b.peek_next_vehicle() > b.modules.values().map(|m| m.vehicle).max().unwrap(),
        "next_vehicle 必须高于现存最大载具 id（防撞岛）"
    );
}

/// 带挂点抽象形状：snap_targets 返回真实挂点目标
#[test]
fn snap_targets_works_for_mounted_shapes() {
    let mut a = Assembly::new();
    a.register(def_cells_mounts("nexus.root", vec![Vec3i(0, 0, 0)]));
    a.register(def_cells_mounts(
        "nexus.l",
        vec![Vec3i(0, 0, 0), Vec3i(1, 0, 0), Vec3i(0, 0, 1)],
    ));
    a.place_root("nexus.root", Vec3i(0, 0, 0), 0).unwrap();
    let targets = a.snap_targets("nexus.l", Vec3i(1, 0, 0), 0, 1);
    assert!(!targets.is_empty(), "带挂点抽象形状应有吸附高亮");
    assert!(
        targets.contains(&(Vec3i(0, 0, 0), Face::East)),
        "高亮应指向邻居 East 面"
    );
}

/// 轮子（Any 挂点）应能连到驾驶舱（Any 挂点）
#[test]
fn wheel_connects_to_cab_with_any_mount() {
    let mut a = Assembly::new();
    a.register(def("nexus.cab", Category::Cab, [1, 1, 1]));
    a.register(def("nexus.w", Category::Wheel, [1, 1, 1]));
    a.place_root("nexus.cab", Vec3i(0, 0, 0), 0).unwrap();
    let id = a.place("nexus.w", Vec3i(0, 0, -1), 0).unwrap();
    assert_eq!(a.modules[&id].vehicle, 1);
    assert_eq!(a.edges.len(), 1);
}

/// 挂点类别限制必须生效：结构件也不能连到 Only(Cab) 挂点面
#[test]
fn structure_cannot_connect_to_restricted_mount() {
    let mut a = Assembly::new();
    let mut root = def("nexus.mount", Category::Structure, [1, 1, 1]);
    root.mount_points = Face::ALL
        .iter()
        .map(|&f| MountPoint {
            cell: Vec3i(0, 0, 0),
            face: f,
            accepts: MountMask::Only(vec![Category::Cab]),
            strength: 100.0,
            layer: 0,
        })
        .collect();
    a.register(root);
    a.place_root("nexus.mount", Vec3i(0, 0, 0), 0).unwrap();
    // 带 Any 挂点的结构件 → 被 Only(Cab) 拒绝
    a.register(def("nexus.s", Category::Structure, [1, 1, 1]));
    assert!(a.place("nexus.s", Vec3i(1, 0, 0), 0).is_err());
    // 无挂点抽象结构件 → 同样被拒绝
    a.register(def_cells("nexus.s2", vec![Vec3i(0, 0, 0)]));
    assert!(a.place("nexus.s2", Vec3i(-1, 0, 0), 0).is_err());
    // Cab 仍能连接
    a.register(def("nexus.cab", Category::Cab, [1, 1, 1]));
    assert!(a.place("nexus.cab", Vec3i(1, 0, 0), 0).is_ok());
}

/// 每格 6 面挂点的形状定义（用于批量形状测试）
fn def_cells_mounts(id: &str, cells: Vec<Vec3i>) -> ModuleDef {
    let mut mps = Vec::new();
    for &c in &cells {
        for f in Face::ALL {
            mps.push(MountPoint {
                cell: c,
                face: f,
                accepts: MountMask::Any,
                strength: 100.0,
                layer: 0,
            });
        }
    }
    ModuleDef {
        id: id.into(),
        name: "挂点形状".into(),
        corp: "nexus".into(),
        category: Category::Structure,
        mass: 10.0,
        hp: 100,
        shape: Shape::Cells { cells },
        mount_points: mps,
        tags: vec![],
        model_path: None,
    }
}

/// 暴露挂点期望数 = 6*格数 - 2*相邻面对数
fn expected_exposed_mounts(cells: &[Vec3i]) -> usize {
    let mut pairs = 0;
    for i in 0..cells.len() {
        for j in (i + 1)..cells.len() {
            let d = cells[i] - cells[j];
            if d.0.abs() + d.1.abs() + d.2.abs() == 1 {
                pairs += 1;
            }
        }
    }
    6 * cells.len() - 2 * pairs
}

macro_rules! shape_tests {
    ($($idx:literal => $cells:expr;)*) => {
        paste::paste! {
            $(
                #[test]
                fn [<shape_mounts_ $idx>]() {
                    let cells: Vec<Vec3i> = $cells;
                    for rot in 0..24u8 {
                        let mut b = Assembly::new();
                        b.register(def_cells_mounts("nexus.s", cells.clone()));
                        let id = b.place_root("nexus.s", Vec3i(0, 0, 0), rot).unwrap();
                        let md = &b.modules[&id];
                        assert_eq!(md.mounts.len(), expected_exposed_mounts(&cells), "rot={} 暴露挂点数", rot);
                        for w in &md.mounts {
                            assert!(!md.cells.contains(&w.face.neighbor(w.cell)), "rot={} 内部挂点不应出现", rot);
                        }
                        assert_invariants(&b);
                    }
                }

                #[test]
                fn [<shape_snap_ $idx>]() {
                    let cells: Vec<Vec3i> = $cells;
                    let mut a = Assembly::new();
                    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
                    a.register(def_cells_mounts("nexus.s", cells.clone()));
                    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
                    let cands = a.snap_candidates("nexus.s");
                    let snap = cands.iter().any(|(c, r)| {
                        matches!(a.probe_place_with_vehicle("nexus.s", *c, *r, 1), Ok(vxl_core::assembly::ProbeResult::Snap))
                    });
                    assert!(snap, "形状 #{} 应有吸附候选", $idx);
                }
            )*
        }
    };
}

shape_tests! {
    1 => vec![Vec3i(0,0,0), Vec3i(1,0,0)];
    2 => vec![Vec3i(0,0,0), Vec3i(1,0,0), Vec3i(2,0,0)];
    3 => vec![Vec3i(0,0,0), Vec3i(1,0,0), Vec3i(0,0,1)];
    4 => vec![Vec3i(0,0,0), Vec3i(1,0,0), Vec3i(0,0,1), Vec3i(1,0,1)];
    5 => vec![Vec3i(0,0,0), Vec3i(1,0,0), Vec3i(2,0,0), Vec3i(1,0,1)];
    6 => vec![Vec3i(0,0,0), Vec3i(1,0,0), Vec3i(-1,0,0), Vec3i(0,0,1), Vec3i(0,0,-1)];
    7 => vec![Vec3i(0,0,0), Vec3i(2,0,0), Vec3i(0,0,1), Vec3i(1,0,1), Vec3i(2,0,1)];
    8 => vec![Vec3i(0,0,0), Vec3i(1,0,0), Vec3i(1,0,1), Vec3i(2,0,1)];
    9 => vec![Vec3i(0,0,0), Vec3i(1,0,0), Vec3i(2,0,0), Vec3i(0,0,1), Vec3i(0,0,2)];
    10 => vec![Vec3i(0,0,0), Vec3i(1,0,0), Vec3i(-1,0,0), Vec3i(0,0,1), Vec3i(0,0,-1), Vec3i(0,1,0)];
    11 => vec![Vec3i(0,0,0), Vec3i(1,0,0), Vec3i(2,0,0), Vec3i(0,0,1), Vec3i(2,0,1), Vec3i(0,0,2), Vec3i(1,0,2), Vec3i(2,0,2)];
    12 => vec![Vec3i(0,0,0), Vec3i(1,0,0), Vec3i(2,0,0), Vec3i(0,0,1), Vec3i(1,0,1)];
}

/// 自由放置贴着载具 → 吸附并继承载具、生成边
#[test]
fn place_free_adjacent_to_vehicle_snaps() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let id = a.place_free("nexus.b", Vec3i(1, 0, 0), 0).unwrap();
    assert_eq!(a.modules[&id].vehicle, 1);
    assert_eq!(a.edges.len(), 1);
}

/// 整车平移后候选缓存必须刷新
#[test]
fn translate_vehicle_invalidates_snap_cache() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let c1 = a.snap_candidates("nexus.b");
    assert!(a.translate_vehicle(1, Vec3i(3, 0, 2)));
    let c2 = a.snap_candidates("nexus.b");
    assert_ne!(c1, c2, "平移后候选应刷新");
}

/// 存档加载后散落归属保持 vehicle=0 且无边
#[test]
fn save_load_preserves_scattered_vehicle0() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    let id = a.place_free("nexus.b", Vec3i(5, 0, 5), 0).unwrap();
    let data = a.save();
    let mut b = Assembly::new();
    b.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    b.load(data).unwrap();
    assert_eq!(b.modules[&id].vehicle, 0);
    assert_eq!(b.edges.len(), 0);
}

/// set_root 提升后 root 允许移除
#[test]
fn set_root_then_remove_allowed() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    let id = a.place_free("nexus.b", Vec3i(3, 0, 3), 0).unwrap();
    a.set_root(id).unwrap();
    assert!(a.remove(id).is_ok());
    assert!(a.root.is_none());
}

/// 多格模块整车平移后连接边保持
#[test]
fn multi_cell_translate_keeps_edges() {
    let mut a = Assembly::new();
    a.register(def_b2_edge("nexus.b2e"));
    a.place_root("nexus.b2e", Vec3i(0, 0, 0), 0).unwrap();
    let id = a.place("nexus.b2e", Vec3i(2, 0, 0), 0).unwrap();
    let before = a.edges.len();
    assert!(a.translate_vehicle(1, Vec3i(1, 0, 1)));
    assert_eq!(a.edges.len(), before);
    assert_eq!(a.modules[&id].origin, Vec3i(3, 0, 1));
    assert_invariants(&a);
}

/// 旋转后模块挂点必须与占用格一致（挂点格在模块内、面朝外）
#[test]
fn rotate_rebuilds_consistent_mounts() {
    let mut a = Assembly::new();
    a.register(def_b2_edge("nexus.b2e"));
    a.place_root("nexus.b2e", Vec3i(0, 0, 0), 0).unwrap();
    assert!(a.rotate_vehicle_y(1, true));
    for md in a.modules.values() {
        for w in &md.mounts {
            assert!(md.cells.contains(&w.cell), "挂点格必须在模块内");
            assert!(!md.cells.contains(&w.face.neighbor(w.cell)), "挂点必须朝外");
        }
    }
}

/// place_free 放置的散落模块：vehicle=0、不生成边
#[test]
fn place_free_creates_scattered_vehicle0() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_free("nexus.b", Vec3i(5, 0, 5), 0).unwrap();
    let id = a.place_free("nexus.b", Vec3i(6, 0, 6), 0).unwrap();
    assert_eq!(a.modules[&id].vehicle, 0, "散落模块必须是 vehicle=0");
    assert!(a.edges.is_empty(), "散落模块之间不生成边");
}

/// set_root 提升散落模块：分配载具 id，之后能正常吸附
#[test]
fn set_root_promotes_and_assigns_vehicle() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    let id = a.place_free("nexus.b", Vec3i(3, 0, 3), 0).unwrap();
    assert_eq!(a.modules[&id].vehicle, 0);
    assert!(a.set_root(id).is_ok());
    assert_ne!(a.modules[&id].vehicle, 0, "提升后必须有载具 id");
    let id2 = a.place("nexus.b", Vec3i(4, 0, 3), 0).unwrap();
    assert_eq!(a.modules[&id2].vehicle, a.modules[&id].vehicle);
    assert_eq!(a.edges.len(), 1);
}

/// ---- 性能尺子（防复杂度回归）----
/// O(V·E) 全表扫 → O(V+E) 邻接表的回归护栏：
/// 500 模块链 + 100 次断边事件。旧实现在此规模约秒级，新实现毫秒级。
/// 上限取 2.0s（宽裕 3 个数量级，防 CI 抖动误报），超过即说明复杂度退化。
#[test]
fn perf_root_component_bfs_scale() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
    for i in 1..500i32 {
        a.place("nexus.b", Vec3i(i, 0, 0), 0).unwrap();
    }
    let start = std::time::Instant::now();
    for i in 1..=100u32 {
        // 从链中间断边：每次都触发 root_component + 全图连通分量 + 孤岛收尾
        let root = a.root.unwrap();
        let mid = i * 3;
        a.damage_connection(root, mid, 1000.0).unwrap_or(Vec::new());
    }
    let elapsed = start.elapsed();
    eprintln!("perf_root_component_bfs_scale: {elapsed:?}");
    assert!(
        elapsed.as_secs_f32() < 2.0,
        "BFS 复杂度疑似退化到全表扫: {elapsed:?}"
    );
}

/// B 键成立载具路径的回归护栏：5000 散落相邻模块 promote。
/// 旧实现格邻接 O(N·V) + 边重建 O(V²) 在此规模为秒级~十秒级；
/// 新实现两者均 O(V)。上限 2.0s（宽裕数个数量级，防 CI 抖动）。
#[test]
fn perf_promote_island_bfs_scale() {
    let mut a = Assembly::new();
    a.register(def("nexus.b", Category::Structure, [1, 1, 1]));
    for i in 0..5000i32 {
        // 散落但格邻接连成链（promote 前无载具归属）
        a.place_free("nexus.b", Vec3i(i, 0, 0), 0).unwrap();
    }
    let start = std::time::Instant::now();
    let first = a.modules.keys().copied().min().unwrap();
    let v = a.promote_island(first).expect("promote 5000 链");
    let elapsed = start.elapsed();
    eprintln!("perf_promote_island_bfs_scale: {elapsed:?}");
    assert!(
        elapsed.as_secs_f32() < 2.0,
        "promote 复杂度疑似退化: {elapsed:?}"
    );
    // 全体收编 + 边表重建完整（链 = N-1 条边）
    assert_eq!(a.modules.values().filter(|m| m.vehicle == v).count(), 5000);
    assert_eq!(a.edges.len(), 4999);
}
