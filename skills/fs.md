# fs 域（fs_read/fs_write/fs_stat/fs_list）
- 沙盒：`_fs_resolve` 强制 `_sandbox_roots` 白名单；**roots 为空 = 全拒**（fail-closed）
- pytest 环境由 conftest 注入 roots；直跑脚本需自设
- fs_read 上限 1MB；fs_write 需 `__authorized: true`（registry 声明式强制）
- 坑：路径穿越（../、绝对盘符）一律拒——S29 fuzz 已锁
- S79：读面三工具（fs_read/fs_stat/fs_list）= Rust 原生（rx-fs.exe），Python 侧只剩
  薄壳转调（exe 缺失报清晰错误不静默降级）；沙盒语义 Rust 侧等价复刻
  （rust/src/sandbox.rs，宽限 realpath 容忍不存在路径）；fs_write 仍 Python 原生。
  契约变化一条：fs_list depth=0 现在字面生效（仅根层）——旧实现 `depth or 1`
  曾把 0 静默强制成 1
