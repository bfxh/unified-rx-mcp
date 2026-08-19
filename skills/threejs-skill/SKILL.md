---
name: threejs-skill
description: "Three.js WebGL 3D 技能（r150+，ES modules + import map / bundler）：场景/相机/几何体/材质/灯光/纹理/动画/交互/GLB 加载/性能优化。在用户要求网页 3D、WebGL 场景、Three.js 交互（旋转/拖拽/点击拾取）、模型展示（glTF/GLB 加载）、3D 动画循环、粒子/天空盒/阴影、性能优化（draw call/几何合并/LOD）时调用。含反模式清单（每帧新建对象、忘记 dispose、阴影配置错误、antialias 滥用）。Invoke when building 3D web experiences, Three.js scenes, WebGL visualizations, product viewers, or 3D animated websites."
version: "1.0"
runAs: subagent
allowed-tools: read_file, write_file, edit_file, bash
---

# Three.js 技能（WebGL 3D）

## 定位

Three.js 是在浏览器中构建 3D 场景的事实标准库（WebGL/WebGPU 之上）。
本技能覆盖：**场景搭建 → 模型/几何 → 材质/灯光 → 相机控制 → 动画 → 交互 → GLB 资产管线 → 性能**。
产出可直接运行的 HTML/JS（ES modules），并在交付前给性能与正确性自查。

## 何时使用

- 网页 3D 展示（产品、建筑、数据可视化）
- 3D 交互（OrbitControls 旋转/缩放、Raycaster 点击拾取）
- GLB/glTF 模型加载与展示
- 3D 动画（循环旋转、相机漫游、材质动画）
- 粒子系统、天空盒、阴影、后期效果
- 性能优化（合并几何、LOD、实例化）

## 核心执行模式（必须遵守）

### 1. 最小场景骨架（ES modules）

```html
<script type="importmap">
{ "imports": { "three": "https://unpkg.com/three@0.160.0/build/three.module.js" } }
</script>
<script type="module">
import * as THREE from "three";
import { OrbitControls } from "https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js";

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, innerWidth / innerHeight, 0.1, 1000);
camera.position.set(5, 4, 8);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));  // 防高分屏性能浪费
document.body.appendChild(renderer.domElement);

// 灯光（Three.js 无光 = 全黑！至少一个光源）
scene.add(new THREE.AmbientLight(0xffffff, 0.5));
const dir = new THREE.DirectionalLight(0xffffff, 1);
dir.position.set(5, 10, 5);
scene.add(dir);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;      // 惯性——体验关键

function animate() {
  requestAnimationFrame(animate);
  controls.update();                // damping 时必须每帧 update
  renderer.render(scene, camera);
}
animate();
window.addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
</script>
```

### 2. 加载 GLB（Three 管线标准——与 Blender 技能配对）

```js
import { GLTFLoader } from "https://unpkg.com/three@0.160.0/examples/jsm/loaders/GLTFLoader.js";
const loader = new GLTFLoader();
loader.load("model.glb", (gltf) => {
  scene.add(gltf.scene);
  // 动画：gltf.animations → AnimationMixer
  const mixer = new THREE.AnimationMixer(gltf.scene);
  const action = mixer.clipAction(gltf.animations[0]);
  action.play();
}, undefined, (err) => console.error("GLB 加载失败:", err));
```

### 3. 点击拾取（Raycaster）

```js
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
renderer.domElement.addEventListener("click", (e) => {
  pointer.x = (e.clientX / innerWidth) * 2 - 1;
  pointer.y = -(e.clientY / innerHeight) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(scene.children, true);
  if (hits.length) console.log("点中:", hits[0].object.name);
});
```

### 4. 几何与材质（常用）

```js
// 几何：Box/Sphere/Cylinder/Plane/Torus/Klein
new THREE.BoxGeometry(1, 1, 1);
// 材质：MeshStandardMaterial（PBR，配合灯光）
new THREE.MeshStandardMaterial({ color: 0xcc6633, roughness: 0.6, metalness: 0.2 });
// 纹理加载
const tex = new THREE.TextureLoader().load("albedo.png");
tex.colorSpace = THREE.SRGBColorSpace;   // r152+ 必须设色彩空间，否则偏灰
```

## 反模式清单（生成后必须自查）

| 反模式 | 后果 | 修正 |
|---|---|---|
| 每帧 `new THREE.Mesh()` / `new Vector3()` | GC 卡顿、掉帧 | 对象池/复用临时向量（`v.set(...)`） |
| 忘记 `texture.colorSpace = SRGBColorSpace` | 颜色发灰/发暗 | r152+ 必须显式设置 |
| 无灯光但用 StandardMaterial | 全黑（新手最常见） | Ambient + Directional 至少一个 |
| `renderer.setPixelRatio(devicePixelRatio)` 不封顶 | 4K 屏性能崩 | `Math.min(devicePixelRatio, 2)` |
| 模型/纹理/材质不用 `dispose()` | 内存泄漏（切换场景后持续涨） | `geo.dispose(); mat.dispose(); tex.dispose()` |
| 大量独立 Mesh 不合并 | draw call 爆炸（>100 掉帧） | `BufferGeometryUtils.mergeGeometries` / InstancedMesh |
| 阴影全开（shadowMap + castShadow 每个对象） | 大场景卡顿 | 只给关键对象 castShadow，`shadow.mapSize` 适中 |
| 忽略 `controls.update()`（damping 开启时） | 相机不动/抖动 | enableDamping 时必须每帧 update |
| 动画循环里创建闭包/监听器 | 内存增长 | 循环外创建，循环内只更新 |

## 性能检查清单（交付前）

1. draw call 数：`renderer.info.render.calls`——>100 需合并/实例化
2. 三角面数：`renderer.info.render.triangles`——移动端 < 200k
3. 内存：切场景后 `renderer.info.memory.geometries` 不增长（dispose 生效）
4. 帧率：`requestAnimationFrame` 间隔 < 16.7ms（60fps）
5. 首屏：大模型用 `DRACOLoader` 压缩（glTF Draco 解码）
6. 光照贴图烘焙 vs 实时灯光：静态场景优先烘焙（减少实时计算）

## 常用资源

- 核心：`THREE.Scene/Camera/Renderer/Mesh/Material/Texture`（官方文档 threejs.org/docs）
- 控制器：`OrbitControls`（交互标配，`enableDamping` 开惯性）
- 加载器：`GLTFLoader` + `DRACOLoader`（模型）、`TextureLoader`（贴图）
- 辅助：`BufferGeometryUtils`（合并）、`InstancedMesh`（实例化）、`LOD`（多级细节）
- 版本注意：r150+ 用 `outputColorSpace = THREE.SRGBColorSpace`；r152+ 纹理 `colorSpace`；import 用 `three/addons/` 路径（Three.js 官方 CDN 别名）
