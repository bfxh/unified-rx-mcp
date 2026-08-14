# IDE 游戏方向升级总文档 v3（skill 学习驱动 · 引擎中立）

> 2026-08-14 · 基于 game-dev/game-design skill 通读提炼——"游戏很抽象，不能只靠一款游戏兼容"
> 原则：unified-rx 保持**通用内核**；游戏能力 = 通用工具 + 引擎中立方法论 + 项目级规则文件

---

## 一、skill 学习结论（2026-08-14 通读 10 文件）

### 1.1 已注册
- `game-dev`（E:\共享\51\skill-build\game-dev → .reasonix/skills/game-dev）：Godot 4 全生命周期 + Bevy 0.18 支持（bevy-mcp BRP localhost:15702 远程调试）
- `game-design`（→ .reasonix/skills/game-design）：18 子技能（手感/平衡/不变量/程序化音频/点阵资产/headless 测试）

### 1.2 提炼的引擎中立方法论（10 项——IDE 可工具化）

| # | 方法论 | 来源子技能 | 引擎中立性 | IDE 落地 |
|---|---|---|---|---|
| M1 | **实现不变量**：设计意图 → 引擎中立不变量 → 代码校验（"failure mode is rule-level rather than API-level"） | implementing-gameplay-invariants | ✅ 完全中立 | `game_invariants` 工具 |
| M2 | **表现寄存器匹配**：character/abstract/serious 三档先定寄存器再选效果（eyes/squash vs tilt/trails） | maximizing-game-feel | ✅ 完全中立 | `game_feel` 建议工具 |
| M3 | **程序化音频**：事件→音色映射、语义稳定音族、连续音 start/update/release/stop 控制面 | creating-godot-procedural-audio | ✅ 方法论中立（实现 Godot 版） | 音频规则（复用现有音频工具） |
| M4 | **可复现 headless 验证**：--headless --path + 项目本地 XDG 三件套 + logs 捕获 + 冒烟/逻辑分离 | running-headless-godot | ✅ 原则中立（Godot 版） | headless 验证规则 |
| M5 | **平衡评估**：exploratory_ratio 质量检测器、确定性种子、遥测完整才判、结构性修复优先数值微调 | evaluating-gameplay-balance | ✅ 完全中立（"in any engine"） | `game_balance` 契约检查 |
| M6 | **极简规则设计**：拒绝弱想法清单（dominant strategy/undefined machinery/high load） | designing-minimal-game-rules | ✅ 完全中立 | 设计审查清单 |
| M7 | **资产管线**：raw→processed（纹理压缩/模型优化/音频转换/UI），并行+跳过已处理 | game-asset-pipeline.ps1 | ✅ 管线原则中立 | `game_asset_check`（引用存在性） |
| M8 | **运行验证链路**：游戏运行截图 → OCR 识别 → AI 解读（"能截图识别就不靠猜"） | VERIFICATION.md | ✅ 完全中立 | 运行验证规则（OCR 复用 local-tools） |
| M9 | **运行时远程检查**：bevy-mcp BRP（localhost:15702）实体查询/修改 | game-dev SKILL v1.1 | Bevy 专属（通用原理：运行期协议） | bevy-mcp 接入说明 |
| M10 | **GDScript 工具链**：gdstyle/gdlint/gdformat/gdradon（生态索引） | ecosystem-resources.md | Godot 专属 | 工具索引 |

### 1.3 关键洞察
- **不变量是规则级不是 API 级**——"连打不应优于精打"在 Bevy/Godot/Unity 里都是同一套校验逻辑（输入节流/冷却/评分脉冲）——这是 IDE 游戏检查的通用原理
- **表现寄存器先于效果**——AI 建议游戏效果前必须先定寄存器（不臆测风格）
- **遥测完整才判**——平衡评估不允许从分数单点下结论（防幻觉）
- **确定性优先**——种子/帧率/聚合逻辑可复现，验证不靠猜

---

## 二、缺口矩阵（unified-rx IDE vs 方法论）

| 能力 | 现状 | 缺口 |
|---|---|---|
| LSP 语义补全 | ✅ 6 语言（rust/py/ts/js/c/cpp + go/json/css/html） | ✗ 游戏框架 API 语义（Bevy 组件/Godot 节点） |
| UI 检查 | ✅ Bevy/Godot/Unity/Flutter 死按钮+字体+root | ✗ 游戏性检查（不变量/寄存器/平衡） |
| 扫描族 | ✅ 24 语言规则 | ✗ 引擎中立游戏红线（每帧 IO/帧率无关逻辑） |
| 资产检查 | ✗ 无 | ✗ res:///GLB/纹理/音频引用存在性 |
| 可复现验证 | ✅ headless 测试理念（VoxelForge） | ✗ 封装成通用规则（XDG 三件套/日志/冒烟分离） |
| 运行验证 | ✗ 无 | ✗ 截图→OCR 链路（OCR 复用 local-tools） |
| 音频 | ✅ local-tools/10 工具集 | ✗ 游戏音频方法论接入（事件→音色） |
| 项目级规则 | ✗ 无 | ✗ game_rules（通用默认 + 项目覆盖） |

---

## 三、路线图（对应实施计划 v3）

```
阶段1 注册+学习（已完成）  ：game-dev/game-design 注册 + 10 项方法论提炼
阶段2 引擎中立检查(P0 完成) ：game_check/game_feel 工具（12 用例契约——3 引擎×3 规则+3 寄存器）
阶段3 引擎语义层(P1 完成)  ：game_api 词典（Bevy 29+Godot 27 项）+ code_complete game_hints 接入
阶段4 可复现验证+项目规则(P1 完成)：
  - game_verify（M4：smoke 脚本/XDG/日志捕获检查——无头验证不靠猜）
  - game_rules（项目级 game_rules.json——通用默认+项目覆盖，physics_range 可调）
  - 音频方法论：程序化音频（事件→音色映射、语义稳定音族、连续音 start/update/
    release/stop 控制面）已提炼入文档——复用现有音频工具链（local-tools/10 工具集）
    ，不新造
阶段5 收尾(P2)              ：全量验证 + 提交 + E 盘同步
```

**验收铁律**：不变量样例在 Bevy/Godot/Unity 写法都命中（引擎无关性测试）；未收录 API 诚实拒绝（防幻觉）；game_rules 通用默认 + 项目覆盖。

---

## 五、游戏方向交付清单（2026-08-14 完成）

| 工具 | 说明 | 方法论来源 |
|---|---|---|
| `game_check` | 引擎中立游戏工程检查（frame_io/input_unthrottled/physics_scale/frame_rate_dependent） | M1/M5 |
| `game_feel` | 表现寄存器判定（character/abstract/serious + 效果建议 + unknown 诚实） | M2 |
| `game_api` | 引擎 API 语义查询（Bevy 29 + Godot 27 项，未收录诚实拒绝） | 防幻觉 |
| `game_verify` | 可复现验证检查（smoke/XDG/日志捕获） | M4 |
| `game_rules` | 项目级规则读写（通用默认+项目覆盖——在游戏文件里再搞一个） | 用户定案 |
| `code_complete` 增强 | LSP 空结果时附 game_api 词典提示（game_hints） | 阶段3 |

**音频**：程序化音频方法论（M3）已提炼（事件→音色映射/音族稳定/连续音控制面）——
实际音频能力复用 local-tools / 10 工具集（用户："对音频处理肯定有更好的东西"）。

---

## 四、约束（用户定案）

- **不绑定单一游戏**：红线/检查全部引擎中立；单游戏特殊项只进该游戏的项目级规则文件
- **复用优先**：音频/OCR 等复用现有工具（local-tools/10 工具集），不新造
- **全部可调**：规则/参数配置化（沿用 D12 理念）
- **先学再做**：本方案全部方法论来自 skill 通读，非臆测
