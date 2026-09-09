# GPU —— GPU 计算支持设计（S114，用户定调「量大的活交给 GPU，IDE 工具也要支持」）

> 目标：把**数据并行**的重活交给 GPU（扫描/统计/熵/批量匹配），同时**不破坏**
> 本仓两条红线：Python 纯 stdlib、Rust `[dependencies]` 恒空。
> 本文给出：运行时选择、实测交叉点、适用/不适用清单、降级纪律、接入面。
>
> **S119 口径修正（重要）**：本文所有倍数都是**对纯 Python 基线**。Rust 侧原生化后
> 用原生 Rust（纯 std，无任何 crate）复测，**现有每一个 GPU 内核都被 Rust 打败**
> （见 §二·三）。故从 S119 起：流式/哈希类热路径走 **Rust**，GPU 降为可选引擎
> （`engine=gpu` 显式可调），新内核必须过"vs Rust 原生"这一关才允许进 `auto`。

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

> 下表倍数 = **对纯 Python 基线**（S114-S117 口径）。S119 起与原生 Rust 的对照见 §二·三。

| 内核 | 1MB | 4MB | 8MB | 64MB | 结论 |
|---|---|---|---|---|---|
| `byte_hist`（直方图 → 熵/打包检测） | GPU **33×** | — | GPU **36×** | GPU **38×** | ✅ ≥1MB auto 走 GPU（预热后；结果与 CPU 逐位一致）。**不迁 Rust**（S120 实测：逐文件调用时进程启动 ~15ms ≥ 计算本身，16MB 时 Rust 1.4ms+15ms ≈ GPU 16ms；200×4KB 整目录仅 46ms） |
| `xor_crib_scan`（单字节异或层枚举） | GPU **0.6×** | GPU **3.8×** | — | GPU **3.6×** | **S120 已迁 Rust**：`file_scan` 的异或枚举 ≥256KB 走 `rx-scan xor`（16MB 485ms vs GPU 1279ms vs CPU 3763ms）；GPU 保留 128KB-256KB 带（无启动开销，实测最优）与回落路径 |
| `literal_scan`（多模式字面量签名） | GPU **0.17×** | — | GPU **0.18×** | — | ❌ CPU 的 `bytes.find`(memmem) 太强，auto 恒走 CPU |

**已退役**：`dot_matrix`（S116 交付、无调用方；S119 实测朴素内核 6.7 GFLOP/s，
比原生 Rust 朴素实现慢 22×——1024³ GPU 317.9ms vs Rust 16 线程 14.1ms）。
按"不留死代码"删除；若将来需要相似度矩阵，用 Rust 实现并过 §二·三 的门。

### n-gram / 近似重复（S117）

| 内核 | 8KB | 64KB | 256KB | 1MB | 4MB | 16MB | 64MB | 结论 |
|---|---|---|---|---|---|---|---|---|
| `ngram_bottomk`（bottom-k MinHash，两遍选择） | **1.7×** | **18×** | **64×** | **147×** | **199×** | **551×** | **444×** | ✅ ≥8KB auto 走 GPU（CPU 参考为纯 Python；4KB 时 0.9×，GPU 固定开销 ~4ms） |
| `ngram_hashes`（全量逐位置哈希，仅回退路径） | — | — | — | 2.9× | 2.8× | 2.9× | — | ⚠️ 输出 4 字节/字节，受回传带宽限制——只作回退与 oracle |

16MB / 64MB 为 min-of-3 单独实测（基准脚本扫到 4MB 以控时长）。

**两遍选择**（`ngram_bottomk_gpu`）：第一遍按哈希高 12 位做 4096 桶直方图，取累计 ≥ 4k
的最小桶边界为阈值；第二遍只发射 ≤ 阈值的哈希（原子计数 + 容量上限），期望 ~4k 个。
回传量从 O(n) 降到 O(k)，且**精确**——所有小于阈值的值都被发射、发射数 ≥ k，故 bottom-k
与全量口径逐位一致；单桶超上限的极端分布（如全零文件）自动回退全量路径。

**负结果入册（实测不接，防"装了 GPU 什么都快"）**：
1. **批量哈希**（FNV-1a 64 每块）GPU vs CPU `hashlib.blake2b` 仅 **0.4-1.2×**——CPU 的 C
   实现更快，故**不接**（内核已删，不留死代码）。
2. **直方图余弦相似度**对高熵数据**无区分力**（随机文件之间也 0.98）→ 近重复检测改用
   经典 bottom-k MinHash（实测近重复 1.0 / 随机 0.0）。
3. **全量哈希输出**只 ~2.8×（回传带宽受限）→ 才有了上面的两遍选择。
4. `ngram_hist`（65536 桶全直方图）API 因**无调用方退役**，数字留档：GPU vs 纯 Python
   **117-356×**、vs `zlib.crc32` 窗口 **31-97×**（≥4MB）；同一 FNV-1a 32 口径现由
   bottom-k 第一遍的 4096 桶直方图承担。

**两个实测坑（入册）**：
1. **内核计数封顶后 early-return 会跳过写回**——首版 `if (++count > 255) return;` 让 GPU 在
   高熵数据上恒报 0、与 CPU 不一致（基准当场抓出）→ 改饱和计数并恒写回。
2. **二进制 crib 不能走字符串**——`MZ(0x90)(0x00)...` 经 UTF-8 重编码字节即变，真密钥永远找不到
   → `file_scan` 支持 `xor_crib="hex:4d5a900003000000"` 形式；且 **crib 需 ≥6 字节**：
   高熵数据上短 crib 每个密钥都会随机命中（N/256^L 次），噪声淹没真密钥（实测：3MB
   随机数据 + 2 字节 crib → 每密钥约 48 次假命中）。

诚实结论：**GPU 的赢面在"统计/数学"类内核，不在"字面量匹配"**。字面量匹配的
GPU 内核保留（`engine="gpu"` 显式可调，供"海量模式 × 超大语料"的极端场景），
但 `auto` 永不选它——数字在上表。

### 二·三、vs 原生 Rust（S119 关键对照，16MB / min-of-3 / 同机）

纯 std Rust（`std::thread`，`[dependencies]` 恒空，opt-level=2 与仓库 release 一致）
对同口径内核复测——**现有 GPU 内核全部落后**：

| 内核（16MB） | Rust 单线程 | Rust 16 线程 | GPU | 结论 |
|---|---|---|---|---|
| 字节直方图（流式） | 4.4ms | **1.4ms** | 15.4ms | Rust 快 **11×** |
| n-gram bottom-k | 27.8ms | **5.4ms** | 32.5ms | Rust 快 **6×** |
| 异或密钥枚举（256 键） | 3084ms | **365ms** | 1279ms | Rust 快 **3.5×** |
| 矩阵乘 1024³（已退役内核） | 96.4ms | **14.1ms** | 317.9ms | Rust 快 **22×** |
| 300×20KB 批量指纹（含 IO/进程） | ~50ms | 13ms | 651ms | Rust 快 **10-13×** |

**为什么 GPU 输（不是 GPU 不行，是内核朴素 + 数据在 CPU 侧）**：
1. 数据本来就在 CPU（文件由 Python 读）→ GPU 要付来回传输；
2. 内核未优化：matmul 只有 6.7 GFLOP/s（4060 Ti 理论 20+ TFLOP/s，无 tiling/局部内存/
   向量化）；**异或枚举只开 256 个 work item**（`gsz = len(keys)`，把并行度锁死在密钥
   数上，而 GPU 有 4 千多个执行槽）；
3. 流式内核（直方图/哈希）是内存带宽活，CPU 直接吃缓存就够。

**门（S119 起）**：任何新 GPU 内核必须给出"vs 原生 Rust"的实测对照，赢了才允许进
`auto`；只赢纯 Python 不算赢。当前已接路径：`near_dupes` 指纹走 `rx-scan sketch`
（Rust 批量，`engine=auto` 优先），`file_scan` 异或枚举走 `rx-scan xor`（≥256KB，
`xor_engine=auto` 优先），GPU 保留 `engine=gpu` / `xor_engine=gpu` 显式可选与回落。

**S120 三档选路实测（单文件异或枚举，min-of-3）**：64KB CPU 14.5ms 最优；
128KB GPU 12.9ms < rust 19.2ms；256KB GPU 22.9ms ≈ rust 23.7ms（分界）；
512KB rust 32.2ms < GPU 43.5ms；16MB rust 485ms < GPU 1279ms < CPU 3763ms。
→ `xor_engine=auto`：≥256KB rust → ≥128KB gpu → cpu。

## 三、适用 / 不适用（如实，防"装了 GPU 什么都快"的错觉）

**适用（数据并行、算术密集）**：
- 字节直方图 / 香农熵（打包、加密、压缩文件识别）
- 异或/掩码枚举（恶意样本常见混淆形态；**批量哈希实测 CPU 更快**，见上节负结果）
- 大规模语料的统计特征（n-gram 指纹 / bottom-k MinHash、相似度矩阵）

**不适用（GPU 帮不上）**：
- **IO 主导**：读盘、遍历、大文件分块读——瓶颈在磁盘
- **IDE 编排类**：编译/测试/LSP 会话/调试器——瓶颈在外部进程与协议
- **字面量匹配**：CPU 的 memmem 已优化到极致（实测见上表）
- **小数据（<1MB）**：传输 + 内核启动开销 > 计算本身；**每次调用固定开销 ~3ms**
  （缓冲创建/写入/两次启动）——300 个 20KB 的批量指纹场景 GPU 651ms，Rust 一次
  进程调用约 15ms（§二·三）
- **凡是原生 Rust 更快的**：当前实测覆盖的全部内核（见 §二·三）——先 Rust，再谈 GPU

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
| **近似重复**（S117-S119） | 新工具 `near_dupes`：目录遍历（CPU）+ bottom-k 指纹（**Rust 批量 `rx-scan sketch` 优先**，GPU 两遍选择 / CPU 参考为回落，三档如实上报）+ **精确候选剪枝**（倒排索引 + Jaccard 下界，纯 CPU 算法优化）+ Jaccard 聚类（CPU）——重复代码分堆、样本同族归并 |
| **IDE 工具** | 编排类（build/test/lsp/debug）**不接 GPU**（瓶颈在外部进程）；批量文本统计（如大仓二进制/打包文件盘点）走 `file_scan` 的 GPU 路径 |
| **遥测** | `gpu_status` 工具：运行时/设备/VRAM/CU 与降级原因 |

## 六、测试与门禁

- GPU 内核 vs CPU oracle 逐位一致（`tests/test_s114_gpu.py`、`tests/test_s116_gpu_kernels.py`、
  `tests/test_s117_neardupes.py`——含独立实现 oracle、溢出回退、无 GPU 降级）；
- Rust sketch vs Python oracle 逐位一致 + 三档引擎回落上报（`tests/test_s119_rust_sketch.py`）；
- 无运行时降级路径（monkeypatch 库名 → 明确错误）；
- `pick_mode` 交叉点选路（含强制模式）；
- 门禁沿用四道（pytest 双解释器 / cargo / selftest / S113 八门）。
