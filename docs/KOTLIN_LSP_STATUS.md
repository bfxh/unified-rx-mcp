# Kotlin LSP 状态（2026-08-14）

> 用户点名 Kotlin/Swift LSP——前置安装评估完成，本体受本机网络限制暂挂。

## ✅ 已完成（JDK 就绪）

- **JDK 17（Microsoft OpenJDK 17.0.20 LTS）**：zip 免安装
  - 路径：`%JDK_HOME%`
  - 验证：`.../bin/java -version` → `openjdk version "17.0.20" 2026-07-21 LTS` ✓
- **KLS 源码**：`<ktsrc>/kotlin-language-server-1.3.12`（codeload 下载 1.4MB）
  - gradle wrapper 就绪（`gradlew.bat`）

## ⛔ 阻塞（网络 TLS 受限）

| 步骤 | 失败原因 |
|---|---|
| KLS release zip（GitHub release） | `objects.githubusercontent.com` TLS 握手失败（exit 35） |
| gradle 构建（依赖下载） | `services.gradle.org` 重定向后证书链验证失败 |
| ghproxy 镜像 | 返回 9 字节（无效） |

## 🔜 恢复步骤（网络恢复后）

```bash
# 方式 A：release zip（优先——免构建）
curl -L -o ktserver.zip \
  "https://github.com/fwcd/kotlin-language-server/releases/download/1.3.12/kotlin-language-server-1.3.12.zip"
unzip -q ktserver.zip -d <toolchain>/kotlin-lsp

# 方式 B：源码构建（release 不可用时）
cd <ktsrc>/kotlin-language-server-1.3.12
set JAVA_HOME=%JDK_HOME%
gradlew.bat --no-daemon -x test assemble
# 产物：server/build/libs/kotlin-language-server-all.jar

# 接入 cae LSP_SERVER_CONFIG：
# "kotlin": (java 全路径, ["-jar", "kotlin-language-server-all.jar"])
# "kotlin" 后缀映射：.kt/.kts → kotlin
```

## 验证方式（接入后）

```python
# kt 文件 definition/hover 实机验证（对齐 test_lsp_query_go_real）
# cae test_server.py 加 test_lsp_query_kotlin_real（KLS 缺失跳过）
```
