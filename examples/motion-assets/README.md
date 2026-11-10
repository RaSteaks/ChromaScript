# 本地 glTF / GLB 资源

本例支持 glTF 2.0 JSON 和 GLB 2。模型目录内的 `.bin` 和 PNG/JPEG/WebP 贴图会在导入时内嵌；渲染不再读取原文件，也不会访问网络。无需添加 npm 依赖。

Blender 导出的模型请先看 [Blender 适配说明](blender.md)，包含导出设置、骨骼／形态键实例、动画时间和实际验证范围。

1. 用 `get_motion_scene` 读取作品及其最新 `revision_id`。
2. 通过 HTTP、MCP 或 CLI 调用 `import_motion_gltf`，将模型保存到新修订。`name` 是源码引用的逻辑名，支持 1–80 位 ASCII 字母、数字、下划线和连字符；同名导入会替换该资源，旧修订保留原内容。

```sh
.venv/bin/chromascript call import_motion_gltf --arguments '{"scene_id":"实际作品ID","expected_revision_id":"最新修订ID","name":"horse","path":"/absolute/path/horse.glb"}'
```

3. 使用返回的新修订号继续保存源码，并完整保留返回的 `assets` 和 `preserved_layers`。SDR 可使用 [sdr.tsx](sdr.tsx)，浮点源码可使用 [float.ts](float.ts)。这些例子假定资源名为 `horse`；请按模型实际尺寸调整相机、位置与缩放。
4. 使用原有预览和导出流程。编辑器保存会保留导入的模型。交付目录包含可独立读取的 `assets/horse.gltf`；`animation.json` 同时包含完整源码、资源与时间规格。

`getGLTF(name)` 返回预加载的 Three.js GLTF 结果，包含 `scene`、`animations`、`cameras`。SDR 预览和正式合成会等待全部资源解码后再挂载组件，贴图失败时显示错误。浮点工厂使用 `await loadGLTF(name)`，首帧等待工厂完成。结果按名称缓存；同一个 `Object3D` 不能同时挂在两个父节点上。不要在组件每次渲染时累加位置或动画时间；`AnimationMixer.setTime(frame / fps)` 可正确支持回跳与时间采样。

多镜头、多角色请用 `createGLTFInstance(name)` 创建独立实例：骨骼、形态键权重、材质和动画片段独立，几何与贴图只读共享；实例卸载时调用 `dispose()`，不会清理其他实例共用的几何／贴图。使用 `get_motion_asset_info(scene_id, name, revision_id?)` 查询节点、骨骼、形态键与动画名称及解码后的关键帧范围，不必读取大块内嵌数据。

资源每个源码图最多 16 个，所有图层的内嵌 JSON 合计最多 64 MiB（包含 Base64 膨胀）。本地依赖必须位于选定模型的目录内，符号链接也不能越界；远程 URL、绝对依赖路径和缺失文件会报错。Draco、Meshopt 和 KTX2/Basis 暂不支持，请导出未压缩网格及 PNG/JPEG/WebP 贴图。

glTF 的纹理、材质因子与顶点颜色有确定的 Rec.709 语义，因此目前支持 React SDR 或 `source_color_space="Linear Rec.709 (sRGB)"` 的浮点源码。不要只修改颜色空间字段把模型重标记为 ACEScg/BT.2020。若需要在宽色域项目中继续创作，可以先在 Rec.709 浮点源码中加载模型，再调用 `upgrade_motion_scene`，将原源码和模型一起保留为独立背景图层；图层之间不共享深度。Alpha 沿用原有线性覆盖率管线，不参与 OCIO RGB 变换。

## English

Import `.gltf` or `.glb` through `import_motion_gltf` with the current scene/revision IDs, a logical asset name and an absolute local path. Buffers and PNG/JPEG/WebP textures inside the model folder are embedded into a new immutable revision. Preserve `assets` and `preserved_layers` when saving source. Reimporting the same name replaces only that asset in the new revision. Existing preview/render operations use the frozen data; deliveries include standalone `assets/<name>.gltf` files and the complete `animation.json`.

Use `getGLTF(name)` in SDR after automatic preloading, or `await loadGLTF(name)` in a float factory. Results are cached; do not parent the same scene twice. Drive animation with absolute `AnimationMixer.setTime()` calls so backward seeks and temporal sampling remain deterministic. Match the camera and scale to your model.

For separate shots/characters, use `createGLTFInstance(name)` after loading. Bones, morph weights, materials and animation clips are independent; geometry and textures are shared read-only. Call the instance's `dispose()` on unmount. `get_motion_asset_info(scene_id,name,revision_id?)` exposes names, rig/morph metadata and decoded keyframe ranges without embedded binary payloads. See the [Blender guide](blender.md) for export settings and the measured compatibility scope.

Limits: 16 assets per source graph, 64 MiB of embedded JSON across all retained layers. Network/absolute dependency URLs, dependencies escaping the model folder (including symlinks), missing files, Draco, Meshopt and KTX2/Basis are rejected. Supported source spaces are React SDR and linear Rec.709 float. A color-preserving upgrade retains the original model in a separate Rec.709 background layer; it does not share depth with new foreground content. Texture/vertex color primaries must never be relabeled as a wider gamut; alpha remains independent of RGB color processing.
