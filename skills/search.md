# search 域（code_search / code_semantic）
- code_search：**BM25 词面**，文件级；符号原文加权重排（S13）。S80 起引擎
  Rust 原生化（rx-search.exe，`rust/src/search.rs`），Python 侧薄壳转调，
  exe 缺失报清晰错误不静默降级。语料遍历 = 每层先本目录文件再下钻（os.walk
  结构）+ 目录内 NTFS upcase 排序，200 上限截断由此定序（S80 对照实验实锤
  定契约）；单次调用全流程 ~140ms（进程启动+建索引+查询），S12 进程内指纹
  缓存随之退役（短命 exe 无从缓存，冷调已比旧 Python 首查 297ms 快一倍）
- code_semantic：**tf-idf 余弦**符号定义级向量；mode=related 给语义邻居；
  doc comment 折入向量（中文注释可桥接中英查询）——仍为纯 stdlib Python
  实现（S81 再议迁移）
- 契约变化（S80）：空查询从 total=0 改为显式拒绝（exe 用法级 exit 2 →
  ok:false "query 必填"）；engine.py BM25 降级路径的结果形状不变
  （file/line/score/snippet）
