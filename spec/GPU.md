# GPU —— GPU 计算支持设计（S114，用户定调「量大的活交给 GPU，IDE 工具也要支持」）

> 目标：把**数据并行**的重活交给 GPU（扫描/统计/熵/批量匹配），同时**不破坏**
> 本仓两条红线：Python 纯 stdlib、Rust `[dependencies]` 恒空。
> 本文给出：运行时选择、实测交叉点、适用/不适用清单、降级纪律、接入面。

## 一、运行时选择（零 pip 依赖）

| 路线 | 依赖 | 结论 |
|---|---|---|
| **OpenCL（选用）** | 系统 `OpenCL.dll` / `libOpenCL.so`（GPU 驱动自带） | ✅ 运行时编译内核（`clBuildProgram`），跨厂商，零 pip 包 |
| CUDA Driver API | `nvcuda.dll` + 需要 nvcc 或预编译 PTX | ❌ 本机无 nvcc；OpenCL 已覆盖同一 GPU |
| wgpu/cust/torch/cupy | 第三方包 | ❌ 违反零依赖红线 |

实现：`tools/gpu.py` —— ctypes 直调 OpenCL（显式 argtypes，避免 64 位句柄截断），
上下文/队列/程序按进程缓存；内核用 OpenCL C 写、运行时编译。

**本机实测环境**：NVIDIA GeForce RTX 4060 Ti（34 CU / 8GB / OpenCL 3.0 CUDA 13.1）。

## 二、实测交叉点（bench/s114_gpu_bench.py，留档 bench/results/s114_gpu.json）

| 内核 | 1MB | 8MB | 64MB | 结论 |
|---|---|---|---|---|
| `byte_hist`（字节直方图 → 熵/打包检测） | GPU **31×** | GPU **34×** | GPU **38×** | ✅ 1MB 起 auto 走 GPU（预热后；结果与 CPU 逐位一致） |
| `literal_scan`（多模式字面量签名） | GPU **0.17×** | GPU **0.18×** | 未测 | ❌ CPU 的 `bytes.find`(memmem) 太强，auto 恒走 CPU |

诚实结论：**GPU 的赢面在"统计/数学"类内核，不在"字面量匹配"**。字面量匹配的
GPU 内核保留（`engine="gpu"` 显式可调，供"海量模式 × 超大语料"的极端场景），
但 `auto` 永不选它——数字在上表。

## 三、适用 / 不适用（如实，防"装了 GPU 什么都快"的错觉）

**适用（数据并行、算术密集）**：
- 字节直方图 / 香农熵（打包、加密、压缩文件识别）
- 批量哈希预筛、异或/掩码枚举（恶意样本常见混淆形态）
- 大规模语料的统计特征（n-gram 计数、相似度矩阵）

**不适用（GPU 帮不上）**：
- **IO 主导**：读盘、遍历、大文件分块读——瓶颈在磁盘
- **IDE 编排类**：编译/测试/LSP 会话/调试器——瓶颈在外部进程与协议
- **字面量匹配**：CPU 的 memmem 已优化到极致（实测见上表）
- **小数据（<1MB）**：传输 + 内核启动开销 > 计算本身

## 四、降级纪律（与 LSP/ast-grep 同款）

- 无运行时 / 无 GPU 设备 / 内核编译失败 → **明确错误**（`gpu_status` 给出原因），
  调用方回落 CPU 路径；**不静默降级、不假装支持**。
- CPU 参考实现（`*_cpu`）与 GPU 内核同文件——既是降级路径，也是 GPU 的 oracle
  （测试逐位比对）。
- `mode="auto"` 按实测交叉点选路；`mode="cpu"/"gpu"` 可强制（排障/对照）。

## 五、接入面（S115 起）

| 面 | 接法 |
|---|---|
| **文件扫描**（扫病毒/扫漏洞的"量大"部分） | 新工具 `file_scan`：SHA-256 哈希匹配（CPU，快）+ **熵/打包检测（GPU）** + 字面量签名（CPU）——**签名/启发式扫描，非杀毒软件**，如实标注 |
| **扫描域**（bug/vuln） | 规则匹配仍是 CPU（AST/正则）；统计类信号（熵、字节分布）走 GPU |
| **IDE 工具** | 编排类（build/test/lsp/debug）**不接 GPU**（瓶颈在外部进程）；批量文本统计（如大仓二进制/打包文件盘点）走 `file_scan` 的 GPU 路径 |
| **遥测** | `gpu_status` 工具：运行时/设备/VRAM/CU 与降级原因 |

## 六、测试与门禁

- GPU 内核 vs CPU oracle 逐位一致（`tests/test_s114_gpu.py`）；
- 无运行时降级路径（monkeypatch 库名 → 明确错误）；
- `pick_mode` 交叉点选路（含强制模式）；
- 门禁沿用四道（pytest 双解释器 / cargo / selftest / S113 八门）。
