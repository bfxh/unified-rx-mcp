# search 域（code_search / code_semantic）
- code_search：**BM25 词面**，文件级；符号原文加权重排（S13）；指纹缓存
  首查 ~0.4s → 复查 <10ms
- code_semantic：**tf-idf 余弦**符号定义级向量；mode=related 给语义邻居；
  doc comment 折入向量（中文注释可桥接中英查询）
- 里子：都不是神经网络嵌入；跨语言查询（中问英码）仅靠注释桥接。
  指纹缓存键 = 全部索引文件 (mtime_ns,size)，改文件自动失效
