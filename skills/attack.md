# attack 域（input_fuzz/path_probe/big_input/auth_gate_sweep）
- 自写对抗用例生成器：路径逃逸/注入串/大输入三类
- 里子：不是 hypothesis 属性测试；S29 的 5 洞是人工对抗测试抓的
- auth_gate_sweep（S77，VULN-HUNTING P0-a）：全工具授权门自审——挂门必拒未授权
  （端到端 registry.call 空参验证，授权检查先于 handler 零副作用）/ schema 必声明
  __authorized（S72b 契约）/ manifest 高权限段一致；单工具混合读写用
  `manual_gate=True` 显式声明（ide_lsp 范例），"收 __authorized 无任何声明"即假门
  → ok:False。S75 人眼盘点法固化成工具，新工具一注册就自动被查
