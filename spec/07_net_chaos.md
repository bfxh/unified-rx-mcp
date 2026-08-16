<!-- SPDX-FileCopyrightText: 2026 bfxh -->
<!-- SPDX-License-Identifier: MIT -->
# §7 — net_chaos 契约（弱网模拟）

> 覆盖工具：`net_chaos`（`net_core.py` 桥接 → `rx-net` Rust 混沌代理）。
> 用途：本地 TCP 代理注入 延迟/丢包/乱序/带宽限速——测试客户端网络鲁棒性。

## 7.1 定位（MUST）

1. `net_chaos` **MUST** 只做弱网模拟：在 `listen` 端口接收客户端连接，
   转发到 `target` 服务，转发途中按配置注入混沌。
2. 混沌参数（`delay`/`loss`/`reorder`/`bandwidth`）**MUST** 只影响转发中的数据，
   **MUST NOT** 修改被转发内容本身（数据一致性是核心不变量）。

## 7.2 参数边界（MUST）

| 参数 | 约束 | 越界处理 |
|---|---|---|
| `action` | `start`/`stop`/`status`/`sanity`（缺省 = `status`） | 其他值 MUST 报 `参数非法` |
| `listen` | `host:port`；start 缺省/端口 0 → 自动分配空闲端口 | — |
| `target` | `host:port`（start 缺省 `127.0.0.1:80`） | — |
| `delay` | ≥0 毫秒（默认 0） | 负值钳制为 0 |
| `loss` / `reorder` | 0-100 百分比（默认 0） | 越界钳制到 [0,100] |
| `bandwidth` | ≥0 KB/s（默认 0=不限） | 负值钳制为 0 |

## 7.3 生命周期（MUST）

1. `start` **MUST** 返回 `{ok, listen, target, cfg, pid}`；端口自动分配时
   **MUST** 返回实际端口（调用方用返回值连接，不得假设）。
2. 重复 `start` 同一 `listen` **MUST** 幂等：返回 `already_running: true`，
   **MUST NOT** 起第二个进程。
3. `stop`（缺省 = 全部停止；指定 `listen` = 只停该代理）**MUST** 返回 `{ok, stopped[]}`；
   对已退出的代理 **MUST** 幂等（不报错）。
4. `status` **MUST** 返回运行中代理清单 `{ok, proxies[], count}`，
   并清理已退出进程的僵尸记录。
5. 代理停止 **MUST** 及时退出（stdin `stop` → 进程 exit ≤3s），
   **MUST NOT** 依赖 kill 超时强杀（accept 阻塞不置 flag 的坑已修）。

## 7.4 失败语义（MUST）

1. `rx-net` 未编译/exe 缺失/`RX_NET=0` → **MUST** 返回
   `{ok: false, error: "rx-net 不可用（未编译或 RX_NET=0）"}`，**MUST NOT** 假成功。
2. `sanity` 自检（echo 往返）**MUST** 返回 `{ok, result}`，result 含毫秒与字节数；
   失败 **MUST** 返回 `{ok: false, error}`。
3. 参数类型非法（非数字）→ **MUST** 返回 `{ok: false, error: "参数非法: ..."}`。

## 7.5 数据一致性不变量（MUST）

1. 无混沌（全部默认 0）时，经代理往返数据 **MUST** 与直连一致（字节级）。
2. 有延迟注入时，往返耗时 **MUST** ≥ 配置延迟（客户端→目标 与 目标→客户端
   两个方向各自注入，往返 ≈ 2×delay）。
3. 100% 丢包时，客户端 **MUST NOT** 收到回显，且连接不得挂死
   （丢包路径 MUST 快速返回，不阻塞调用方）。

## 7.6 探针与测试

- pytest：`test_net_chaos.py`（9 例：参数钳制/自动端口/延迟注入/100% 丢包/
  重复启动幂等/双代理并存/stop 全部/sanity×2）——`python -m pytest test_net_chaos.py -q`
- Rust 单测：`rx-net` crate（5 例：drop/reorder/bandwidth 纯函数 + proxy 往返 + 延迟注入）
- 契约断言（MUST 级）：§7.2 边界、§7.3 生命周期、§7.5 不变量全部有 pytest 覆盖
