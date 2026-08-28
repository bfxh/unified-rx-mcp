# python 语言 skill（ide 域）
- 构建：`compileall -q`（语法）；**语法错误走 stdout 不是 stderr**（坑）
- 诊断：pylsp（LSP）；修复轮 pytest FAILED+E 断言解析
- 调试：ide_break settrace 记录器（**line 事件 return None 在 3.11+ 不关帧
  追踪，max_hits 截停必须 sys.settrace(None)**）；traceback 帧解析
- 坑：venv 无 pip（uv 需 --seed）；老仓 conftest 撞新 pytest（按年代钉）；
  compileall -q 的 rc=1 但错误在 stdout
