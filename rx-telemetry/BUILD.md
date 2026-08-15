# rx-telemetry 构建说明

> 注意：仓库路径 `D:\开发` 含中文，msys2 GNU 链接器在默认 target 目录下
> 会因中文路径找不到 .o 文件（rustc 1.95 gnu 工具链已知问题）。**必须**
> 用 `CARGO_TARGET_DIR` 指到英文路径构建，产物拷回约定位置。

## 构建（release）

```bash
export CARGO_TARGET_DIR="D:/rj/.rx-target"   # 任意英文路径
cd rx-telemetry
cargo test                                   # 10 个单测（环形缓冲/落盘/轮转/聚合/tail/坏行）
cargo clippy --all-targets                   # 0 警告
cargo build --release
cp "$CARGO_TARGET_DIR/release/rx-telemetry.exe" target/release/
```

## server.py 查找约定（对齐 rx-core）

```
rx-telemetry/target/release/rx-telemetry.exe
rx-telemetry/target/debug/rx-telemetry.exe
```

## 数据文件

- 默认落盘：`~/.unified-rx/telemetry.jsonl`（`UNIFIED_RX_STATE_DIR` 可覆盖）
- 轮转：超过 100MB 自动轮转 `telemetry.jsonl → telemetry.1.jsonl → .2.jsonl`（保留 3 份）
- 内存环形缓冲：10_000 条；缓冲满 100 条批量落盘

## CLI

```bash
rx-telemetry serve          # 常驻行协议（server.py/daemon.py 经 Popen 调用）
rx-telemetry agg [path]     # 流式聚合（GB 级不整载内存）
rx-telemetry tail [path] -n 20
rx-telemetry status [path]
```
