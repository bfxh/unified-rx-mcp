# fs 域（fs_read/fs_write/fs_stat/fs_list）
- 沙盒：`_fs_resolve` 强制 `_sandbox_roots` 白名单；**roots 为空 = 全拒**（fail-closed）
- pytest 环境由 conftest 注入 roots；直跑脚本需自设
- fs_read 上限 1MB；fs_write 需 `__authorized: true`（registry 声明式强制）
- 坑：路径穿越（../、绝对盘符）一律拒——S29 fuzz 已锁
