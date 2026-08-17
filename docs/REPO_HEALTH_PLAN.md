# repo_health 代码库健康四理念（去重 / 剔残缺 / 分支 / 标矛盾）

> 2026-08-17 · 用户理念：**去重、剔残缺、分支、标矛盾**是主要目标理念之一。
> 文档先行（用户工作流：先方案后代码）。

## 一、定位

`repo_health` 是一个 **action 分发的工具族**（与 `mesh`/`telemetry` 组合化风格一致），
四个检测维度 + 一个汇总入口，避免工具数膨胀：

| action | 理念 | 检测内容 | 输出 |
|---|---|---|---|
| `dedup` | 去重 | 全库相似文件对（内容哈希+相似度）、重复代码块、重复工具/函数定义 | 相似文件对 + 重复块位置 |
| `incomplete` | 剔残缺 | 空实现（pass/.../NotImplementedError）、TODO/FIXME 堆积、占位符、断引用（import 失败/符号不存在） | 残缺清单（按严重度排序） |
| `branch` | 分支 | git 分支状态（未合并/已合并/分叉点）、游离 HEAD、未提交改动 | 分支健康报告 |
| `conflict` | 标矛盾 | 同名符号多处定义、规范冲突（std_check 规则打架）、逻辑互斥代码对（同一文件矛盾注释 vs 实现） | 矛盾清单 |
| `all` | 汇总 | 四维一屏 + 健康评分（0-100） | 总报告 |

## 二、检测规则定义

### dedup（去重）
- 文件级：按内容 SHA-256 分组 → 完全相同文件组；再按"行集合 Jaccard 相似度 ≥ 0.8"找近似重复文件对
- 块级：函数体/类体提取后做行级归一化（去空白/注释）→ 相同签名+相同体 = 重复块
- 排除：vendor/、node_modules/、__pycache__/、.git/、dist/、target/、models/
- 上限：相似对最多返回 20 对（防上下文爆炸）

### incomplete（剔残缺）
- 空实现：函数/方法体只含 `pass`、`...`、`NotImplementedError`、`raise NotImplementedError`、空 return
- 占位符：`TODO`、`FIXME`、`XXX`、`HACK`、`placeholder`、`coming soon`、`待实现`、`XXX 实现`、
  `foo`/`bar`/`baz` 等无意义命名（仅作提示不判死）
- 断引用：Python import 语句解析失败（模块不存在）；AST 中 Name 引用无定义（粗检：按文件作用域收集）
- 严重度：空实现=high，TODO 堆积（同文件 ≥3）=medium，占位符=low
- 上限：单文件最多报 20 条，全库最多 100 条

### branch（分支）
- 需要 git 仓库：`git branch -v`、`git merge-base`、`git log` 解析
- 未合并分支（merge-base 之后有提交且未合并入 HEAD）、已合并分支、当前分支分叉点距离
- 游离 HEAD、未提交改动数量（git status --porcelain 计数）
- 非 git 目录：报告"非 git 仓库（跳过分支检查）"，不报错

### conflict（标矛盾）
- 同名符号多处定义：AST 收集每个文件的顶层函数/类名 → 跨文件同名冲突（排除测试文件与
  `_`/`__` 开头）
- 规范冲突：调用 `std_check` 规则集时同一文件同时命中"禁止 X"与"必须 X"类规则（规则组配置）
- 注释-实现矛盾：`# TODO: 已修复` / `# FIXME` 但实现完整（启发式：注释标记 vs 空实现状态反向）
- 上限：同名冲突最多 20 组

## 三、接口

```
repo_health action=dedup|incomplete|branch|conflict|all
            root=<项目根，默认 UNIFIED_RX_PROJECT 或 cwd>
            top=<结果上限，默认 20>
```

返回 `{ok, action, root, items, summary, score?, elapsed_ms}`。

## 四、实现与测试

- 实现：`repo_health.py`（纯 stdlib：ast/hashlib/git 命令子进程）
- 注册：server.py `_TOOLS["repo_health"]`（schema: action 必填枚举 + root + top）
- 测试：`test_repo_health.py`——用临时目录构造：重复文件对/空实现/TODO/同名符号/
  假 git 分支（用 git init 真实仓库）→ 断言检测命中
- 安全：只读（不修改任何文件）；路径规范化防越界（root 限定）

## 五、验收

1. `pytest test_repo_health.py` 全过
2. 对 unified-rx 自身跑 `repo_health action=all`：应有 dedup/incomplete/conflict 发现且无异常
3. 非 git 目录跑 branch：graceful 降级不炸
