# Blender → ChromaScript

本通道读取 Blender 导出的 **glTF 2.0 `.glb` 或 `.gltf`**。它保留导出的 PBR 材质、贴图、节点变换、骨骼、形态键与动画，不读取 `.blend` 工程，也不会运行 Blender 的节点图或约束系统。

## 导出设置

在 Blender 的 **File → Export → glTF 2.0** 中选择：

| 设置 | 建议与约束 |
| --- | --- |
| Format | 优先 GLB；分离式 glTF 的 `.bin`、贴图须放在模型目录内，保持相对路径。中文及带空格的贴图文件名可用。 |
| Transform | 保留 `+Y Up`。导出器已经把 Blender Z-up 转成 glTF Y-up；ChromaScript 不再追加旋转，也不自动改模型尺寸。 |
| Geometry | 导出 UV、Normals；有法线贴图时可导出 Tangents。保留原有父子层级及缩放。 |
| Materials | 使用可导出的 Principled BSDF/PBR 图像贴图。程序纹理和不受支持的节点效果须先烘焙。Base Color/Emissive 是颜色贴图，法线、粗糙度、金属度等是数据贴图，不要手动统一改成 sRGB。 |
| Skinning | 导出骨骼与权重。优先最多 4 个骨骼影响；超过 4 个影响不在当前验证范围内。 |
| Shape Keys | 开启 Shape Keys 和对应动画。Blender 3.6 的 Apply Modifiers 导出选项会阻止形态键导出；有形态键时保持关闭，必要的拓扑修改先在副本中处理。 |
| Animation | 导出所需 Actions/NLA；约束、驱动或非关键帧运动先采样／烘焙。使用明确的动画名称。不同版本的 Actions/NLA 合并规则可能不同，以导出文件实际片段为准。 |
| Compression | 关闭 Draco。Meshopt 与 KTX2/Basis 也暂不支持；使用普通网格和 PNG/JPEG/WebP。 |

Blender 的 AgX、Filmic、视口 HDRI 和合成节点不会自动成为 glTF 材质。ChromaScript 使用自己的灯光、相机与显示管线，不能把“成功加载”当作与 Blender 视口逐像素一致的认证。导出器支持范围可查 [Blender glTF 手册](https://docs.blender.org/manual/en/4.5/addons/import_export/scene_gltf2.html)。

本机 Blender 3.6.10 的导出器在拆分 ORM 通道时识别的是 **Separate Color（RGB 模式）**，不是旧的 Separate RGB。使用 Green → Roughness、Blue → Metallic，并把原图设为 Non-Color。本次实际回读了输出贴图：粗糙度 0.42、金属度 0.1 分别存为 G=107、B=26；旧节点在这个版本中会错误取红通道，造成材质外观变化。

## 导入、查看实际节点与动画

先读取当前修订，再调用原有导入接口：

```sh
.venv/bin/chromascript call import_motion_gltf --arguments '{"scene_id":"作品ID","expected_revision_id":"最新修订ID","name":"horse","path":"/absolute/path/horse.glb"}'
.venv/bin/chromascript call get_motion_asset_info --arguments '{"scene_id":"作品ID","name":"horse"}'
```

HTTP、MCP 和 CLI 的参数相同。MCP 使用工具的 `request` 包装。`get_motion_asset_info` 是只读操作，可传 `revision_id` 查询旧版本，返回：

- 导出器标识、资源哈希与大小；
- 节点名称、父子关系、变换，以及材质、骨骼和形态键名称；
- 动画名称和目标通道；
- 从实际 FLOAT SCALAR 时间缓冲区解码的 `keyframe_range`：`start_seconds`、`end_seconds`、`span_seconds`。稀疏或隐式时间 accessor 不会被猜测，范围返回 `null` 并附说明。

这些是结构信息与关键帧时间，不包含贴图二进制，不声称提供运行时包围盒或视觉认证。模型加载后的实际动画长度可读取 `AnimationClip.duration`。

## 多镜头与独立角色

`loadGLTF`／`getGLTF` 返回缓存模板。每个镜头或角色应独立实例化，避免一个角色的骨骼、材质修改或父节点影响另一个角色：

```tsx
import { useEffect, useMemo } from "react";
import { createGLTFInstance } from "chromascript:assets";

function Horse() {
  // SDR 在挂载组件前已完成加载。每个 Horse 实例拥有独立骨骼与材质。
  const model = useMemo(() => createGLTFInstance("horse"), []);
  useEffect(() => () => model.dispose(), [model]);
  return <primitive object={model.scene} />;
}
```

完整的动画和清理写法见 [SDR 示例](sdr.tsx)；浮点工厂先 `await loadGLTF("horse")`，再调用 `createGLTFInstance("horse")`，见 [浮点示例](float.ts)。实例的 `scene`、骨骼、形态键权重、材质与 clips 独立；几何与贴图只读共享。卸载时停止 mixer、解除其 root 缓存，再调用实例 `dispose()`；不要释放共享几何／贴图。需要移动或缩放整个角色时，在外面加 Group，保留模型内部绑定层级。

动画按秒求值，不按调用次数累加。使用项目的 `frame / fps`，不要用 Blender 的帧号替换项目帧率。本次真实导出样本的首个关键帧为 `1/24` 秒，峰值在 `13/24` 秒；导入不会偷偷将它移到 0。若分镜要求片段从零开始，可明确采用 `time + start_seconds`。默认循环的周期是 `AnimationClip.duration`，包含初始留白；如果需要只循环关键帧区间，应显式按 `span_seconds` 取模并加上 `start_seconds`。使用 LoopOnce／clampWhenFinished 时，回跳前 `action.reset().play()`，再 `mixer.setTime(time)`，防止上次播放结束留下 paused 状态。

导入的颜色仍要求 React SDR 或 `Linear Rec.709 (sRGB)` 浮点源码。升级到宽色域时可保留原模型为独立 Rec.709 背景图层，不能仅修改标签来获得正确的宽色域纹理。

## 实际验证范围

真实生成器：**Blender 3.6.10 / Khronos glTF Blender I/O v3.6.28**。固定样本包含中文贴图路径、Base Color + Alpha、Normal、Metallic/Roughness、Clearcoat、双骨骼蒙皮、形态键 `Squash`、NLA 合并动画 `BendAndSquash`、父节点旋转／缩放、灯光和相机。可重复生成脚本和版本记录位于 `tests/fixtures/motion-assets/blender/`。

测试使用离线导入和 headless WebKit，覆盖浮点路径与实际 Remotion 导出编译包；不启动 Chrome，不加载个人 Blender 文件。未把该结果外推为所有 Blender 版本、全部材质节点、全部扩展或完整视频编码的认证。

## English

Export glTF 2.0 GLB (preferred) or separate glTF with local dependencies inside the model folder. Keep +Y Up enabled, include UVs/normals, skinning and morph animation, and disable Draco. Bake unsupported procedural materials and constraints. Do not apply another Z-up rotation or silently normalize exported transforms. The checked Blender 3.6 exporter disables shape-key export when Apply Modifiers is enabled. Blender viewport transforms and compositing are not transferred as glTF materials.

Import with `import_motion_gltf`, then inspect `get_motion_asset_info` for exporter, names, rig/morph structure and decoded keyframe ranges. The latter reads actual FLOAT SCALAR input buffers, not declared min/max; sparse/implicit inputs report unavailable timing. Use the project's rational frame rate and the exported time offsets explicitly. The tested clip starts at 1/24 s; loading never changes that offset or the project timebase.

Use `createGLTFInstance` for each shot/character. It rebinds cloned skinned meshes to cloned bones, separates morph weights, materials and clips, and keeps geometry/textures shared read-only. Cleanup instance materials/skeletons with `dispose()` after stopping/uncaching its mixer. Preserve model hierarchies; place them with an outer Group. Reset a completed LoopOnce action before backward seeks. Color support remains SDR or linear Rec.709 float, with color-preserving background layers available for wider-gamut projects.

Measured with Blender 3.6.10 / exporter 3.6.28 and both GLB/separate glTF: PBR maps, alpha, clearcoat, a two-bone skin, morph targets, named animation, transforms, and independent animated instances. The fixtures and provenance are reproducible. Other Blender versions, arbitrary shader graphs/extensions and full Chrome video encoding are not certified by these checks.

The tested exporter follows Separate Color (RGB mode), not the deprecated Separate RGB node, when packing ORM channels. Use Green → Roughness and Blue → Metallic with a Non-Color source image. Actual output pixels confirmed G=107/B=26 for roughness .42/metalness .1; the old node selected the red channel in this exporter version.
