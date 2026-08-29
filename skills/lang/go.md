# go 语言 skill（ide 域）
- 构建：`go build ./...`；错误格式 `file:line:col: msg` **无 level 词**
  （专用解析器，非 gcc 正则）
- 调试：dlv trace 函数级（本机 delve）；**行级断点需交互式 dlv——如实降级**
- 坑：错误消息无 error/warning 字样；go.mod 存在但 go 不在 PATH → 如实报
