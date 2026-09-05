# appaudit 域（app_clone/app_audit/app_clean）
- Electron asar 解包 + 凭据/密钥掩码扫描；双克隆对照
- 隔离三保证：克隆唯一落点（时间戳-哈希-净化名）防名注入；audit 只收沙箱内路径；clean 需 `__authorized` 且严格限沙箱内
- app_audit（S85）：Rust 原生化 rx-audit.exe——JS 危险面 6 规则/秘密 5 规则（只落掩码前 6 字符+***len=N，绝不回显原值）/URL 清单+ai 宿主/二进制盘点/asar SHA256 自标定提取后复扫（label 前缀 `asar:`）。实现唯一事实源 rust/src/appaudit.rs；Python 侧薄壳，exe 缺失报清晰错误不静默降级
- 沙盒门：审计走 Rust 侧 strictly_under（normcase 严格子判定，等价复刻）；app_clean 走 Python 侧 _strictly_under——双实现等价性由 oracle 逐字节对拍钉死
- 坑：想审原件 = 结构性拒绝——先 app_clone 再把返回的 snapshot 路径传进来
- oracle：旧 Python 实现 vs Rust exe 10 场景逐字节一致（含 CRLF/坏 UTF-8/BOM/8 上限截断/9MB 跨窗 asar 头/错误包络类名前缀），存档 %TEMP%\s85\
