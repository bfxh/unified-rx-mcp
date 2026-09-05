# game 域（game_check/blender_verify）
- Blender 资产/场景校验；依赖本机 Blender 安装（外部资产用例 skipif 守卫）
- **契约变化（S88）**：game_check 的 path 先过沙盒钳制——越界返回
  `{"error": "路径越界（沙盒外）：…"}`（S73 纪律补全；blender_verify 本就有门）
