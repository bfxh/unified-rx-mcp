//! 放置校验（V3）：目标格解析 + 预览一致性。

use crate::assembly::{Assembly, PlaceError};
use crate::module::Vec3i;
use crate::rotation::rotated_local_cells;

/// 解析放置格：目标格与占用重叠时沿外移方向逐格找空位（最多 8 格）
pub fn resolve_cell(asm: &Assembly, def_id: &str, rot: u8, cell: Vec3i, outward: Vec3i) -> Vec3i {
    if rot >= 24 {
        return cell;
    }
    // 漏洞A修复：outward 为 0 时防死循环（默认外移 +X）
    let outward = if outward == Vec3i(0, 0, 0) {
        Vec3i(1, 0, 0)
    } else {
        outward
    };
    let Some(def) = asm.defs.get(def_id) else {
        return cell;
    };
    let occ: std::collections::HashSet<Vec3i> = asm
        .modules
        .values()
        .flat_map(|md| md.cells.iter().copied())
        .collect();
    let locals = rotated_local_cells(&def.shape.local_cells(), rot);
    let mut c = cell;
    for _ in 0..8 {
        let free = locals.iter().all(|&lc| !occ.contains(&(c + lc)));
        if free {
            return c;
        }
        c = c + outward;
    }
    cell
}

/// 尝试放置（严格：必须连接）
pub fn try_place(
    asm: &mut Assembly,
    def_id: &str,
    cell: Vec3i,
    rot: u8,
) -> Result<u32, PlaceError> {
    asm.place(def_id, cell, rot)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::assembly::Assembly;
    use crate::module::{Category, Face, ModuleDef, MountMask, MountPoint, Shape};

    fn def() -> ModuleDef {
        ModuleDef {
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
                })
                .collect(),
            tags: vec![],
            model_path: None,
        }
    }

    #[test]
    fn resolve_overlap_outward() {
        let mut a = Assembly::new();
        a.register(def());
        a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
        // 目标 (0,0,0) 被占 → 外移 (1,0,0)
        let c = resolve_cell(&a, "nexus.b", 0, Vec3i(0, 0, 0), Vec3i(1, 0, 0));
        assert_eq!(c, Vec3i(1, 0, 0));
    }

    #[test]
    fn resolve_no_overlap_unchanged() {
        let mut a = Assembly::new();
        a.register(def());
        a.place_root("nexus.b", Vec3i(0, 0, 0), 0).unwrap();
        let c = resolve_cell(&a, "nexus.b", 0, Vec3i(1, 0, 0), Vec3i(1, 0, 0));
        assert_eq!(c, Vec3i(1, 0, 0));
    }
}
