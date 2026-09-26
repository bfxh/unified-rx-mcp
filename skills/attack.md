# attack 域（input_fuzz/path_probe/big_input/auth_gate_sweep/rust_taint_scan/attack_cruise）
- 自写对抗用例生成器：路径逃逸/注入串/大输入三类
- 里子：不是 hypothesis 属性测试；S29 的 5 洞是人工对抗测试抓的
- auth_gate_sweep（S77，VULN-HUNTING P0-a）：全工具授权门自审——挂门必拒未授权
  （端到端 registry.call 空参验证，授权检查先于 handler 零副作用）/ schema 必声明
  __authorized（S72b 契约）/ manifest 高权限段一致；单工具混合读写用
  `manual_gate=True` 显式声明（ide_lsp 范例），"收 __authorized 无任何声明"即假门
  → ok:False。S75 人眼盘点法固化成工具，新工具一注册就自动被查。**S132/H3：
  增「组合透传」静态自审**——扫 tools/+bench/ 里 `registry.call("<挂门工具>", …)`
  是否**字面量**携带 `__authorized`（包装器隐式注入不算数）；首跑即抓出
  agent_selfcheck→app_clone、swe_repair→ide_break 两处真缺口（被门拒后静默
  退化，同 S130 病灶型）——授权三档立文见 HARDENING §七
- rust_taint_scan（S78，VULN-HUNTING P1-a；**S128 跨文件链**）：Rust 污点引擎 rx-taint 的
  Python 壳——root 过 _fs_resolve 沙盒后交给 exe 扫描，返回来源→汇点发现（kind=definite/
  clue/naive 三级；definite=宿主入口可达）。**跨文件传播默认开启**：全扫描集唯一名才连边
  （同名多义如实跳过并计数 cross_skipped_ambiguous），名解析消费 nameres 调用图
  （from-import 别名/模块属性调用可连，S125 同一作用域引擎）；链证据 `origin` 随发现
  返回（flow=cross）——"某文件:行 来源(种类) → 目标文件:函数.形参"；净化器跨界规则
  不变（实参被净化 or callee 内净化都截断链）；cross=false 逐字节回到 S78 文件内
  语义（A/B 对照）。exe 自动发现 TEMP/rx-rs-target，缺失时清晰报错不静默降级；
  naive=true 走旧全参数模式供对照验收
- **attack_cruise（S133，CONSOLIDATION §四 P3）**：全攻击面一键巡航——薄聚合
  （同 ide_doctor 惯例，不造新检测）：授权门自审（auth_gate_sweep）+ 被动探针
  （path_probe 8 形态）+ 主动模糊（input_fuzz × big_input）。默认四靶电池=对本包
  自身回归式对抗（fs_read/locate_edit/code_search/bug_scan，`<pkg>` 占位替换为包目录）；
  `targets` 增补任意工具（{tool_name, base_args, fuzz_field}）、`battery=false` 只跑增补、
  `big=false` 跳大输入。输出统一报告：gates/passive/fuzz/big + **failures/errors
  全量列出不吞** + verdict（clean/issues）。档位（HARDENING §七）：纯自审不挂门；
  增补挂门靶时其模糊调用得「授权拒绝」= PASS-reject 属合法判定。

### cargo_audit —— Rust 依赖安全审计（RustSec 薄壳，执行类需授权）

经 **cargo-audit --json** 的能力探测薄壳：RustSec 官方 advisory 库逐条漏洞
（id/package/版本/title/patched）+ unmaintained 等警告摘要。执行外部子进程
⇒ `requires_auth`（与 ide_build/ide_diag 同款纪律）。能力探测：cargo-audit
缺失如实 `available=false` + 安装 hint（cargo install cargo-audit --locked），
装好即自动可用。`stale=true` 跳过 DB 更新（离线环境；DB 可手动克隆到
~/.cargo/advisory-db）。诚实边界：只覆盖 Cargo.lock 依赖树，DB 时效由上游管，
本工具不做修复——升级/换 crate 是人的决策。放 attack 域的理由：与
rust_taint_scan 同为「Rust 项目安全检查」族，且 core 首屏裁剪面不收低频审计
（渐进披露）。实测：本仓 rust/（零依赖）→ 0 漏洞 0 警告 exit 0；本机 TLS
拦截环境下 advisory DB 手动克隆 + `--stale` 实测可用。
