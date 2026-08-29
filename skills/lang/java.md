# java 语言 skill（ide 域）
- 构建：javac 全量 `-d out`（无 mvn/gradle 时的诚实降级；pom.xml 存在但无
  mvn 也走此路并如实）
- **强制 `-J-Duser.language=en -J-Duser.country=US`**：JDK 本地化中文消息
  （"错误"）会击穿诊断正则
- 调试：jdb 脚本化馈送（stop in/at → locals → where → cont），**命令节奏
  用 stdin 一次性馈送 + 输出按状态解析**；class 名白名单校验（防 jdb 注入）
- 坑：jdb 输出本地化由 JAVA_TOOL_OPTIONS 控制；堆栈首行有
  `Exception in thread "main"` 前缀（解析需兼容）
