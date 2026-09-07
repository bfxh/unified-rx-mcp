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
- S90：fs_write 收官——fs 域 4/4 全走 rx-fs.exe。写内容经 stdin **二进制字节通道**
  （argv 不传内容，绕开 Windows 命令行 32767 码元上限；text 模式 stdin 会做
  \n→os.linesep 换行翻译——探针实锤，scan/search 同源通道同轮二进制化）。
  契约不变项：授权门仍在 registry（requires_auth=True，exe 永不自行放权）；
  size=Unicode 字符数（非字节）；大小上限先于沙盒检查（超大+越界同中报"内容过大"）。
  已知偏差（S86 掩码口径）：OS 错误文本尾段发散（[WinError 5] vs (os error 5)）。
  fs.py 写盘原语（os.replace/urxtmp 拼接）退役，`_resolve` 保留（oracle 锚 +
  scan/search/game/ops 导入面）
