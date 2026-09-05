# appaudit 域（app_clone/app_audit/app_clean）——S86 全域薄壳收官
- Electron asar 解包 + 凭据/密钥掩码扫描；双克隆对照
- 隔离三保证：克隆唯一落点（时间戳-哈希-净化名）防名注入；audit 只收沙箱内路径；clean 需 `__authorized` 且严格限沙箱内
- S85/S86：三工具全部 Rust 原生化——app_audit=rx-audit.exe（rust/src/appaudit.rs）、
  app_clone/app_clean=rx-appops.exe `clone|clean` 子命令（rust/src/appclone.rs）。
  Python 侧只剩薄壳转调，exe 缺失报清晰错误不静默降级
- 授权门结构性留 Python registry（requires_auth + `__authorized` 于 registry.call
  统一强制），exe 永不自行放权；沙盒门两语言各一版（appaudit.rs::strictly_under
  ↔ tools/appaudit.py::_strictly_under），等价性由 oracle 对拍钉死
- Python 3.14 克隆语义（Rust 逐条复刻）：junction 不再是 symlink（islink=False、
  悬空也算目录）——有效 junction 目标内容被克隆进 junction 名下、悬空静默剪枝、
  均不进 skipped_links（那只数真 symlink）；junction 判别走 reparse tag 手写 FFI
  （Rust read_link 已剥设备前缀，文本不可判别）；清单根层 rel=""（"\t{size}\n"）；
  os.path.relpath 的 Win32 GetFullPathName 归一（成分尾部空格/点剥除）在 errors
  显示侧复刻；时间戳 GetLocalTime FFI（零 crate 红线）
- 坑：想审原件 = 结构性拒绝——先 app_clone 再把返回的 snapshot 路径传进来
- oracle：S85 app_audit 10 场景逐字节一致（CRLF/坏 UTF-8/BOM/8 上限截断/9MB 跨窗
  asar 头/错误包络类名前缀，存档 %TEMP%\s85\）；S86 写面 24 步 norm 后全 PASS
  （junction 双形态/尾点文件/预算四档/SchemaError 前置门/只读位+mtime 保真/清理门
  九态/授权门，存档 %TEMP%\s86\；已知偏差：OS 错误消息文本跨运行时发散，只比类名）
