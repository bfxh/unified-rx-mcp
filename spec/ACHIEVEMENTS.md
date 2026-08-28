# 成果清单（S22-S31 精选）

> 完整逐轮对账见 [UPGRADE.md](UPGRADE.md)，评测口径见 [EVAL.md](EVAL.md)。
> 本文是"前面的作为"按主题归类的速览，每条带证据出处。

## 一、P3 外锚：SWE-bench Verified 执行验证体系（S21→S25→S28）

| 阶段 | 内容 | 证据 |
|---|---|---|
| S21-S22 | 协议搭建 + 扩样 6→47（12 仓 × 双臂 94 run 全判） | commit bb29177 / c940d61；judge 94/94 零失败 |
| S23 | 机械落地层：LLM 手写 unified diff 0/16 可应用 → S/R 块协议 + 模糊窗 + 接地修复轮，可应用率 5%→30%；判官去通胀（空候选/markup 一律 false + 三票多数） | commit b0c31ec；judge_eq 12.8%/12.8%（戳破软判官 34% 通胀） |
| S24 | 真 fail-to-pass 执行验证：uv per-task venv，29/47 任务环境，patch 必须基线 FAIL→候选必须 PASS；判官 vs 执行 13/15 一致且分歧双向纠错 | commit c9d4ec4；verified A1/B2 |
| S25 | 真·闭环：执行失败输出回喂模型修复轮（≤3 轮），verified A 1→3 / B 2→3 | commit 68239ff；requests-1766 双臂翻绿、django-11999 被 PTB 回归拦截 |
| S28 | WSL 执行环境：C 扩展仓入局，feasible 29→33 | commit e8fc440；sklearn 0.20.dev WSL 构建通过 |
| S30 | WSL 补完：WSL_TASKS 16 任务（sklearn7/mpl4/astropy1/xarray2/seaborn2），**feasible 44/47（93.6%）**，verified A 8 / B 6 | commit b962c00 |

关键教训：S22 的 B 34% 是"散文 patch"通胀（16/16 solved 补丁 0 个可 apply）——
执行口径落地后 H1 的 fix-equivalent 增益归零，增益收缩到定位面（same_root
72.3% vs 63.8%）与可核验面。这就是执行验证存在的意义。

## 二、S29 高压检查：新模块对抗审计

- 5 洞坐实并修复：sr path 逃逸（写/读任意文件，safe_join commonpath 门禁）、
  wsl 脚本名碰撞（pid+序号）、FTB node id bash 注入（shlex.quote）、
  instance_id 路径穿越（safe_iid）、wsl_run 目录假设
- 8 个对抗测试先红后绿，常驻 tests/test_s29_fuzz.py（commit 9f92799）

## 三、P1 标注库与独立人工审计（S26→S27）

- S26：30 条自标注语料（15 bug + 15 clean，全历史"信号计数下降"挖掘），
  P/R 首测 P=R=1.0（自标注循环口径，如实定框）
- S27：32 快照独立人工标注（快照选择独立于 scan 输出，~490 候选逐个语义判定）：
  3 处真 unsafe（2 处 rotations_24 无守卫穿越 + 1 处不可证 unwrap），
  definite 家族零 FP，**clue 召回 1/3 → 揪出 indexing 正则漏 `[x.f as usize]`
  缺口 → 修复 → 3/3**（真快照回归锁死）

## 四、S31 语义工具回归 + P1 复测

- **P1 复测**（indexing 缺口修复后）：TP=3 FN=0，clue 召回 **1/3→3/3**；
  FP=810 全部是 clue 家族设计性命中（definite/clue 拆分口径，不算缺陷）
- **code_semantic 新工具**（S9 删掉的 semantic_search 的正确复活）：
  符号定义级 tf-idf 余弦向量空间，mode=search（自然语言→定义）/
  related（符号→语义邻居）；doc comment 纳入向量；指纹缓存秒级
  - 真仓库实测：VF3 中文查询"轮子 载具 速度 驱动"0.32s 命中 drive 语义簇；
    related(wheel_speed)→DriveCommand 0.866 / drive_fraction 0.819
  - 诚实边界：tf-idf 是词面向量，中英跨语言需嵌入模型（重依赖，未采用）；
    跨语言靠仓库内中文注释桥接

## 五、当前基线

- **180 passed** / SCHEMA_BAD 0 / CI 常驻
- 工具面 **37**（12 域）全过对抗测试；code_search(BM25 文件级) + 
  code_semantic(符号级向量) 互补
- 评测三套（L3 judge / P3 verified+judge / P1 P/R）统一口径
  bench/unified_report.py（verified 主、judge 辅）

## 五、挂账（不丢，但不值得单独开轮）

| 事项 | 原因 |
|---|---|
| astropy-8872 环境 | 2015 年代，py≤3.6，构建链太老 |
| django-15280 执行验证 | 数据集 FTB 字段是整句话，非测试 id |
| requests-2931 | 数据集 node id 与 commit 实际类名漂移 |
| P3 样本 47→100+ | 纯堆量，把 verified 口径的显著性做实 |
