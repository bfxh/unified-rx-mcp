# §3 — 代码库认知契约（cb_index / cb_scan / cb_status）

## 3.1 输入契约（MUST）

1. `cb_index(path)`：`path` **MUST** 为代码库根目录；索引全库符号+哈希+变更感知。
2. `cb_scan(path, max_files=200)`：变更优先扫描（增量感知）。
3. `cb_status()`：无必填参数，返回索引状态。

## 3.2 输出契约（MUST）

1. `cb_index` **MUST** 返回索引元信息（索引路径/文件数/耗时），**MUST NOT** 返回假成功
   （索引失败要报错）。
2. `cb_scan` **MUST** 变更优先：改过的文件排在前面（增量认知，省 token——session 核心诉求）。
3. 输出项 **MUST** 带 `file:line` 证据；snippet 截断 ≤160 字符。

## 3.3 增量与缓存（MUST，probe_08 断言）

1. 相同文件二次扫描 **MUST** 命中缓存（mtime+hash 双检），不重复索引。
2. 文件外部改动 **MUST** 通过 mtime+hash 检测，不得误判"未变"。
3. 索引缓存 **MUST** 有上限（热层 LRU），防膨胀。

## 3.4 与 search_index / repo_graph 的关系（SHOULD）

1. `search_index`（语义搜索）与 `repo_graph`（符号图）**SHOULD** 复用 cb 索引产物，
   不重复建索引。
2. 扩展索引（tree-sitter 符号）**SHOULD** 懒加载——首次用到才构建，省内存。
