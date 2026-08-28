//! 整车变换：平移/旋转/单模块回写、质量质心、碰撞伤害公式、边表重建。
use super::types::mount_edge_strength;
use super::{Assembly, Edge, ModuleId, VehicleId, WorldMount};
use crate::module::{Face, Vec3i};
use crate::rotation::rotations_24;

impl Assembly {
    /// 平移整辆载具（vehicle>0 的所有模块），冲突则不动
    pub fn translate_vehicle(&mut self, vehicle: VehicleId, delta: Vec3i) -> bool {
        if vehicle == 0 || delta == Vec3i(0, 0, 0) {
            return true;
        }
        let ids: Vec<ModuleId> = self
            .modules
            .iter()
            .filter(|(_, m)| m.vehicle == vehicle)
            .map(|(id, _)| *id)
            .collect();
        if ids.is_empty() {
            return false;
        }
        let self_set: std::collections::HashSet<ModuleId> = ids.iter().copied().collect();
        let mut plan: ahash::AHashMap<ModuleId, Vec<Vec3i>> =
            ahash::AHashMap::with_capacity(ids.len());
        for &id in &ids {
            let md = &self.modules[&id];
            let new_cells: Vec<Vec3i> = md.cells.iter().map(|&c| c + delta).collect();
            for &c in &new_cells {
                if let Some(&owner) = self.occupancy.get(&c)
                    && !self_set.contains(&owner)
                {
                    return false;
                }
            }
            plan.insert(id, new_cells);
        }
        for &id in &ids {
            let old_cells = self.modules[&id].cells.clone();
            for &c in &old_cells {
                self.occupancy.remove(&c);
            }
            let md = self.modules.get_mut(&id).unwrap();
            md.origin = md.origin + delta;
            md.cells = plan[&id].clone();
            md.mounts = md
                .mounts
                .iter()
                .map(|w| WorldMount {
                    cell: w.cell + delta,
                    ..w.clone()
                })
                .collect();
        }
        for (id, cells) in plan {
            for c in cells {
                self.occupancy.insert(c, id);
            }
        }
        self.invalidate_candidates();
        true
    }

    /// 绕 Y 轴旋转整辆载具（clockwise=true 顺时针 90°），冲突则不动
    pub fn rotate_vehicle_y(&mut self, vehicle: VehicleId, clockwise: bool) -> bool {
        if vehicle == 0 {
            return false;
        }
        let ids: Vec<ModuleId> = self
            .modules
            .iter()
            .filter(|(_, m)| m.vehicle == vehicle)
            .map(|(id, _)| *id)
            .collect();
        if ids.is_empty() {
            return false;
        }
        let root_id = if let Some(r) = self.root
            && self.modules.get(&r).map(|m| m.vehicle) == Some(vehicle)
        {
            r
        } else {
            ids[0]
        };
        let root_origin = self.modules[&root_id].origin;
        let mut r = crate::rotation::rotate_y_90_idx(0) as usize;
        if !clockwise {
            r = crate::rotation::rotate_y_90_idx(r as u8) as usize;
            r = crate::rotation::rotate_y_90_idx(r as u8) as usize;
        }
        let m = rotations_24()[r];
        let self_set: std::collections::HashSet<ModuleId> = ids.iter().copied().collect();
        let mut plan: ahash::AHashMap<ModuleId, (Vec3i, Vec<Vec3i>)> =
            ahash::AHashMap::with_capacity(ids.len());
        for &id in &ids {
            let md = &self.modules[&id];
            let new_cells: Vec<Vec3i> = md
                .cells
                .iter()
                .map(|&c| root_origin + m.apply_to_coord(c - root_origin))
                .collect();
            let new_origin =
                new_cells
                    .iter()
                    .fold(Vec3i(i32::MAX, i32::MAX, i32::MAX), |mut min, c| {
                        min.0 = min.0.min(c.0);
                        min.1 = min.1.min(c.1);
                        min.2 = min.2.min(c.2);
                        min
                    });
            for &c in &new_cells {
                if let Some(&owner) = self.occupancy.get(&c)
                    && !self_set.contains(&owner)
                {
                    return false;
                }
            }
            plan.insert(id, (new_origin, new_cells));
        }
        for &id in &ids {
            let old_cells = self.modules[&id].cells.clone();
            for &c in &old_cells {
                self.occupancy.remove(&c);
            }
            let (new_origin, new_cells) = plan[&id].clone();
            {
                let md = self.modules.get_mut(&id).unwrap();
                md.origin = new_origin;
                md.cells = new_cells;
                md.rotation = crate::rotation::rotate_y_90_idx(md.rotation);
            }
            let def = self.defs[&self.modules[&id].def_id].clone();
            let origin = self.modules[&id].origin;
            let rotation = self.modules[&id].rotation;
            let mounts = self.compute_world_mounts(&def, origin, rotation);
            self.modules.get_mut(&id).unwrap().mounts = mounts;
        }
        for (id, (_, cells)) in plan {
            for c in cells {
                self.occupancy.insert(c, id);
            }
        }
        self.rebuild_edges_for_vehicle(vehicle);
        self.invalidate_candidates();
        true
    }

    /// 把单个模块平移到新 origin（物理回写/对齐用），冲突则不动
    pub fn set_module_origin(&mut self, id: ModuleId, origin: Vec3i) -> bool {
        let Some(md) = self.modules.get(&id) else {
            return false;
        };
        let delta = origin - md.origin;
        if delta == Vec3i(0, 0, 0) {
            return true;
        }
        let new_cells: Vec<Vec3i> = md.cells.iter().map(|&c| c + delta).collect();
        for &c in &new_cells {
            if let Some(&owner) = self.occupancy.get(&c)
                && owner != id
            {
                return false;
            }
        }
        let old_cells = md.cells.clone();
        for &c in &old_cells {
            self.occupancy.remove(&c);
        }
        let md = self.modules.get_mut(&id).unwrap();
        md.origin = origin;
        md.cells = new_cells;
        md.mounts = md
            .mounts
            .iter()
            .map(|w| WorldMount {
                cell: w.cell + delta,
                ..w.clone()
            })
            .collect();
        for &c in &md.cells {
            self.occupancy.insert(c, id);
        }
        self.invalidate_candidates();
        true
    }

    /// 整车质量（Σ 模块质量）
    pub fn vehicle_mass(&self, vehicle: VehicleId) -> f32 {
        self.modules
            .values()
            .filter(|m| m.vehicle == vehicle)
            .map(|m| self.defs[&m.def_id].mass)
            .sum()
    }

    /// 整车质心（世界坐标，模块 AABB 中心加权）
    pub fn vehicle_center(&self, vehicle: VehicleId) -> Option<(f32, f32, f32)> {
        let mut mass_sum = 0.0f32;
        let mut pos = (0.0f32, 0.0f32, 0.0f32);
        for md in self.modules.values().filter(|m| m.vehicle == vehicle) {
            let def = &self.defs[&md.def_id];
            let mut min = Vec3i(i32::MAX, i32::MAX, i32::MAX);
            let mut max = Vec3i(i32::MIN, i32::MIN, i32::MIN);
            for &c in &md.cells {
                min.0 = min.0.min(c.0);
                min.1 = min.1.min(c.1);
                min.2 = min.2.min(c.2);
                max.0 = max.0.max(c.0);
                max.1 = max.1.max(c.1);
                max.2 = max.2.max(c.2);
            }
            let center = (
                min.0 as f32 + (max.0 - min.0 + 1) as f32 * 0.5,
                min.1 as f32 + (max.1 - min.1 + 1) as f32 * 0.5,
                min.2 as f32 + (max.2 - min.2 + 1) as f32 * 0.5,
            );
            pos.0 += center.0 * def.mass;
            pos.1 += center.1 * def.mass;
            pos.2 += center.2 * def.mass;
            mass_sum += def.mass;
        }
        if mass_sum <= 0.0 {
            return None;
        }
        Some((pos.0 / mass_sum, pos.1 / mass_sum, pos.2 / mass_sum))
    }

    /// 泰拉科技式碰撞伤害：相对速度 × min(双方质量)
    pub fn impact_damage(relative_speed: f32, self_mass: f32, other_mass: f32) -> f32 {
        relative_speed * self_mass.min(other_mass)
    }

    /// 重建某载具内部连接边（旋转/平移后保证面语义正确）。
    /// O(V) 几何寻邻：每模块每占用格向 6 邻查 occupancy 命中同车模块再测匹配——
    /// 旧实现按模块两两配对全量扫格 O(V²)，B 键成立大载具时的真瓶颈。
    /// 语义保持：每对模块仍只记第一条匹配接触面（与旧 break 行为一致）。
    pub fn rebuild_edges_for_vehicle(&mut self, vehicle: VehicleId) {
        if vehicle == 0 {
            return;
        }
        let keys: Vec<(ModuleId, ModuleId)> = self
            .edges
            .keys()
            .copied()
            .filter(|(a, b)| {
                self.modules.get(a).map(|m| m.vehicle) == Some(vehicle)
                    || self.modules.get(b).map(|m| m.vehicle) == Some(vehicle)
            })
            .collect();
        for k in keys {
            self.edges.remove(&k);
        }
        let ids: ahash::AHashSet<ModuleId> = self
            .modules
            .iter()
            .filter(|(_, m)| m.vehicle == vehicle)
            .map(|(id, _)| *id)
            .collect();
        let mut done_pairs: ahash::AHashSet<(ModuleId, ModuleId)> = ahash::AHashSet::new();
        for &a in &ids {
            let ma = self.modules[&a].clone();
            let def_a = self.defs[&ma.def_id].clone();
            for &c in &ma.cells {
                for face in Face::ALL {
                    let Some(&b) = self.occupancy.get(&face.neighbor(c)) else {
                        continue;
                    };
                    if b == a || !ids.contains(&b) {
                        continue;
                    }
                    let pair = (a.min(b), a.max(b));
                    if done_pairs.contains(&pair) {
                        continue;
                    }
                    let mb = self.modules[&b].clone();
                    // 与旧实现一致：固定以小 id 为搜索起点方向
                    let (na, nb, cell, f) = if a < b {
                        (&ma, &mb, c, face)
                    } else {
                        (&mb, &ma, face.neighbor(c), face.opposite())
                    };
                    let def_na = if a < b {
                        &def_a
                    } else {
                        &self.defs[&nb.def_id]
                    };
                    if self.connection_matches(def_na, &na.mounts, nb, cell, f)
                        && done_pairs.insert(pair)
                    {
                        let strength =
                            mount_edge_strength(na.mounts.iter(), f, cell, nb.mounts.iter());
                        self.edges.entry(pair).or_default().push(Edge {
                            face_a: f,
                            face_b: f.opposite(),
                            strength,
                        });
                    }
                }
            }
        }
    }
}
