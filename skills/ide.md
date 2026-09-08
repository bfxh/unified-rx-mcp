# ide 域（19 工具：读取/编辑/构建/调试/诊断/测试/体检/语义）
- **ide_edit_multi**：内容匹配（非行号），CRLF 保留，`dry_run: true` 出
  unified diff 预览不落盘（S34）；模拟在副本上整段跑，mismatch 不会半应用；
  S55 语法门（py 结果不可编译整批拒不落盘）；S55 `validate: true` 写前 LSP
  验证（error 拒写）；S60 BOM 文件匹配修复（\ufeff 剥离还原）；S61
  `fuzzy: true` 空白容忍查找；>10MB 拒编辑（防截断静默丢内容）
- **locate_edit / code_context / ide_rename**（S93 原生化）：定位三件转调
  rx-ide.exe（唯一实现在 rust/src/ide.rs），S93 对照实验 51 场景 masked 全等。
  locate_edit：全库模糊定位——忽略大小写命中、limit*3 双层停机、
  references_in_scan 区分大小写（含触发停机文件）、默认 max_files=100 /
  limit=10、10 扩展名判型（非代码不占额度）、13 跳过目录、snippet=前 1 行+
  当前行+后 3 行；code_context：光标行窗口——radius 0=缺省 30、钳 5-200、
  cursor 0=头窗、RAW split 保留 \r 与尾幻影行、负 cursor 出负 end（Python
  负切片）、>10MB 拒读（getsize 门在沙盒 resolve 之前——沙盒外文件报
  "文件不可读"）；ide_rename：只出预案不落盘——固定 200 文件帽、空符号
  count("")=len+1 怪癖、note 提示确认后用 fs_write 应用。遍历 junction
  下钻/悬空静默剪（os.walk 3.14 口径）；定位三件的解析类失败（越界/非目录/
  path 必填/query 为空）走工具级 {"error": ...} 包络
- **ide_build**（执行类需授权）：按构建标记路由 Cargo.toml→cargo check/test/clippy（lint）、
  go.mod→go build、.java→javac、.c/.cpp→gcc/g++ -fsyntax-only、.py→compileall。
  诊断缓存：源指纹失效判定（S34）。向上找最近构建根
- **ide_debug / ide_break**（执行类需授权）：argv 列表直跑不走 shell（schema 拒 str）；
  RUST_BACKTRACE=1 自动；解析 rust panic（新旧双格式+回溯帧）/py traceback/
  java 堆栈/go panic/pytest FAILED+E 断言；ide_break=python settrace 记录器
  （locals+栈+条件断点）、java jdb、go dlv；**rust 断点需 gdb/lldb，缺失如实报错**
- **ide_test**（执行类需授权，S57）：pytest/cargo test/go test 一条命令 →
  per-test 结构化结果+失败帧；cargo workspace 多 crate result 行全量累加（S63）；
  收集到 0 个测试显式报出（exit 5）；target 拒 '-' 旗标（防 argv 注入）。
  **S104 测试影响分析（TIA，仅 pytest，`tia=true`）**：首次全量并建立依赖图
  （pytest 插件 `urx_tia_plugin` 用 audit hook 记录——收集期按文件、执行期按
  nodeid，`.pyc` 映射回源文件）→ 之后只跑依赖集与"变更文件集"相交的测试；
  无依赖记录/新测试/收集失败一律全量（宁多跑不误跳）；`full=true` 强制全量。
  无受影响测试时**不执行**并如实报 `mode=skipped-no-impact`。依赖图进程内保存
  （跨重启重来）。cargo/go 目标报 `mode=unsupported` 并全量执行。
- **ide_doctor**（执行类需授权，S59）：一键体检六项聚合（scan/review/build/
  test/dep/stability）→ verdict（clean/warn/issues）+ problems/warns；
  没写测试=黄灯
- **ide_lsp**：真 JSON-RPC，仅 rust-analyzer/pylsp；diagnostics 靠 pump 拉推；
  会话根向上找 .git（S60：src/ 与 tests/ 共享一个服务器）；S55 validate_content
  写前验证；S62 入站帧 64MB 上限。S99：status 对 `python -m <mod>` 形态**验到
  模块层**——pylsp 未装时如实 detected=false + reason（旧版 exe=解释器本身，
  which 检查假阳性，违反"绝不假装支持"）
- **ide_diagnostics**（S37 统一通道）：LSP+clippy 聚合同形状
  {source,file,line(1-based),severity,message}，修复循环直接消费
- **ide_health_trend**（S69，低优缓迁移）：读自动驾驶历史 JSONL，输出最近 N 次
  体检时间线与 per-project verdict 变化（root 参数是记录字段过滤，非路径读）。
- **scip_refs（S112）**：SCIP 索引消费（只读）——外部索引器（`rust-analyzer scip .`、
  scip-python 等）产出的紧凑代码情报索引，本工具手写 protobuf 解析（零依赖），
  给出符号的定义/引用位置（`files[].lines/defs`），**不起 LSP 会话**。不生成索引；
  索引新鲜度由生成方负责（如实标注）。
- **ide_impact**（S58）：符号 → LSP references 按文件聚合+测试覆盖标注
  （python test_<stem>.py 约定代理）——改前先看碰哪些裸奔文件。**三级降级
  （S99/S109，engine 字段如实标注）**：①`lsp` 语义级（需 rust-analyzer/pylsp）；
  ②`resolved` 解析级——名字解析（S107/S108）给出"谁 import 了这个定义"
  （跨文件精确到 import 行、**别名绑定也覆盖**）＋同文件精确到引用行；③`text`
  文本级兜底（大小写敏感全文计数，含注释/字符串、无行号，受 200 文件帽限制）。
  取不到符号/exe 缺失/解析失败 → 逐级回落，最终包络给出清晰错误。
  实测（本仓 `_resolve` 定义）：解析级 21 文件 23 处（含 `_fs_resolve` 别名），
  文本级同符号假阳性率 93.9%（bench/results/s108_resolve_compare.json）。
- **ide_rename / rename_apply**（S58）：rename_plan 只出预案；rename_apply
  落盘需 `__authorized: true`，UTF-16 列正确、CRLF 保留、逐文件沙盒防逃逸、
  非 file: uri 拒绝
- **ide_batch_edit**（S65）：跨文件行块批量替换（同 ide_edit_multi 匹配语义），
  默认 dry_run per-file diff 预览，apply=true 落盘；py 单文件语法门失败只跳过
  该文件不挡批次；>10MB 跳过；白名单 files 可缩范围
- **ide_outline / ide_read_symbol**（S66 立面 / S92 原生化）：结构大纲与按名精读——
  四语言（python/rust/go/javascript）行级符号清单（kind: fn/type、起止行、参数数）、
  大纲 300 符号上限；params 计数口径 outline 只认 fn、read_symbol 只认括号；
  唯一实现在 rust/src/ide.rs（rx-ide.exe），零 LSP 依赖毫秒级（S66 立意：
  高频动作不付语言服务器成本）；S92 对照实验 43 场景 masked 全等——CRLF/
  尾幻影行/unicode 标识符/一行 fn 含 struct 翻 type/js class 落 fn 等
  S70 语义怪癖逐字节对齐
- 坑：JDK/gcc 本地化消息（中文"错误"）破坏诊断正则 → javac 强制
  `-J-Duser.language=en`、gcc `LC_ALL=C`；pytest 语法错误走 stdout 非 stderr；
  CPython 3.11+ line 事件 trace 返回 None 不关帧追踪（必须 sys.settrace(None)）；
  pylsp 定义跳转需 jedi<0.20（0.20 goto 空，环境已钉 0.19.2）
