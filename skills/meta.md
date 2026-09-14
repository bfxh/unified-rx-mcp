# meta 域（gpu_status / local_run / process）

- **gpu_status**：GPU 遥测（OpenCL 运行时/设备/CU/VRAM 与降级原因）
- local_run：任意命令执行，需 `__authorized: true`；带超时/取消/进度
- process：进程列举/终止
- 边界：**argv 直传不走 shell**（S137 审计实锤修复：此前 shell=True 与契约
  矛盾，已改非 posix shlex 切分 + shell=False，Windows 路径反斜杠保真）；
  命令模板白名单 + `_ALLOWED` 字符集纵深；后台模式有心跳
