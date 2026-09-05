# search 域（code_search / code_semantic）
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
- **契约变化（S88）**：code_search/code_semantic 的 root（含默认 cwd）先过
  沙盒钳制——越界返回 `{"error": "路径越界（沙盒外）：…"}`（S73 纪律补全）
