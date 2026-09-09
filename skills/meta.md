# meta 域（gpu_status（GPU 遥测：OpenCL 运行时/设备/CU/VRAM 与降级原因）
- local_run / process）
- local_run：任意命令执行，需 `__authorized: true`；带超时/取消/进度
- process：进程列举/终止
- 边界：不走 shell（argv 直传）；后台模式有心跳
