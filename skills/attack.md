# attack 域（input_fuzz/path_probe/big_input/auth_gate_sweep/rust_taint_scan）
- 自写对抗用例生成器：路径逃逸/注入串/大输入三类
- 里子：不是 hypothesis 属性测试；S29 的 5 洞是人工对抗测试抓的
- auth_gate_sweep（S77，VULN-HUNTING P0-a）：全工具授权门自审——挂门必拒未授权
  （端到端 registry.call 空参验证，授权检查先于 handler 零副作用）/ schema 必声明
  __authorized（S72b 契约）/ manifest 高权限段一致；单工具混合读写用
  `manual_gate=True` 显式声明（ide_lsp 范例），"收 __authorized 无任何声明"即假门
  → ok:False。S75 人眼盘点法固化成工具，新工具一注册就自动被查
- rust_taint_scan（S78，VULN-HUNTING P1-a；**S128 跨文件链**）：Rust 污点引擎 rx-taint 的
  Python 壳——root 过 _fs_resolve 沙盒后交给 exe 扫描，返回来源→汇点发现（kind=definite/
  clue/naive 三级；definite=宿主入口可达）。**跨文件传播默认开启**：全扫描集唯一名才连边
  （同名多义如实跳过并计数 cross_skipped_ambiguous），名解析消费 nameres 调用图
  （from-import 别名/模块属性调用可连，S125 同一作用域引擎）；链证据 `origin` 随发现
  返回（flow=cross）——"某文件:行 来源(种类) → 目标文件:函数.形参"；净化器跨界规则
  不变（实参被净化 or callee 内净化都截断链）；cross=false 逐字节回到 S78 文件内
  语义（A/B 对照）。exe 自动发现 TEMP/rx-rs-target，缺失时清晰报错不静默降级；
  naive=true 走旧全参数模式供对照验收
