# ide 域（17 工具：编辑/构建/调试/诊断/测试/体检/语义）
- **ide_edit_multi**：内容匹配（非行号），CRLF 保留，`dry_run: true` 出
  unified diff 预览不落盘（S34）；模拟在副本上整段跑，mismatch 不会半应用；
  S55 语法门（py 结果不可编译整批拒不落盘）；S55 `validate: true` 写前 LSP
  验证（error 拒写）；S60 BOM 文件匹配修复（\ufeff 剥离还原）；S61
  `fuzzy: true` 空白容忍查找；>10MB 拒编辑（防截断静默丢内容）
- **ide_build**（执行类需授权）：按构建标记路由 Cargo.toml→cargo check/test/clippy（lint）、
  go.mod→go build、.java→javac、.c/.cpp→gcc/g++ -fsyntax-only、.py→compileall。
  诊断缓存：源指纹失效判定（S34）。向上找最近构建根
- **ide_debug / ide_break**（执行类需授权）：argv 列表直跑不走 shell（schema 拒 str）；
  RUST_BACKTRACE=1 自动；解析 rust panic（新旧双格式+回溯帧）/py traceback/
  java 堆栈/go panic/pytest FAILED+E 断言；ide_break=python settrace 记录器
  （locals+栈+条件断点）、java jdb、go dlv；**rust 断点需 gdb/lldb，缺失如实报错**
- **ide_test**（执行类需授权，S57）：pytest/cargo test/go test 一条命令 →
  per-test 结构化结果+失败帧；cargo workspace 多 crate result 行全量累加（S63）；
  收集到 0 个测试显式报出（exit 5）；target 拒 '-' 旗标（防 argv 注入）
- **ide_doctor**（执行类需授权，S59）：一键体检六项聚合（scan/review/build/
  test/dep/stability）→ verdict（clean/warn/issues）+ problems/warns；
  没写测试=黄灯
- **ide_lsp**：真 JSON-RPC，仅 rust-analyzer/pylsp；diagnostics 靠 pump 拉推；
  会话根向上找 .git（S60：src/ 与 tests/ 共享一个服务器）；S55 validate_content
  写前验证；S62 入站帧 64MB 上限
- **ide_diagnostics**（S37 统一通道）：LSP+clippy 聚合同形状
  {source,file,line(1-based),severity,message}，修复循环直接消费
- **ide_impact**（S58）：符号 → LSP references 按文件聚合+测试覆盖标注
  （python test_<stem>.py 约定代理）——改前先看碰哪些裸奔文件
- **ide_rename / rename_apply**（S58）：rename_plan 只出预案；rename_apply
  落盘需 `__authorized: true`，UTF-16 列正确、CRLF 保留、逐文件沙盒防逃逸、
  非 file: uri 拒绝
- **ide_batch_edit**（S65）：跨文件行块批量替换（同 ide_edit_multi 匹配语义），
  默认 dry_run per-file diff 预览，apply=true 落盘；py 单文件语法门失败只跳过
  该文件不挡批次；>10MB 跳过；白名单 files 可缩范围
- 坑：JDK/gcc 本地化消息（中文"错误"）破坏诊断正则 → javac 强制
  `-J-Duser.language=en`、gcc `LC_ALL=C`；pytest 语法错误走 stdout 非 stderr；
  CPython 3.11+ line 事件 trace 返回 None 不关帧追踪（必须 sys.settrace(None)）；
  pylsp 定义跳转需 jedi<0.20（0.20 goto 空，环境已钉 0.19.2）
