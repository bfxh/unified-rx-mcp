# c 语言 skill（ide 域）
- 构建：gcc -fsyntax-only 逐文件（无 make/cmake 的诚实降级）；**LC_ALL=C**
  强制英文消息（本地化会破坏正则）
- 调试：运行时崩溃只报 exit 信号（无 gdb 帧）——如需帧，装 gdb 后扩展
- 坑：msys64 gcc 在 PATH；-fsyntax-only 免链接最快
