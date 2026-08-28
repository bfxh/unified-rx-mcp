# ide 域（8 工具：locate_edit/code_context/ide_edit_multi/ide_rename/ide_build/ide_debug/ide_break/ide_lsp）
- **ide_edit_multi**：内容匹配（非行号），CRLF 保留，`dry_run: true` 出
  unified diff 预览不落盘（S34）；模拟在副本上整段跑，mismatch 不会半应用
- **ide_build**：按构建标记路由 Cargo.toml→cargo check/test/clippy（lint）、
  go.mod→go build、.java→javac、.c/.cpp→gcc/g++ -fsyntax-only、.py→compileall。
  诊断缓存：源指纹失效判定（S34）。向上找最近构建根
- **ide_debug**：argv 列表直跑不走 shell（schema 拒 str）；RUST_BACKTRACE=1
  自动；解析 rust panic（新旧双格式+回溯帧）/py traceback/java 堆栈/go panic/
  pytest FAILED+E 断言
- **ide_break**：轻依赖断点——python sys.settrace 记录器（locals+栈）、
  java jdb 脚本化、go dlv trace；**rust 需 gdb/lldb，缺失如实报错**
- **ide_lsp**：真 JSON-RPC，仅 rust-analyzer/pylsp；diagnostics 靠 pump 拉推
- **ide_diagnostics**（S37 统一通道）：LSP+clippy 聚合同形状
  {source,file,line(1-based),severity,message}，修复循环直接消费
- 坑：JDK/gcc 本地化消息（中文"错误"）破坏诊断正则 → javac 强制
  `-J-Duser.language=en`、gcc `LC_ALL=C`；pytest 语法错误走 stdout 非 stderr；
  CPython 3.11+ line 事件 trace 返回 None 不关帧追踪（必须 sys.settrace(None)）
