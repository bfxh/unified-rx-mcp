# rust 语言 skill（ide 域）
- 构建：`cargo check/test/clippy --message-format=short`（诊断缓存键含 action）
- lint：clippy 缺失双路探测（which cargo-clippy + 输出特征）→ 如实报
  rustup component add clippy
- 调试：RUST_BACKTRACE=1 自动；panic 新旧双格式 + 回溯帧解析；
  **断点需 gdb/lldb——缺失如实报错**（轻依赖替代=panic 帧）
- 坑：depth-1 fetch 无 git tag → setuptools_scm 版本 0.1.dev（用
  SETUPTOOLS_SCM_PRETEND_VERSION）；mpl/astropy 类 C 扩展仓需 py3.8 回退
