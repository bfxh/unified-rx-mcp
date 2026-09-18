# sys 域（sys_topology / sys_threads / sys_steer / sys_devices）

混合架构（大小核）调度观测与引导，S148 立。引擎 `rx-sys.exe`（Rust，零依赖手写
FFI，`rust/src/sysinfo.rs`）。动机：传统引擎默认所有核心一致 → 关键渲染线程被
调度到 E 核，帧率波动。

## 四工具

- **sys_topology**（读）：CPU 厂商/型号（CPUID 品牌串）、**P/E/LP-E 分级**、
  SMT、L3、NUMA、CPU 集清单。判据 = Windows `EfficiencyClass`，**双 API 交叉**：
  `GetLogicalProcessorInformationEx(RelationProcessorCore)` 与
  `GetSystemCpuSetInformation` 各出一份分级，两口径必须一致（本机实测：
  Ultra 7 270K 两份都给 P=8 逻辑核 [0,1,10,11,12,13,22,23]）。非混合平台
  （多数 AMD/旧 Intel）如实报 `uniform`——**不瞎标 P/E**。
- **sys_threads**（读）：某进程每线程 TID/名字（GetThreadDescription）/优先级/
  理想核/CPU 集。注意：EcoQoS 状态**读不回来**（GetThreadInformation 不支持），
  以 steer 的设置返回值为准（工具内已注明）。
- **sys_steer**（**执行·写，需 `__authorized`**）：按档位引导线程调度。
  - `render`（关键线程→P 核）：松开上一档硬掩码 → 限 P 核 CPU 集 → 理想核指 P
    → `AboveNormal` 优先级 → **关 EcoQoS**；
  - `background`（后台→E 核）：同理反向（E 集 + `BelowNormal` + **EcoQoS 开**，
    调度器会导向 E 核并选最省电频率）；
  - `hard=true` 追加硬亲和掩码：**Intel 官方明确劝阻**（硬约束引发迁移/缓存
    抖动、E 核多于 P 核时反噬）——默认走 CPU Set 软定向，硬掩码只在"必须始终
    在 P 核"时用，代价自担；
  - `tids` 缺省 = 该进程全部线程。
- **sys_devices**（读）：显示适配器清单（NVIDIA/AMD/Intel 识别；同一 GPU 的多个
  显示口去重；虚拟显示适配器照实列出）。VRAM/DXGI 细节留待后续（见边界）。

## 实测坑（都已在代码里修掉，防回归）

1. **约束栈顺序**：硬亲和掩码是硬约束，CPU 集/理想核是软定向——不先释放旧硬
   掩码，`SetThreadIdealProcessorEx` 以 INVALID_PARAMETER 拒绝（render↔background
   互切必现）。正确序：**硬掩码 → CPU 集 → 理想核 → 优先级 → EcoQoS**。
2. **`SYSTEM_CPU_SET_INFORMATION` 字段偏移**：`EfficiencyClass` 在载荷第 10 字节
   （不是第 7）；错读会把 CPU 集 Id 当分级（本机曾输出 0..23 全不同）。
3. **`ThreadPowerThrottling` 类号 = 3**（0=MemoryPriority、1=AbsoluteCpuPriority、
   2=DynamicCodePolicy）——写成 2 时 API 拒绝参数、EcoQoS 静默全败。
4. **模块名不得叫 `sys.py`**：与 stdlib `sys` 撞名时 `from . import sys` 会**静默
   跳过**（包属性已存在），工具不注册——本模块因此叫 `sysinfo.py`。

## 边界（如实；与厂商工具链的关系）

- **Thread Director**：硬件+OS 内建，无直接 API。我们能做的是给它更准的输入
  （QoS/优先级/理想核）——这正是 `sys_steer` 在做的事；
- **APO / iBOT**：厂商侧运行时（配置档 / 二进制重排），不接受第三方调用，
  不在工具面内；
- **ITT / VTune / GPA**：需厂商 SDK 与可视化栈——本域只产出**结构化 JSON**
  供任何分析工具消费，不引入 SDK（零依赖红线）。ETW 验证通道
  （`Microsoft-Windows-Kernel-Scheduler` / `-Processor-Power`）留作后续；
- **权限**：跨进程改线程需要相应访问权（管理员对高权限进程）；`sys_threads`
  对系统进程（如 PID 4）可能枚举为空——如实返回，不假装。

## 验证姿势

```
sys_topology                     → 看 classes（P/E 核数与逻辑核分布）
sys_threads(pid)                 → 看 priority / ideal_number / cpu_sets
sys_steer(pid, "background")     → 期望 priority=-1、cpu_sets=E 集、ideal 落 E 核
sys_steer(pid, "render")         → 期望 priority=1、cpu_sets=P 集、ideal 落 P 核
```
