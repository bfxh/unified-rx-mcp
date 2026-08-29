# cpp 语言 skill（ide 域）
- 构建：g++ -fsyntax-only 逐文件（同 c.md，扩展名 .cpp/.cc/.cxx）
- 调试：同 c——运行时崩溃无帧（无 gdb）；编译诊断 gcc 同构格式
- 坑：C++ 文件会同时被 .c 规则误判 → ide_build 按 .cpp/.cc/.cxx 优先路由 g++
