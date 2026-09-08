# search 域（code_search / code_semantic / repo_map）
- code_search：**BM25 词面**，文件级；符号原文加权重排（S13）。S80 起引擎
  Rust 原生化（rx-search.exe，`rust/src/search.rs`），Python 侧薄壳转调，
  exe 缺失报清晰错误不静默降级。语料遍历 = 每层先本目录文件再下钻（os.walk
  结构）+ 目录内 NTFS upcase 排序，200 上限截断由此定序（S80 对照实验实锤
  定契约）；单次调用全流程 ~140ms（进程启动+建索引+查询），S12 进程内指纹
  缓存随之退役（短命 exe 无从缓存，冷调已比旧 Python 首查 297ms 快一倍）
- code_semantic：**tf-idf 余弦**符号定义级向量；mode=related 给语义邻居；
  doc comment 折入向量（中文注释可桥接中英查询）。S81 起引擎 Rust 原生化
  （rx-semantic.exe，`rust/src/sem.rs`）：四语言七定义匹配器（py def/class、
  rs fn/type/impl 含 `for` 回溯、go 接收者、js fn/class；.ts/.tsx/.jsx 不算
  js 的怪癖原样保留），Python 侧薄壳转调；实测 ~330ms vs 旧 Python ~930ms，
  S31 进程内缓存随之退役
- 大查询通道（S81）：超 _QUERY_ARGV_CAP=10000 字符的查询不经 argv（Windows
  命令行上限 32767 码元），两工具同款走 stdin（argv 传 "-"）
- 契约变化（S80）：code_search 空查询从 total=0 改为显式拒绝（exe 用法级
  exit 2 → ok:false "query 必填"）；**code_semantic 空 query 仍合法**
  （search 返回 total=0、related 返回模糊锚点——S31 契约保留）
- engine.py BM25 降级路径的结果形状不变（file/line/score/snippet）
- **结果缓存（S103）**：code_search/code_semantic/repo_map 的结果进入进程内
  内容寻址缓存（键含输入指纹与 cursor；文件变更即失效；`__no_cache: true` 旁路）。
  实测 bug_scan 48→3.7ms、ast_scan 45→3.7ms（约 12×）；整仓 repo_map 因指纹
  本身要读全仓小文件，命中收益较小（~1.8×）——边界如实写在 tools/cache.py。
- **契约变化（S88）**：code_search/code_semantic 的 root（含默认 cwd）先过
  沙盒钳制——越界返回 `{"error": "路径越界（沙盒外）：…"}`（S73 纪律补全）
- **契约变化（S101）**：①**查询资格门**——文档整词必须包含某个"查询根词"
  （标识符类词取整词 + 去分隔符连写变体；普通小写词取整词），只被查询标识符
  的**子词**命中的文档不再入选（查 `HTTPSConnection` 不再带出 "HTTPS handshake
  failed for Connection" 的文档；`parse_json` 仍能命中 `parseJson`、前缀
  `auth_gate_sweep` 仍能命中 `AUTH_GATE_SWEEP_MARKER`；纯 CJK 查询门不生效）。
  索引侧仍拆子词（查 `mapping` 能中 `FooMapping`）——"索引拆、查询不因拆词放宽"。
  ②`code_search(hybrid=true)`：与 code_semantic 定义级结果做 **RRF 融合**
  （k=60，免归一化），hits 增 `rrf`/`bm25_rank`/`semantic_rank`/`symbol`/`kind`，
  顶层增 `hybrid`/`rrf_k`/`paths`；语义路不可用时**显式降级**（hybrid=false +
  degraded 原因 + BM25 结果完整），默认 false 时输出与旧版同形。
  实测（tools 域，`sandbox resolve`）：融合把语义路 #1、BM25 #3 的
  `_resolve_in_sandbox` 顶到第一，压过只被 BM25 命中的注释行。
- **repo_map（S102）**：仓库符号地图——定义/引用图 + **个人化 PageRank** + token
  预算裁剪（算法源自 aider repo map，零依赖自研，实现在 `rust/src/repomap.rs`，
  rx-ide repomap 子命令）。图：file --引用次数--> def，file --包含--> 自己的
  def，def --1--> 所属 file；个人化 = focus 命中的文件与其定义 ×50（其余 ×1）。
  定义用 `ide::symbol_spans` 四语言同口径；引用 = 全仓词法扫描命中已知定义名。
  输出 `map` 每行 `相对路径:行 kind 名字`（按 rank 降序，预算 = chars/4 近似）；
  顶层 `defs_total/defs_shown/tokens_est/truncated/engine`。focus 匹配口径：
  全等/路径后缀（`fs.py`）/目录前缀（`tools`）/词干（`fs`）。**双偏置**（S102 补记）：
  遥传向量 ×50（让相关定义经图传播受惠）+ 最终排名再乘 50——只有遥传时，在
  "内部互引密集的大文件簇"（如快照语料）面前会被图结构淹没；`max_files` 截断
  时 focus 文件优先入队（发现上限 = max(4×max_files, 2000)，读取解析仍只对
  入选的 max_files 个文件做）。
  实测：全仓 focus=search → top 为 `tools/search.py` 与 `rust/src/search.rs` 的
  定义（无 focus 时是快照语料里的高频被引者）。
  **简化边界（如实）**：引用归属算给"包含引用的文件"而非"包含引用的定义"；
  同名定义共享权重；token 用字符/4 近似——定位是"该看哪些定义"的骨架，
  不是精确调用图。
