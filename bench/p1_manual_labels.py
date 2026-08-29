# -*- coding: utf-8 -*-
"""p1_manual_labels.py —— 人工语义标注（评审者=模型逐行读码，独立于 bug_scan 输出）。

口径：safe = 结构存在但语义上无缺陷（测试断言/守卫/不变量可证/数值范围安全）；
unsafe = 真实缺陷风险（无守卫索引/无法证明的不变量）。
标注只覆盖评审者自己的枚举（词法超集 grep + 函数上下文精读）；
python undefined_name 的 FN 方向未全量精读（connector 2941L）——如实记录。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "p1_manual_labels.jsonl")

# 快照级标注：unsafe 明细；未列出的一律 safe（含全部测试模块与守卫路径）
LABELS = {
    "V3_24_head": {"unsafe": [], "note": "全文件几乎全为 #[cfg(test)] mod tests；expect/unwrap 均为测试断言"},
    "V3_25_rand": {"unsafe": [
        {"line": 1355, "rule": "unwrap", "why": "center.unwrap() 在循环内无可见守卫——上游 Option 不变量无法从局部代码证明，实体缺中心即 panic"},
    ], "note": "19+64 候选：网格坐标 as f32 数值域安全；.single() 均 let-else 守卫；1513 forward_input==0.0 为枚举字面量非浮点比较"},
    "V3_26_head": {"unsafe": [], "note": "4 候选：ITEMS_PER_ROW as f32/md.hp as f32 数值域安全；total==0.0 为求和早退非精度敏感"},
    "V3_27_rand": {"unsafe": [], "note": "同 26"},
    "V3_28_head": {"unsafe": [], "note": "get_mut().unwrap() 均被前置 self.modules[&id] 索引或校验循环保证不变量"},
    "V3_29_head": {"unsafe": [], "note": "测试模块"},
    "V3_30_rand": {"unsafe": [], "note": "测试模块"},
    "V3_31_head": {"unsafe": [], "note": "stress 测试台：unwrap/expect/panic! 均为断言语义"},
    "V3_32_rand": {"unsafe": [], "note": "同 31"},
    "VoxelForge_01_head": {"unsafe": [], "note": "dims>=1 域保证 (dims-1) 无下溢；.single().ok()? 守卫；L613 测试 unwrap"},
    "VoxelForge_02_rand": {"unsafe": [], "note": "同 01 结构"},
    "VoxelForge_03_head": {"unsafe": [], "note": "L311 显式 rot>=24 防御后再索引 rotations_24()[rot]"},
    "VoxelForge_04_rand": {"unsafe": [
        {"line": 206, "rule": "indexing", "why": "rotations_24()[rot as usize] 无 rot>=24 守卫——rot 为 u8 全域可达，24..255 直接越界 panic（head 版本后补了防御注释，证明此缺陷真实存在过）"},
    ], "note": "其余 as f32/测试同前"},
    "VoxelForge_05_head": {"unsafe": [], "note": "pm.rotation 由 rotate_picked 的 position 查找维持 0..23 不变量；.single() 均 let-else"},
    "VoxelForge_06_rand": {"unsafe": [], "note": "同 05 不变量"},
    "VoxelForge_07_head": {"unsafe": [], "note": "全部 md.rotation % 24 后索引（防御已在位）"},
    "VoxelForge_08_rand": {"unsafe": [
        {"line": 61, "rule": "indexing", "why": "rotations_24()[md.rotation as usize] 无 % 24 归一——head 版本后补 % 24（如坏存档 rotation 越界即 panic），旧版留洞"},
    ], "note": "L178 load_game unwrap 域保证（刚注册的 def/空位）"},
    "VoxelForge_09_head": {"unsafe": [], "note": "python 探针：无 except:/eval 候选；undefined_name FN 方向未逐行（见协议注）"},
    "VoxelForge_10_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_11_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_12_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_13_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_14_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_15_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_16_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_17_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_18_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_19_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_20_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_21_head": {"unsafe": [], "note": "同上"},
    "VoxelForge_22_head": {"unsafe": [], "note": "connector 2941L：词法候选 0；undefined_name 未逐行"},
    "VoxelForge_23_rand": {"unsafe": [], "note": "同 22"},
}


def main():
    snaps = [json.loads(l) for l in
             open(os.path.join(HERE, "manual_snapshots.jsonl"), encoding="utf-8")
             if l.strip()]
    with open(OUT, "w", encoding="utf-8") as f:
        for s in snaps:
            lab = LABELS.get(s["snap_id"], {"unsafe": [], "note": "未标注"})
            f.write(json.dumps({**s, "labels": lab}, ensure_ascii=False) + "\n")
    n = sum(len(json.loads(l)["labels"]["unsafe"]) for l in open(OUT, encoding="utf-8"))
    print(f"[OK] {len(snaps)} 快照标注 -> {os.path.relpath(OUT, ROOT)}（unsafe {n} 处）")


if __name__ == "__main__":
    main()
