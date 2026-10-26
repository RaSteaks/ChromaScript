# ChromaScript

**用代码创作画面，在本机完成预览、色彩管理与交付。**

ACES 2.0 程序化视频工作台 · 网页 / MCP / CLI · 本地渲染

[快速开始](#快速开始) · [创作流程](#创作流程) · [预览与性能](#预览与性能) · [色彩与交付](#色彩与交付) · [音频](#项目音频) · [MCP 接入](#mcp-接入) · [常见问题](#常见问题)

| 创作方式 | 适合内容 | 输出路径 |
| --- | --- | --- |
| **WGSL** | 程序图形、色彩实验、参数化动画 | 线性 ACEScg → 图像序列 / 视频 |
| **Three.js 浮点** | HDR 图形、光效、多章节动画 | RGBA32F → ACEScg EXR / SDR、HDR 视频 |
| **React SDR** | 文字、DOM、SVG、ThreeCanvas | sRGB 画面 → SDR 视频 |

共享源码编辑、逐帧时间线、大屏预览、项目音轨和后台任务。所有产品操作均有等效 MCP 工具与 CLI 调用；参考 [场景示例](examples/README.md) 与 [多文件动画](examples/motion-chapters/README.md)。

## 快速开始

需要 macOS、Python 3.12、Node.js 22.12+（含 npm），以及包含 `libx264`、`libx265`、`prores_ks`、`dnxhd` 编码器和 `zscale` 滤镜的 FFmpeg。确保 `ffmpeg` 和 `ffprobe` 可在终端调用。

动画代码的本地导出还需要已安装的 Google Chrome 或 Chromium；自定义路径可通过 `CHROMASCRIPT_CHROME` 指定。运行时不会下载浏览器或访问云端渲染服务。

下载仓库后，在终端执行：

```sh
# 将占位路径替换为本机仓库的实际路径。
cd /absolute/path/ChromaScript
./install.sh
./start.sh
```

安装脚本会检查系统依赖、创建或复用 Python 3.12 虚拟环境、按锁文件安装依赖并构建网页；首次安装需要联网下载依赖。项目更新后重新运行 `./install.sh`。脚本不会自动安装系统依赖。

日常只需运行 `./start.sh`，打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，创建项目并新建空白场景（示例模板可选）。启动不联网安装依赖，数据默认保存在仓库下的 `workspace`，按 `Ctrl+C` 停止服务。

## 创作流程

1. **选择作品** — 创建项目，选择已有作品或新建 WGSL、Three.js、React 作品。示例模板可选。
2. **编辑与预览** — 修改源码、参数和画面规格，直接预览草稿；播放、逐帧定位或使用“大屏查看”。
3. **设置交付** — 保存作品，选择格式、色彩、采样与导出目录。浮点动画默认交付 32-bit ACEScg EXR 母版、2×2 空间采样，也可选择仅视频。
4. **任务与成果** — 查看进度、下载文件、打开导出文件夹，或恢复满足条件的中断任务。

切换步骤保留草稿；外部更新与本地修改冲突时保留编辑内容。项目音轨独立保存，交付冻结提交时的源码、音轨、色彩配置与有理数帧率。

## 预览与性能

| 路径 | 行为 |
| --- | --- |
| **原生 WGSL 渲染** | 最多四个分块批量提交和读回 GPU；传输缓冲上限 16 MiB，保持原有 float32、Alpha 和采样算法。 |
| **实时 WGSL 预览** | 最高 1280×720，竖屏 720×1280；单次采样。按最高 24 次/秒调度，耗时计入刷新间隔，同一时刻只有一个请求；慢帧会跳过显示位置。 |
| **动画交互** | 播放时复用源码序列化结果和消息监听；返回未改动的动画直接复用已挂载预览。隐藏视图暂停播放，显式重新编译仍可重试。 |
| **HDR 视频预览** | 在兼容设备上使用项目原始帧率的 PQ / HEVC 10-bit 分段视频。跳转优先复用缓冲中的帧，超出缓冲时重新生成。 |

预览刷新上限不代表保证实时帧率。渲染尺寸、交付帧率与采样设置保持独立；预览不会降低母版精度。自动 HDR 需浏览器与显示设备支持实际 HEVC/PQ 解码，否则回退 sRGB；也可手动选择 sRGB SDR。HDR 启动需要缓冲，自动化检查不证明实体屏幕的亮度表现。

正式浮点交付使用最多 4 MiB 的传输块，复用浏览器 float32 读回缓冲，并在 Python 消费当前帧时提前采集下一帧。下一帧只留在浏览器内存中，每路在导出盘最多保留一帧 `.f32`；采样、色彩变换、Alpha 和交付精度不变。EXR 母版附带视频时，验证后的 EXR 像素直接进入编码器，避免全部母版完成后再次读取整段序列；Half 母版仍按其实际存储像素生成视频。WGSL 缓存与浮点母版逐帧追加恢复记录，完成时原子生成完整清单，减少长序列的重复写入。

浮点动画交付新增“渲染并发”1／2／4 路（默认 1 路），对应 CLI/MCP 的 `output.render_workers`。独立进程并行生成和处理不同帧，按绝对帧号和有理数时间计算，通过每路一个固定大小的共享内存槽按序送入同一个编码器；不拆分视频码流，不改变编码质量、Alpha 或采样。仅视频、EXR 母版及两者同时输出均支持；React SDR 保持单路。提交会按并发数量检查内存和临时磁盘需求，取消/失败会清理整组进程和共享内存。并行依赖源码遵守现有逐帧确定性约定；共享 GPU 的实际收益需测量。修改并发只影响新提交任务，不会改变运行中的任务。

任务总耗时包含最终验证与发布。浮点交付的 `verification.json.performance` 另记录母版处理和传输阶段耗时；浏览器采集与消费会重叠，分阶段时间不能直接相加。性能依赖场景、尺寸、采样、编码格式和导出磁盘，不承诺固定加速倍数。

## 色彩与交付

“动画代码”使用 `Three.js RGBA32F → 浮点读取 → OCIO → ACEScg EXR`，母版不烘焙显示变换。SDR 预览使用 ACES 2.0 sRGB 变换；HDR 预览与视频使用 ACES 1000-nit PQ/HLG 输出变换，可选 HEVC Main10 或 Apple ProRes 422 HQ（MOV、10-bit 4:2:2）。GPU 会实际测试负值、超过 1 的高光和 float32 读回；无法满足时明确失败。同时支持 `remotion-sdr-v1`：React/DOM/SVG/ThreeCanvas → sRGB 截图 → SDR 视频。历史未标记引擎的动画按 SDR 打开，源码与修订记录不变。

`WGSL 代码 → 线性 ACEScg 生成与处理 → ACES 变换 → sRGB 网页预览 / 用户选择的交付空间`

`shade(ctx)` 返回非预乘的线性 ACEScg RGBA：RGB 可为负值或大于 1，Alpha 为 0–1，始终不参与色彩转换。代码中的 RGB 常量按 ACEScg 解释，不需要另选输入色彩空间或在代码中烘焙显示变换。

颜色参数的 `color_space` 仅声明参数的存储编码（ACEScg 或 sRGB）；参数进入 `ctx.params` 前统一转换为线性 ACEScg，不会改变整个场景的生成空间。WGSL 网页预览固定使用 ACES 2.0 sRGB SDR 显示变换，不随交付设置改变，也不替代 HDR 校准监看。

在交付面板分别选择输出色彩空间与 Gamma。显示交付应用一次 ACES 输出变换；ACEScg、ACES2065-1 或 ACEScct 场景编码仅做色彩空间转换，不烘焙显示效果。可用组合仍受封装、编码器与位深限制。

<details>
<summary><strong>交付格式、磁盘预算与恢复规则</strong></summary>

### ACEScct 12-bit 调色中间片

浮点动画新增 `output.video=prores-4444-acescct`。选择“仅视频”可直接逐帧输出 MOV，不保留 EXR；也可与 EXR 同时交付。原生 `prores_videotoolbox` profile 4 使用 P416 高精度输入并验证相邻 12-bit Y/Cb/Cr 码值；不提供 XQ，不回退为 10-bit。能力不足时显示原因。

线性黑底合成后只进行 ACEScg → ACEScct 编码，不烘焙显示变换，不带 Alpha。整数存储前拒绝超出 0–1 的 Log 码值。AP1/D60 与 ACEScct 通过 MOV 声明和 `color-encoding.json` 记录；标准 colr 为 2/2/1，BT.709 仅表示 YCbCr 打包矩阵。Resolve 请手动指定 ACEScct 输入和 Video 数据范围。这是制作中间片，不是标准 ACES2065-1 归档容器；报告不会把声明验证说成自动颜色识别或 Resolve 导入认证。

磁盘面板区分“预计成品体积”和“预计磁盘峰值占用”，不增加 20%、30% 或 EXR 10% 余量。快速规格估算不创建文件；“采样估算”按当前完整草稿、正式尺寸和采样倍率处理最多 12 帧，使用真实 ZIP 文件和 ProRes 包的平均大小外推，不使用最大帧放大。未采样的编码部分及未知开销明确注明；估算不是体积上限。采样估算只保留一帧临时原始数据；正式导出每路最多一帧，发布重命名不计第二份视频。

CLI/MCP 与网页共享 `start_motion_render_estimate`、`get_motion_render_estimate`、`cancel_motion_render_estimate`。提交可传 `storage_estimate_id`，采用匹配源码、颜色、时间、输出和运行环境的采样结果验盘。内容改变即失效，名称改变不失效；刷新只重读磁盘和音轨。采样可取消、不保存草稿、不创建正式渲染任务，超时或中断后清理临时样片；正式任务仍保留自己的恢复规则。

### 调色母版与完整色彩标签

动画的“视频输出”支持 **Apple ProRes 422 HQ · MOV**：浮点源可选 sRGB、PQ 或 HLG，SDR 网页源可选 sRGB。支持仅视频及 EXR 母版附带视频；视频合成到黑底，不携带 Alpha。需要本机 `prores_ks` 编码器，导出后检查实际 codec、HQ profile、位深、采样、帧率与原生色彩标签。SDR 网页截图仍为 8-bit 源，不因 ProRes 编码而增加源精度。CLI/MCP 的 `submit_motion_render` 使用 `output.video=prores-hq-srgb|prores-hq-pq|prores-hq-hlg`，通过 `get_motion_delivery_options` 查询当前源的可用选项。ProRes 视频空间按 `prores_ks` HQ 的压缩宏块目标码率计算，按帧数累积，不增加百分比余量；不再用未压缩帧体积预算压缩视频。3840×2160、60 fps、9 分 20 秒的仅视频规格估算约 121 GiB，实际文件随画面复杂度变化；不会为调整预算而降低编码质量。

“动画导出”面板按所选导出模式提供 **渲染预估磁盘空间**，随当前草稿尺寸、帧数、Half/Float 精度、超采样、视频输出和导出目录更新。分别显示 EXR 未压缩体积、导出盘所需空间预算、磁盘可用空间，以及中间数据、视频、音轨和工作区元数据明细。ZIP 实际体积取决于画面；预估不预留空间、不扣除已有缓存，提交时使用同一套预算重新检查。修改音轨或释放磁盘后可点击“刷新预估”。

MCP 使用 `chromascript.estimate_motion_render`，CLI 使用同名操作：

```sh
.venv/bin/chromascript call estimate_motion_render --arguments '{"settings":{"width":3840,"height":2160,"frames":300,"fps":{"numerator":60,"denominator":1}},"output":{"master_bit_depth":32,"video":"none","render_scale":2}}'
```

可传 `export_directory` 检查指定磁盘，传 `project_id` 计入项目当前音轨；估算不会创建目录或渲染任务。SDR 源显示中间帧和视频预算，不标作浮点母版。


新建动画默认使用浮点引擎，默认导出 ACEScg 32-bit Float / ZIP EXR 母版、不额外编码视频。`submit_motion_render.output.render_scale` 可选 1、2、4，默认 2，即每个输出像素使用 2×2 空间采样。浮点路径在线性预乘 RGB/Alpha 中平均后解除预乘、转为 ACEScg；SDR 截图在线性 sRGB 光量中合成和缩小，不增加源位深。预览仍为低分辨率单次采样；本项不添加运动模糊。

动画尺寸、总帧数和帧率分子/分母可自定义，整数范围为 1–2147483647，不再限定 4096 像素、36000 帧或 1–240 fps。实际渲染受 GPU 纹理/视口、可用内存、磁盘和编码器约束；不支持时明确失败，不缩放成其他交付规格。有理数帧率始终原样传递并在编码后核对。

浮点和 SDR 动画均直接在 `export_directory` 所选磁盘的任务临时目录生成中间帧、编码文件与最终成果，验证后在同一磁盘发布。本机工作区保留任务数据库与日志；浏览器仍使用系统临时配置。支持外部硬盘，断开或替换导出卷时失败，不回退写入本机。SDR 和仅视频模式重试时从头渲染；保留母版模式重试复用已验证 EXR。

自定义 shader 保持可用。报告分别记录 `float_pipeline_verified` 与 `source_color_contract`。浮点验证不自动证明任意源码的颜色语义；`scene_linear_master` 表示是否交付场景线性母版；仅视频模式为 false，浮点源仍独立验证。人工审核后通过界面“记录源码审核”或共享 `record_motion_color_review(scene_id, expected_revision_id, reviewer, notes)` 记录依据。审核绑定修订及源码哈希；修订改变后为未审核，旧任务保留提交时的审核记录。未审核不阻止创作和渲染。

显示交付必须使用能准确标记色域和传递函数的组合。默认 SDR 为 Rec.709 色域 / sRGB，HDR 为 Rec.2020 / PQ 或 HLG；通用交付也保留受支持的 P3-D65 和 Gamma 2.2。纯 Gamma 2.4/2.6 及 AP1/ACEScct 没有精确的标准视频标签，不能通过备注或错误套用 BT.709 标签发布；这些旧视频组合禁用；动画 ACEScct 4444 使用独立、明确声明的中间片规则，EXR 与有原生数字 Gamma 字段的 DPX 不受影响。回读验证码流的 primaries、transfer、matrix、range，MOV/MP4 还核对实际 colr 字段；报告、CLI 和 MCP 使用同一规则。

HLG 高饱和高光可能产生大于 1 的编码值。Limited YCbCr 交付以 32-bit float RGB 送入矩阵／范围转换，保留窄范围允许的超白，不在 RGB 阶段裁切；Alpha 独立处理。Full／RGB 整数输出遇到无法表示的 HLG 超白会明确拒绝，请改用 Limited YCbCr；超出窄范围实际码位的信号同样拒绝。HDR 4:2:0 按 BT.2100 使用 top-left 色度共址，并核对原生位置标记。视频交付还须完整严格解码，解码错误即使不改变帧数也不能通过验证；报告以 `decode_verified` 单独记录结果。

Three.js 的 Hex/CSS 颜色表示 sRGB，宿主将其转换到所选线性创作空间；数值 RGB 直接采用该空间。新建浮点动画默认线性 BT.2020，历史未声明源空间的动画保持线性 Rec.709。线性并不等于 SDR 或 0–1；其他编码输入需要真实转换，不能只改标签。

</details>

<details>
<summary><strong>动画源码、线性空间与多文件约定</strong></summary>

HDR 浮点代码的外部包仅允许 `three`，默认导出同步 `createScene({width,height,frames,fps,frameRate})`，返回 `{scene,camera,update({frame,time,fps,width,height})}`。工厂与 update 另提供 `rasterWidth/rasterHeight` 表示实际采样尺寸；使用 `gl_FragCoord` 的 shader 应用它们归一化坐标，布局仍使用逻辑 width/height。每帧直接设置状态，时间为 `frame × 帧率分母 / 分子`。Three 数值颜色采用 `source_color_space` 指定的线性空间，保留负值和超过 1 的值；交由宿主线性转换为 ACEScg/AP1。旧源码省略该字段时保持线性 Rec.709/D65，新建空白浮点动画默认线性 BT.2020。材质不得烘焙 tone mapping 或显示编码。透明材质使用 NormalBlending，输出小于 1 的 Alpha 时必须启用 transparent。允许导入同一动画的分章模块；禁止网络素材及宿主文件导入，代码只在隔离浏览器运行。

不透明自定义 shader 的实际覆盖率固定为 1，宿主只调整编译副本，不改变保存的源码和 RGB。需要半透明时使用 `transparent:true` 与 `NormalBlending`；透明材质的 `NoBlending` 会被拒绝。不透明 GLSL3 自定义 shader 需要唯一可识别的 vec4 颜色输出。透明自定义 shader 默认 `premultipliedAlpha:false`，应输出非预乘 RGB，由混合阶段乘以 Alpha；若设置 `premultipliedAlpha:true`，作者必须输出已经乘过 Alpha 的 RGB，宿主不会自动判断或修正不一致的声明。Three 内置材质保留其原有预乘处理。

原生宽色域创作通过 `MotionSpec.source_color_space` 选择 `"Linear Rec.709 (sRGB)"`、`"Linear Rec.2020"`、`"ACEScg"` 或 `"ACES2065-1"`。工作空间在执行入口及分章顶层代码前设置；数值 `THREE.Color(r,g,b)`、灯光和 shader 输出直接采用所选原色，不需手动转换。十六进制/CSS 颜色仍表示 sRGB，由 Three 转入当前工作空间；新宽色域空间的颜色纹理需预先成为工作空间线性采样并使用 `NoColorSpace`，不能仅重标记 sRGB 纹理。工厂额外获得 `sourceColorSpace` 和 `workingColorSpace`。创作空间不接受 PQ、HLG 或 ACEScct；采样、预乘 Alpha 合成、白点适应与母版转换保持线性，显示变换仅发生于 sRGB/HDR 预览或视频出口。AP0/AP1 超出显示设备范围的颜色仍需在显示出口映射。

已有动画通过界面的“管线目标空间 → 升级色彩管线”，或共享 `upgrade_motion_scene(scene_id, expected_revision_id, source_color_space)` 保色升级。旧源码与分章保存在 `preserved_layers`（最多 16 层），各层保留原线性空间并在浮点像素交界处转换；新内容在目标空间原生创作。图层按顺序合成，**图层之间不共享三维深度**；“编辑图层”可继续修改旧内容。换工作空间不自动扩大旧画面的饱和度。直接在 `update_motion_scene` 改 `source_color_space` 则会重新解释数值，需由作者明确选择。升级是新修订，保留历史、冲突检查、完整源码快照和导出；保留图层源码另存于 `preserved-layers/`。

```sh
.venv/bin/chromascript call create_motion_scene --arguments '{"project_id":"PROJECT_ID","engine":"three-float-v1","source_color_space":"ACEScg"}'
.venv/bin/chromascript call upgrade_motion_scene --arguments '{"scene_id":"SCENE_ID","expected_revision_id":"REVISION_ID","source_color_space":"ACES2065-1"}'
```

视频出口继续选择 `output.video="pq"` 或 `"prores-hq-pq"` 获得 PQ 编码 BT.2020；该选择不改变源空间或 EXR 的线性处理。显示输出仍应用一次 ACES 2.0 输出变换，不能将其理解为纯矩阵变换。

Agent 通过 `get_motion_capabilities.color_authoring` 发现保色升级操作和指南；读取 MCP 资源 `chromascript://docs/motion-color`，或调用 `get_documentation(topic="motion-color")` 获取同一套步骤。MCP 还提供 `upgrade_animation_color_space` prompt。先 `get_motion_scene` 取得真实修订，再调用 `upgrade_motion_scene`；编辑后使用返回的新修订保存完整 spec，保留 `preserved_layers`，不能只改颜色空间字段冒充保色转换。原生 MCP 工具参数包装为 `{"request": {...}}`，HTTP/CLI 参数保持扁平结构。服务升级后重启本地服务并重新连接 MCP，使工具目录与指引刷新。

单场景支持多个分章文件联合渲染。在“源码文件”中切换入口与分章，用“添加分章文件”拆分长动画；入口通过 `import ... from "./chapters/intro"` 引用分章并安排时间。浮点场景共享同一 scene/camera，SDR 可用 Remotion `Sequence` 组织各章。全部文件一起预览、保存、检查修订冲突并冻结到渲染任务，交付中附带完整 `sources/` 源码树。CLI/MCP 的 `MotionSpec` 新增可选 `files: {相对路径: 源码}`，旧 `code` 仍是入口；参见 [多文件示例与调用方式](examples/motion-chapters/README.md)。

本地创作源码可放在 `workspace/authored/`，使用本地 `workspace/tsconfig.json` 指向 `frontend` 的 TypeScript 设置与已安装的 Three 类型声明。工作区不纳入 Git 或前端应用构建。

MCP/CLI 使用 `get_motion_delivery_options` 发现输出，`render_motion_frame` 检查未保存草稿的 sRGB 图片和原始浮点范围，`submit_motion_render` 的 `output` 为 `{"master_bit_depth":32,"video":"pq"}`（video 可选 none/pq/hlg）。浮点动画直接在导出盘的任务临时目录生成逐帧 EXR、源码、快照、缩略图、可选视频与验证报告，验证后原地发布；workspace 不再复制母版。失败或中断保留已验证帧，重试校验帧哈希和冻结的 GPU/运行环境/色彩配置。`inspect_frame` / `sample_pixels` 可检查导出目录中的原始母版。

新增“仅视频”模式使用 `output={"master_bit_depth":null,"video":"pq","render_scale":1}`（视频也可选 `hlg` / `srgb`），通过同一 `submit_motion_render` / `estimate_motion_render` 操作在界面、CLI 和 MCP 使用。每帧浮点读取、线性采样/Alpha 处理与 ACES 输出变换后直接送入 FFmpeg，不写 EXR，也不二次渲染。每路在导出盘仅保留一帧临时 `.f32` 传输文件，消费后删除；成果仍附源码、规格、缩略图和验证报告。默认母版模式保持不变。仅视频不能同时设置 `video="none"`，中断后的显式重试从头开始；已原子发布的成果可直接恢复完成状态。

仅视频空间预算包含单帧中间数据、音轨、辅助文件以及压缩视频估算；规划码率为 `max(8 Mbps, 宽×高×fps×0.2)`，按精确有理帧率算时长，不追加百分比余量；未测量的封装开销会单独注明。规划码率**不是编码器限制**，原有 CRF/质量不变，不能保证实际文件大小；运行时持续检查磁盘余量。4K UHD 60fps、10 分钟、1×1 无音轨的估算约 7.08 GiB，而不是按整段未压缩 EXR 计算。

“下载 ACEScg 母版 ZIP”在下载时流式打包，不在服务端保存 ZIP；浏览器下载位置会保存用户请求的归档。MCP/CLI 可通过 `export_artifact` 将同一虚拟 `master-archive` 写入指定文件。流式 ZIP 不支持断点下载，中断后需重新开始。旧任务文件和快照保留不迁移；升级改变运行时指纹后，旧任务若要继续渲染需重新提交。

模型/CLI 使用 `list_motion_templates`、`create_motion_scene`、`get_motion_scene`、`update_motion_scene`、`compile_motion_preview`、`submit_motion_render` 等共享操作，例如 `chromascript call list_motion_templates`。`get_documentation` 的 `motion` 主题提供完整调用约定。

</details>

<details>
<summary><strong>HDR 预览接口与兼容性细节</strong></summary>

SDR 模板使用`engine=remotion-sdr-v1`，默认导出 React 组件。允许原有 React、Remotion、ThreeCanvas 与 Three.js 导入。`get_motion_delivery_options(engine)` 返回源类型对应的选项；SDR 的 `output` 为 `{"master_bit_depth":null,"video":"srgb"}` 。`rec709`（纯 Gamma 2.4）因缺少精确标准视频标签而禁用。浏览器 SDR 源不会被重新标记为 HDR 或 ACEScg 母版。

不需要通过模板开始：`create_scene` 仅传 `project_id` 即创建空白 WGSL 场景；`create_motion_scene` 仅传 `project_id` 创建空白浮点动画；显式传入 `engine: "remotion-sdr-v1"` 创建 SDR 网页动画。两者均可直接传入 `spec`，也可在创建后使用修订号通过 MCP/CLI 修改源码。空白场景只含可运行的最小透明画面代码，没有示例图形或动画；项目用于归属与历史管理，模板不构成依赖。

HDR 预览通过共享 `encode_motion_hdr_preview(chunk)` 操作在本机编码，HTTP、CLI 和 MCP 使用同一严格模型。输入可为原始线性 RGBA32F 的 Base64 帧，或 GPU 已经显示转换的 P010 PQ。每片段最多 64 帧且不超过 192 MiB，偶数尺寸最高 1280×720（或竖屏 720×1280），保留项目精确有理数帧率。P010 输入不会再执行色调映射。宿主将线性像素转为 ACEScg、合成黑底并应用一次 ACES 2.0 1000-nit PQ 输出变换；验证实际解码属性和 MP4 色彩标签后返回临时 fMP4。无后台导出任务，不保存片段，不修改母版/草稿。`compile_motion_preview(spec, hdr_preview=true)` 启用 720p GPU P010 采集协议；外层播放器负责编码调用和 MSE，源码 iframe 保持 opaque origin、禁止网络和媒体。默认编译调用仍返回独立可用的 sRGB 预览。

Safari 26.2 的 `video-dynamic-range` 查询可能尚未实现。自动 HDR 在 high/standard 均不匹配时改用 `dynamic-range: high`，再核对 HEVC/MSE 以及实际 PQ 编码；已明确报告 SDR 视频平面的设备仍回退 sRGB。

720p HDR 预览通过 transferable buffer 和鉴权二进制接口减少像素复制，优先使用通过实际回读验证的 VideoToolbox Main10；软件回退仍保留原帧率。采集下一片段与编码重叠，播放前缓冲约 2 秒，暂停或跳转只生成一帧清晰 HDR 图像。二进制地址为 `/api/operations/encode_motion_hdr_preview/binary`，`X-Chroma-HDR` 为规格 JSON（含 `frame_count`、`input_format` 和原帧率）；与 MCP/CLI 的 Base64 操作共用处理规则。


采样生命周期：名称修改及保存触发的能力刷新保留正在运行的采样；源码、输出、目录或工作上下文改变后取消。取消和超时会对任务拥有的子进程先终止、再强制结束，初始化失败或线程中断会释放采样槽位。12-bit 能力探测或诊断文件写入失败仅禁用 4444，不影响已有输出格式。动画 EXR 与缩略图均不添加隐藏磁盘余量。

</details>

## 项目音频

导入音频或用代码生成音效、配乐，在“项目音频”中编排时间线并保存。预览音量只影响监听；交付按冻结音轨混音，并附精确时长的 WAV。

<details>
<summary><strong>音轨、SoundFont 配乐、听审与 MCP 参数</strong></summary>

统一“工作区”提供 **项目音频**：导入文件或生成音效，试听素材，点击“添加到音轨”，设置时间线起点、素材起点、播放时长及增益，再保存音轨。音轨用于该项目所有场景的新交付任务；保存后，网页实时场景与动画预览会随播放、暂停、跳转和循环同步播放音轨；首次需用户点击播放。Agent 保存的音轨约每 2 秒自动同步，可直接拖动预览音量条调整 0–100% 音量，0% 即静音，仅影响监听、不改变音轨或导出增益。浏览器预览用于监听，不替代交付的精确样本验证。

Agent 先调用 `chromascript.get_audio_guidelines(project_id)` 获取项目对应的本地保存目录与制作规范（CLI 同名操作）。建议将脚本、源音频和制作清单保存在返回的 `source_directory`，用 `intro-chime-v001.wav` 等版本化名称，保留种子、来源、实际格式与峰值。导入/生成后由服务保存到 `<workspace>/audio/<asset_id>.wav`，禁止直接覆盖；通过 `get_artifact(asset.id).local_path` 查询实际路径。制作优先采用无损 48 kHz 双声道 WAV，使用短淡入淡出，给叠加留出余量；帧位置按 `frame × fps.denominator / fps.numerator` 换算秒，不累加逐帧舍入误差。

配乐使用 `inspect_soundfont` + `create_music`：agent 根据动画编曲，FluidSynth 使用本地 SoundFont 演奏。`create_audio` 继续用于基础音调和音效。先通过 `get_audio_guidelines.music` 或 `get_capabilities.audio.music` 检查 FluidSynth 的可用状态、版本及限制；FluidSynth 是可选的本机依赖，服务不下载或捆绑音源。FFmpeg 用于验证和解码输出。

```sh
.venv/bin/chromascript call inspect_soundfont --arguments '{"soundfont_path":"/absolute/path/instruments.sf2"}'
.venv/bin/chromascript call create_music --arguments @examples/music-score.json
```

调用前修改 [四声部乐谱示例](examples/music-score.json) 中的项目 ID、音源路径和 bank/program。`inspect_soundfont` 读取实际 SF2 预设名称、从零开始的 bank/program、大小与 SHA-256；名称来自文件，不认证真实乐器身份。不要假定所有音源均遵守 GM 编号。当前接受本地绝对路径的 SF2，最多 512 MiB，检查预设头不要求安装 FluidSynth。

`create_music(project_id,name,soundfont_path,score)` 支持最多 15 个声部（例如钢琴、小提琴、中提琴、大提琴）、每声部 4096 音符、总计 16384 音符和 600 秒。声部名称须唯一；声部含 `bank`、`program`、MIDI `volume`（0–127）及 `pan`（-1–1）。音符含 MIDI `pitch`（0–127）、`start_seconds`、`duration_seconds`、`velocity`（1–127）。整体含 `duration_seconds`、`gain_db`（-96–12，默认 0）和 `fade_out_seconds`（0–10 秒，默认 0.02，不超过总时长）。所有音符必须在总时长内，同声部同音高的按键区间不能重叠。

`score.constraints` 将动画制作要求与乐谱一起保存：

- `theme`：主题与情绪文字，由 agent 理解和实现。
- `instrumentation`：必须存在的声部名称列表；实际音色还需匹配音源预设。
- `hit_points`：`label`、`time_seconds`、`tolerance_seconds`（默认 0.05，范围 0–1）及可选 `track_name`。服务要求指定声部或任一非静音声部在容差内有起音。
- `reading_windows`：`label`、`start_seconds`、`end_seconds`、`max_velocity`（默认 64）和 `allow_note_onsets`（默认 true）。窗口内重叠的按键音符不能超过力度限制；设为 false 可禁止窗口内起音。

可选效果与踏板字段（MCP、CLI、HTTP 使用同一模型）：

- `score.reverb`：对象启用混响，默认参数为 `{"room_size":0.5,"damp":0.2,"width":0.8,"level":0.7}`；`room_size`、`damp`、`level` 范围 0–1，`width` 范围 0–100。
- `score.chorus`：对象启用合唱，默认参数为 `{"nr":3,"level":0.6,"speed":0.2,"depth":4.25}`；`nr` 为 0–99 个声部，`level` 为 0–10，`speed` 为 0.1–5 Hz，`depth` 为 0–256 ms。两种效果省略或设为 null 时关闭；传 `{}` 使用上述默认值。
- 声部的 `reverb_send`、`chorus_send`：可选 0–127 整数，分别发送 CC91、CC93；省略或 null 保留原生送量默认值。启用效果时建议显式设置送量。效果参数全局共享，SoundFont 调制器也会影响效果量，最终混入 WAV。
- 声部的 `sustain_pedal`：例如 `[{"down_seconds":0,"up_seconds":1.8}]`。每段必须在采样舍入后先踩下再松开，段间不可重叠（允许首尾相接），松开后必须至少保留 `max(0.02, fade_out_seconds)` 秒。相同时刻依次发送松键、松踏板、踩踏板、按键；同音按键区间的重叠校验不变。

约束时间相对于音乐素材，编曲时从场景时间中减去预定的音轨起点；窗口含起点、不含终点。服务校验乐谱时间与落点，不分析画面或认证听感；阅读窗口不检查踏板延音、释放与效果尾音。MIDI 时间使用每秒 48000 ticks，但 FluidSynth 内部调度仍有时间量化，音色本身也可能缓慢起音，需试听。音符时长指按键时间，踏板踩下时松键的音会保持到松踏板；应按音色和效果实际需要留出超过最低要求的尾音余量，最后按指定总时长裁切并淡出。合成器增益 0.2、256 发声数、单 CPU 核心，原生渲染超时为 300 秒。

返回的 `generation_report` 同时保存在 `asset.metadata.generation_report`，记录实际样本数、时长、采样率、声道、PCM 样本峰值/dBFS、饱和前峰值、削波声道样本数、WAV 哈希、音源快照哈希与预设、完整乐谱及约束、落点检查、FluidSynth 版本/选项/输出探测和实现哈希。输出增益与淡出在浮点域完成后才转换为 32-bit 整数 PCM，不自动归一化。新增 `loudness` 含 `integrated_lufs`、`loudness_range_lu`、`true_peak_dbtp`、`method`、FFmpeg 版本与原始 Summary，通过 FFmpeg `ebur128=peak=true` 测量最终 WAV。`create_audio`、`import_audio` 也在 `asset.metadata.loudness` 保存同样的测量；听审在 `report.objective.loudness` 测量选中的原始立体声片段。Summary 分辨率为 0.1 LU/dB；静音真峰值、低于或等于 -70 LUFS 门限或没有有效门控块（含不足 400 ms 片段）的综合响度为 null，短片段的 LRA=0 不代表有意义的响度范围。测量失败不会发布素材，测量结果不等于交付认证。保留原音源与乐谱以便重复生成，不保证跨 FluidSynth 版本或平台逐位一致。更新代码后重启服务和 MCP 客户端以加载新字段。分轨、片段预览、公开 MIDI 导入/导出和多版本自动试配留待后续扩展。

本地 Qwen2.5-Omni / llama.cpp 可作为配乐听审模型，MCP 与 CLI 提供同名操作：

```sh
.venv/bin/chromascript call get_audio_model
.venv/bin/chromascript call configure_audio_model --arguments '{"config":{"base_url":"http://127.0.0.1:8080","model":"ggml-org/Qwen2.5-Omni-7B-GGUF:F16"}}'
.venv/bin/chromascript call review_audio --arguments @examples/audio-review.json
```

配置前从 `get_audio_model.models[].id` 复制完整 ID，保留 `:F16` 等后缀；上面的 ID 只是本机当前示例，不能用模型仓库名替代服务提供的 ID。服务重新部署后若 ID 改变，需重新配置。

运行音频辅助 Python 脚本时，使用 ChromaScript 仓库中的 `.venv/bin/python`。本机 Mido 安装在该环境中；Mido 是外部 MIDI 制作脚本的可选依赖，内置 `create_music` 不依赖它，也不要求 agent 先手工生成 MIDI。

`get_audio_model` 检查服务就绪状态、实际模型 ID 与声明的音频输入能力；不能仅凭模型名称判断支持。`configure_audio_model` 验证成功后将连接配置保存到当前工作区，固定模型 ID，不下载、启动或切换模型。服务需已加载音频投影并接受本机无认证请求；仅连接 HTTP 回环地址（`127.0.0.1`、`::1`、`localhost`），不经环境代理、不跟随重定向。默认地址为 `http://127.0.0.1:8080`。可配置 `timeout_seconds`（5–600，默认 180）、`max_tokens`（128–4096，默认 1536）、`temperature`（0–1，默认 0.2）与 `seed`（默认 42）。

编曲反馈流程为 **生成素材 → `review_audio` → agent 根据反馈修改乐谱 → 生成新版本并复查 → 保存选中的音轨**。调用传入 `project_id`、`asset_id`，可选 `review` 中指定 `start_seconds`、`duration_seconds`、`brief` 和 `focus`，见[听审参数示例](examples/audio-review.json)。每次检查 0.1–30 秒；未指定时长表示检查剩余全部素材，超过 30 秒会明确拒绝，不会静默只听开头。长配乐应按章节、公式阅读窗口和转场分段检查，保留覆盖范围。

真实音频以 base64 WAV 传给模型；监听副本转换为 16 kHz 单声道 16-bit PCM，不做响度归一化，原始 48 kHz 双声道素材不变。生成时的主题、配器及相关落点/阅读窗口会作为“创作目标”附带；它们不代表模型实际听到了这些内容。模型未收到画面，不能认证音画同步，也不能通过监听副本认证原始声像、高频质量、LUFS、真峰值或削波。

结果直接反馈给调用 agent：`report.objective` 是源片段的实测样本峰值、每声道 RMS、满幅样本计数及 `loudness`（综合响度、响度范围、真峰值与测量方法）；`report.feedback` 是模型的总结、优点、问题、建议和不确定性。满幅样本数不等于已证实削波。问题时间是片段内近似秒数，加上 `objective.start_seconds` 才是素材时间。`feedback_status="complete"` 仅表示结构有效；截断、无效 JSON、越界时间或模型切换会返回 `invalid`、原始回答和原因。网络或模型拒绝请求则明确报错，不伪造听审结果。模型意见可能出错，agent 应结合客观结果和制作要求判断；不会自动修改音符、覆盖素材或替换音轨。

听审报告作为 `audio-review` JSON 成果保存在 `<workspace>/audio-reviews/`，可通过现有 `get_artifact`、`export_artifact` 读取/导出。报告记录素材与监听副本哈希、检查范围、上下文、模型 ID/配置/服务版本、token 使用量及提示词/实现指纹；不认证模型权重哈希或跨运行环境的逐位重现。每个服务同时执行一项听审，不阻塞普通编辑操作。模型协议依据 [llama.cpp 服务文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)。

基础音效可通过 `chromascript.create_audio` 生成。以下是相同操作的 CLI 示例（替换项目 ID）：

```sh
.venv/bin/chromascript call create_audio --arguments '{
  "project_id": "PROJECT_ID",
  "name": "双音提示",
  "spec": {
    "duration_seconds": 1,
    "seed": 42,
    "events": [
      {"waveform":"sine","frequency_hz":440,"start_seconds":0,"duration_seconds":0.2,"gain_db":-18},
      {"waveform":"sine","frequency_hz":660,"start_seconds":0.3,"duration_seconds":0.3,"gain_db":-18}
    ]
  }
}'
```

生成接口返回 `asset.id`。先调用 `get_project_audio` 读取当前 `revision_id`（初始为 `"0"`），再用 `set_project_audio` 将素材放进项目音轨：

```json
{
  "project_id": "PROJECT_ID",
  "expected_revision_id": "0",
  "timeline": {
    "clips": [{"asset_id":"ASSET_ID","start_seconds":0,"gain_db":0}]
  }
}
```

所有接口均可用 `chromascript call 操作名 --arguments @参数.json` 调用；MCP 使用同名 `chromascript.*` 工具。`get_documentation(topic="audio")` 提供完整输入契约。`import_audio` 接受 `source_path`（本地绝对路径）或 `data_base64` 二选一；外部工具生成的音频也可通过此接口进入项目。

本地合成支持正弦波、三角波、噪声、定时事件、声像和淡入淡出。重复事件组成节奏，重叠事件组成和弦；不调用音乐或语音模型。每段素材最多 10 分钟，导入文件最多 128 MiB；素材统一为 48 kHz 双声道 32-bit 整数 PCM，原始音频实际属性另行记录，存储位深不等于源精度。

交付冻结音轨修订与素材校验和。后续修改不影响已排队任务；使用修改后的音轨需新任务键。视频交付混入音频（MP4/AAC，MOV/MKV/MXF 使用 PCM，仍须通过实际编码验证），所有带音频的交付额外包含精确时长的 `audio.wav`。EXR 等图像序列通过交付接口获得独立 WAV；单帧、保存的预览文件和单独的 `render_sequence` 操作仍静音。音轨空白补静音、超出画面时长裁切；报告记录实际解码样本数和削波情况。WAV 混音须小于 4 GiB。音频不进入 OCIO，画面色彩流程保持独立。

</details>

## OpenRouter 云端配音（Claude / MCP / CLI）

`generate_speech` 是明确的云端、可能付费的配音操作；原有本地合成、配乐和渲染流程不依赖它。发送内容仅为台词、表演要求、声音和模型参数，不上传视频。Gemini TTS 用于旁白/对白，不生成背景音乐，也不保证台词时长或口型同步。

将仓库的 `.env.example` 复制为 `.env`，在本地填写并执行 `chmod 600 .env`（仅当前用户可读写；拒绝符号链接；不要把密钥发给 agent，也不要提交 Git）：

```dotenv
OPENROUTER_API_KEY=你的OpenRouter密钥
OPENROUTER_TTS_MODEL=google/gemini-3.8-flash-tts
OPENROUTER_TTS_VOICE=Kore
```

配置每次调用时读取，优先级为 **服务进程环境变量 > `<workspace>/.env` > `<workspace父目录>/.env`**，不依赖 Claude 的启动目录。默认 `workspace` 位于仓库下，因此仓库根目录 `.env` 可直接使用。支持上述配音键及 `OPENROUTER_MUSIC_MODEL`、`OPENROUTER_REQUIRE_ZDR`、`OPENROUTER_MUSIC_REQUIRE_ZDR`、`OPENROUTER_ANALYSIS_MODEL` 的单行字面值，可使用引号、`export` 前缀和行尾注释，不执行 shell、不展开变量。更新 `.env` 不需要重启，但本次代码升级需要重启 ChromaScript 服务并重新连接 MCP。进程已有同名环境变量时，应在服务启动环境中更新或删除它。

先检查配置（仅返回密钥是否存在，不验证账号、余额或模型可用性，不发起网络请求）：

```sh
.venv/bin/chromascript call get_speech_config
```

复制 `examples/speech.json` 并替换项目 ID、台词和请求键，然后主动执行以下命令才会调用付费服务：

```sh
.venv/bin/chromascript call generate_speech --arguments @examples/speech.json
```

`request` 支持 `text`（1–12000 字符）、`instructions`（最多 4000 字符）、可选 `model`、`voice`、`response_format`。省略模型/声音时读取 `.env`，未设置则使用上面的默认值。模型必须是 OpenRouter 的完整 `provider/model` ID；声音须由所选模型支持，接口不会自动换模型。默认格式 `pcm`；Gemini TTS 仅支持 `pcm`，其他模型可按其文档选择 `mp3`。表演要求放在 `instructions`，不要混进需要逐字朗读的台词。

**同一请求重试必须使用同一个 `idempotency_key`。** 完成后返回同一素材；内容改变需新键。失败或进程中断留下的键不会自动再次请求上游，以免重复付费，超时仍可能产生费用。接口返回 `speech_not_retried` 时先检查返回的源文件目录；如已有 `source.wav` / `source.mp3`，可以用 `import_audio` 恢复。只有明确决定重新生成时才使用新键。每个服务同时运行一项配音，普通编辑不被云端等待阻塞。

源音频与 `generation.json` 保留在 `<workspace>/speech-sources/<request_id>/`；返回的 `asset` 使用现有不可覆盖素材机制，仅公开模型、上游生成 ID、响应哈希、源编码、实际解码属性、响度和隐私路由政策。台词、表演要求与完整制作记录只保留在本地私有目录的 `generation.json`，不放入返回的素材元数据/SQLite 记录。Gemini PCM 默认按其文档的 24 kHz、单声道、16-bit 封装 WAV，优先读取响应头的采样率/声道；其他 PCM 模型必须返回这些头字段。归一存储为 48 kHz 双声道 32-bit PCM **不代表原始精度升级**，也不自动归一化音量。最多接收 128 MiB / 600 秒，网络读取有超时和时限，不自动重试或跟随重定向。

Claude Desktop 使用下节已有的 MCP 配置即可，无需第二个 MCP 服务，也无需在客户端配置中填写 OpenRouter Key。新增工具是 `chromascript.get_speech_config` 和 `chromascript.generate_speech`，HTTP 同步提供 `POST /api/operations/generate_speech`，CLI 同名。给 Claude 的示例指令：

> 先读取 ChromaScript 音频制作规范与配音配置。为项目 PROJECT_ID 制作一段自然、克制的普通话旁白，使用已配置的 OpenRouter TTS。这次允许调用云端配音。台词为“城市渐渐安静下来，而我们的故事才刚刚开始。”使用请求键 narration-intro-v001，失败时不要换键自动重试。生成后给我素材信息；读取当前音轨修订，再保留已有音轨，将旁白放到第 3 秒。

生成操作只创建素材；`get_project_audio` → `set_project_audio(expected_revision_id, timeline)` 才修改项目音轨。通过 `get_artifact(asset.id)` 获取可播放/导出的本地 WAV。精确画面对齐需测量实际生成时长后编辑，现有音轨混音和视频导出流程继续适用。

接口依据：[OpenRouter TTS 文档](https://openrouter.ai/docs/guides/overview/multimodal/tts)、[Gemini 3.8 Flash TTS](https://openrouter.ai/google/gemini-3.8-flash-tts)。仓库测试使用模拟上游响应并实际解码 PCM，不产生真实费用；模型质量、账号权限和实际计费需要用户配置后验证。

## Lyria 配乐与 Gemini 配音同时使用

`generate_music` 使用 OpenRouter 的流式聊天音频接口；`generate_speech` 使用语音接口。它们共用 `.env` 中的 Key，但各自保留模型配置：

```dotenv
OPENROUTER_API_KEY=你的OpenRouter密钥
OPENROUTER_TTS_MODEL=google/gemini-3.8-flash-tts
OPENROUTER_TTS_VOICE=Kore
OPENROUTER_MUSIC_MODEL=google/lyria-3-pro-preview
```

新增 `chromascript.get_music_config`（无网络/费用）和 `chromascript.generate_music`，自动通过现有 MCP、HTTP 和 CLI 暴露。无需第二个 MCP 服务；更新后重启 ChromaScript 服务并重新连接 Claude。`create_music` 仍是原有的本地 FluidSynth 乐谱渲染，和新增云端音乐生成是独立操作。

```sh
.venv/bin/chromascript call get_music_config
# 替换示例中的项目 ID 后执行；此操作发送云端请求并可能收费。
.venv/bin/chromascript call generate_music --arguments @examples/cloud-music.json
```

`request` 字段：

| 字段 | 作用 |
|---|---|
| `prompt` | 配乐描述，1–12000 字符；可写情绪、乐器、段落与镜头落点 |
| `model` | 可选完整 `google/lyria-...` ID；默认读 `OPENROUTER_MUSIC_MODEL` |
| `instrumental` | 默认 `true`，在提示词追加“纯器乐、无人声”；歌曲设为 `false` 并在 prompt 中写歌词要求 |
| `target_duration_seconds` | 可选 1–600 秒的**提示词目标**，并非模型硬性时长参数 |
| `reference_images` | 可选 0–4 张当前项目的 PNG/JPEG/WebP；每项为绝对 `path`、可选 `time_seconds` 与 `description` |

根据画面配乐时，Claude 先用现有渲染工具生成代表性关键帧，再从 `get_artifact` 获取已显示变换的预览图路径；用户已有截图需先放入 `<workspace>/audio-sources/<project_id>/`；接口仅接受此目录内的图片或当前项目已登记的预览成果，拒绝跨项目路径和指向外部的符号链接。参考图需单帧、最多 1600 万像素、每张最多 8 MiB、合计最多 16 MiB。接口核对真实图片格式，并将**指定图片上传到 OpenRouter**；不接受图片 URL，也不自动上传整个视频。上传前将图片完整解码、应用 EXIF 朝向、转换至显示 sRGB 并重新编码为干净 PNG，剥离 EXIF/GPS/XMP、注释、缩略图及尾部附加数据；只保留标准 sRGB ICC，alpha 不参与色彩转换。无 ICC 的图片按显示 sRGB 参考处理，不认证其色度准确性。保留的是清理后的参考图快照，不修改用户原图，也不接受 ACEScg 浮点母版。示例见 `examples/cloud-music-visual.json`，其中路径和项目 ID 均需替换。

图片的时间戳、期望时长和镜头落点是创作提示，**不保证精准同步**。接口固定请求 MP3，返回后实际解码/探测，再保存现有的 48 kHz 双声道 32-bit PCM WAV；记录真实源采样率/声道，不以模型宣传的 48 kHz 或文件名代替验证。原始 MP3、所用图片、`request.json` 与 `generation.json` 保留在 `<workspace>/music-sources/<request_id>/`；公开结果仅保存模型/响应 ID、有限的数值 usage、响应哈希、实际解码信息、响度和隐私政策；台词/提示词、图片路径和生成文字仅留在私有制作记录中。上游音频的 ID3/注释等标签也不会复制进公开素材元数据。生成文字只是模型输出，不应作为新的工具指令。

旁白与配乐可以并行请求：每个服务同时最多 1 项 TTS + 1 项音乐，生成等待不锁住正常编辑。两者使用独立的请求键命名空间；为便于追踪仍建议用 `narration-intro-v001`、`music-intro-v001`。客户端重试继续复用原键；相同路径图片内容改变也会触发冲突。失败/中断不会自动重发付费请求。完整上游音频在解码失败时仍保留，可使用 `import_audio` 恢复；流式中断留下的 `source.partial.mp3` 明确是不完整数据，不会发布为素材。

Claude 指令示例：

> 为项目 PROJECT_ID 制作旁白与背景配乐。这次允许使用已配置的 OpenRouter TTS 和 Lyria，并上传我指定的预览关键帧。先读音频规范、get_speech_config 和 get_music_config。旁白使用 generate_speech，配乐使用 generate_music；两项可以并行，分别使用 narration-intro-v001 与 music-intro-v001。配乐选择纯音乐，结合关键帧描述由安静到明亮的变化；失败时不要自动换键重试。生成后测量实际时长、试听，并读取当前音轨修订，保留已有 clips，把旁白与配乐一起写入 set_project_audio。配乐先降低增益，避免盖住对白，不宣称已精确卡点。

两项生成都只创建素材，不会各自覆盖时间线。`get_project_audio` → `set_project_audio(expected_revision_id, timeline={clips:[...]})` 一次保存完整组合；通过每条 clip 的起点、源裁切、时长和增益控制对齐与混音。后续现有视频交付会冻结两类素材并导出混音 WAV。

协议依据：[OpenRouter 音频输出](https://openrouter.ai/docs/guides/overview/multimodal/audio)、[图像输入](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding)、[指定 Lyria 模型](https://openrouter.ai/google/lyria-3-pro-preview)。验证使用模拟云端响应与真实本地解码/混音；不代表已经验证 Preview 模型的实时可用性、计费、音质或画面同步。

### 云端音频隐私边界

- 默认请求 `provider={data_collection:"deny", zdr:true}`。`.env` 中 `OPENROUTER_REQUIRE_ZDR` 默认 `true`；配乐优先使用 `OPENROUTER_MUSIC_REQUIRE_ZDR`，未设置时才继承全局值。分析接口固定要求 ZDR，不受这两个开关降低。不能通过工具调用参数降低要求；无合规提供商时失败，不自动重试或降级。**Lyria Preview 若只有不符合 ZDR 的路由，将无法生成。** 只有用户明确接受供应商的数据保留政策后，才应在本地设置 `OPENROUTER_MUSIC_REQUIRE_ZDR=false`（保持 `OPENROUTER_REQUIRE_ZDR=true` 以保护配音）；`data_collection:"deny"` 仍然保持。ZDR 是 OpenRouter 的路由约束，不是本项目对第三方的独立审计，也不会关闭用户在 OpenRouter 账号中主动启用的提示词日志。
- 密钥仅从本地权限为 0600 的常规 `.env` 文件或服务进程环境读取，并只放在固定 HTTPS 请求的 Authorization 头中；不跟随重定向、不使用环境代理、不执行 dotenv 内容。检测到生成参数中出现已配置 Key 或 OpenRouter Key 格式时，在请求前拒绝。HTTP、CLI、MCP 参数验证错误均不会回显误填的密钥值。
- 台词、提示词和明确选择的参考图仍需发送给 OpenRouter 及其提供商才能生成。图片清理只去掉隐藏元数据，**不会自动遮挡画面里的人脸、姓名、地址或屏幕文字**。授权的 Claude/MCP 客户端仍可访问该工作区；这不是针对同一系统用户下恶意进程的隔离沙箱。
- 新建 `speech-sources` / `music-sources` 及请求目录为 0700，保留的源音频、图片和 JSON 为 0600，云端生成的托管 WAV 也为 0600。完整制作记录不再自动进入 MCP 返回、项目列表和公开素材元数据；本地私有文件仍保存原始台词/提示词以支持恢复，未加密，不自动到期删除。共享、备份或导出这些私有目录之前需自行决定是否保留。此修复不追溯删除旧数据库、旧文件或已有备份中的内容。

政策参考：[OpenRouter ZDR](https://openrouter.ai/docs/guides/features/zdr)、[提供商数据政策](https://openrouter.ai/docs/guides/routing/provider-selection)。

## Gemini 文本／音频分析（analyze_media）

`chromascript.analyze_media` 通过 OpenRouter `/chat/completions` 调用 `google/gemini-3.8-flash`，返回分析文字，供 Claude 做听审、拟定配乐提示词或分析节奏/情绪。它与生成语音的 `generate_speech`、生成音乐的 `generate_music` 是三条独立通路；原来的 `review_audio` 仍只连接本地模型。

本版输入支持**文本及当前项目中已登记的音频素材**。图片、视频直接输入尚未实现；不应据此声称已经分析画面能量或验证视听同步。外部音频先用 `import_audio` 导入，再传 `audio_asset_id`，不接受任意文件路径或 URL。

```dotenv
OPENROUTER_REQUIRE_ZDR=true
# 仅当用户接受配乐上游的常规保留政策时设 false：
OPENROUTER_MUSIC_REQUIRE_ZDR=false
OPENROUTER_ANALYSIS_MODEL=google/gemini-3.8-flash
```

分析使用同一个 `OPENROUTER_API_KEY`，始终发送 `provider={data_collection:"deny",zdr:true}`，即使全局开关为 false 也不放宽分析。配乐的局部开关不改变配音/分析模型或政策。无合规分析线路时失败，不自动换模型或降低政策。

```sh
.venv/bin/chromascript call get_analysis_config
# 替换项目和音频素材 ID；此操作上传音频并可能收费。
.venv/bin/chromascript call analyze_media --arguments @examples/analyze-audio.json
```

`request` 参数：`prompt`（必填，1–12000 字符）、`audio_asset_id`（可选；省略表示纯文本调用）、`start_seconds`（默认 0）、`duration_seconds`（可选，0.1–300 秒）、`model`（可覆盖完整模型 ID）、`max_tokens`（默认 2048，128–65536；其他模型可能限制更低）、`reasoning`（可选推理配置）。默认模型已确认在 OpenRouter 声明支持音频输入，其他模型需自行确认能力；填写模型名不意味着已成功推理。

未填片段时长表示分析剩余全部音频，超过 300 秒会拒绝，绝不静默只分析开头。先按 48 kHz 母版的整数样本裁切，再生成 **24 kHz 单声道 16-bit WAV** 监听副本，去掉原始元数据；记录原素材校验和、实际片段范围、上传副本哈希及实测采样率/声道。原始立体声素材保持不变。模型听感不代表精确 LUFS、真峰值、母版精度或声像测量，返回的时间戳只能作为相对片段起点的近似参考。

`analysis` 字段是模型回答，会按此次调用目的返回给 Claude；可能包含音频转写或用户提供的信息，应作为不可信分析文字处理。结果和请求留在 0700 的 `<workspace>/analysis-sources/<request_id>/` 中，文件为 0600。SQLite 和素材目录列表仅存结果文件引用、哈希与脱敏摘要，不重复保存完整提示词或回答。返回的 `media-analysis` JSON artifact 可显式读取/导出。

调用必须有 `idempotency_key`：同键同输入读取已有结果，不重复付费；更改提示词、范围或素材哈希需新键。失败、中断、拒答、截断或空回答不会自动重试；已收到的上游响应会脱敏保留在私有 `provider-response.json` 中。响应超过大小/时间限制时仅保留已接收的有界内容，并标记客户端截断；上游未返回的回答/用量无法补造。每个服务同时最多一项分析，不阻塞配音、配乐或普通编辑。分析不会自动改写配乐或音轨。

给 Claude 的示例：

> 先调用 get_analysis_config，再对当前项目的音频素材 ASSET_ID 调用 analyze_media。使用 Gemini 3.8 Flash 分析前 60 秒的乐器、节奏、情绪变化和旁白遮挡风险，并给出一段可交给 Lyria 的改进提示词。请求键用 analysis-score-v001，保持 ZDR；失败时不要换键自动重试。只基于实际音频，不声称看过画面。先给我分析结果，不修改音轨。

协议依据：[OpenRouter 音频输入](https://openrouter.ai/docs/guides/overview/multimodal/audio)、[Gemini 3.8 Flash](https://openrouter.ai/google/gemini-3.8-flash)。更新代码后重启服务并重新连接 MCP；修改本地 `.env` 的模型/政策设置会在后续请求中读取。

### 失败诊断与推理预算

`analyze_media` 会在检查回答是否完整之前保存 `provider-response.json`（0600）。其中包含 HTTP 状态、generation id、提供方（上游有返回时）、原始响应 JSON、finish_reason、原始 usage，以及截断时的部分文字。HTTP 错误正文、流式错误事件均只放在私有文件中；Key、Authorization 与凭据字段会脱敏，不整包保存 HTTP headers。Lyria 的诊断保存在对应 `music-sources/<request_id>/provider-response.json`，不再只有字节计数。

工具错误详情返回精简的 `diagnostics_path`、`http_status`、`generation_id`、`provider_name`、`finish_reason` 和 `usage`。`analysis_truncated` 表示 token 预算耗尽，`analysis_refused` 表示明确拒答/内容过滤；其他异常结束仍为 `analysis_incomplete`。失败键重试仍不发请求，但会再次返回已保存的诊断摘要。**不会自动换键、扩大预算或重试付费请求。**

当上游提供 `completion_tokens_details.reasoning_tokens` 时，返回的 `usage` 区分总 `completion_tokens`、`reasoning_tokens` 和由两者相减得到的 `output_tokens`；缺少推理统计时不猜测，统计矛盾时输出 token 数为 null。上游返回的 `usage.cost` 会保留，缺少费用时保持未知，不当作零。目前尚未自动查询 `/generation` 的最终结算金额；旧版本已丢弃的回答与 generation id 无法从本地恢复。

作曲类请求可显式使用：

```json
{
  "prompt": "根据已确认的乐器、时间点和情绪要求编写乐谱。",
  "max_tokens": 16384,
  "reasoning": {"effort": "low", "exclude": true}
}
```

`reasoning.effort` 支持 `none/minimal/low/medium/high/xhigh`，由实际模型决定接受程度；也可改用 `reasoning.max_tokens`，必须小于总 `max_tokens`，并与 effort 二选一。`exclude=true` 只要求不返回推理文字，**不会关闭推理或免除推理计费**。Gemini 3 对数字推理预算可能映射为思考等级，不能保证精确 token 数；优先使用 `effort:low` 控制。上限 65536 对应当前 Gemini 3.8 Flash 的公开端点限制，不代表任意其他模型都接受此数值。

[OpenRouter 推理参数说明](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)。JSON Schema、temperature、seed、费用回查等本轮未增加。

### 大项目音频查询

`get_project_audio(project_id)` 默认返回资产摘要（ID、名称、时长、采样格式、哈希、精简响度与削波标记）及完整时间线和修订号，不再复制每项素材的 ffprobe、完整乐谱和原始响度日志。前端播放/编辑不需要修改；项目复制内部明确读取完整元数据，避免丢失来源信息。

- 查询所有完整数据：`get_project_audio(project_id, include_metadata=true)`，可能很大。
- 只查一项完整数据：`get_artifact(asset_id)`，素材信息在 `artifact.metadata` 中。

建议模型先读摘要选择素材，再按 ID 查详情。`import_audio` 的生成返回结构本轮未改动。

## MCP 接入

在 MCP 客户端中添加以下配置，将两处占位路径替换为实际绝对路径：

```json
{
  "mcpServers": {
    "chromascript": {
      "command": "/absolute/path/ChromaScript/.venv/bin/chromascript",
      "args": ["--workspace", "/absolute/path/ChromaScript/workspace", "mcp"]
    }
  }
}
```

MCP 自动启动的网页固定使用 [http://127.0.0.1:8765](http://127.0.0.1:8765)，端口被占用时不会自动改用随机端口。同一工作区已有健康服务时会继续复用其地址（包括手动指定的端口）。旧版本已启动的随机端口服务需在任务结束后停止，再重新连接 MCP，才会切换到固定端口。

## 常见问题

**服务无法启动或网页打不开？**

查看启动终端的报错。提示安装不完整时先运行 `./install.sh`；端口被占用时，运行 `./start.sh --port 8766`，再打开对应端口。MCP 自动启动的服务地址记录在 `workspace/server.json`；该文件含访问令牌，请勿分享。

**网页提示需要构建，或更新后界面未变化？**

重新执行 `./install.sh`，然后重启服务并刷新网页。脚本报告缺少依赖或版本错误时，按提示安装上述版本；已有 `.venv` 不兼容时，先将该目录移走再安装。

**导出选项不可用或渲染失败？**

运行 `.venv/bin/chromascript doctor` 检查本机环境，并查看任务错误。缺少编码器或滤镜时，安装包含所需组件的 FFmpeg 后重启服务；规格不兼容时，按交付面板提示调整封装、编码、位深或色彩设置。

**导入 DaVinci Resolve 后颜色不一致？**

根据交付报告核对输入色彩空间、Gamma 和数据级别。部分组合需要手动指定，具体见 [Resolve 导入说明](docs/RESOLVE_IMPORT.md)。

**渲染中断后如何继续？**

运行 `.venv/bin/chromascript resume <job-id>`，将 `<job-id>` 替换为任务 ID。MCP 断开不会停止后台任务；依赖或数值实现变化导致恢复被拒绝时，需重新提交任务。

恢复还会核对实际 worker GPU 与提交时冻结的后端身份；后端变化时不复用旧母版。编码和封装处理代码也属于运行时指纹，相关实现更新后须重新提交旧任务。

**如何更改数据或导出目录？**

使用 `./start.sh --workspace /path/to/data` 指定数据目录。导出时点击“选择文件夹”打开系统选择器，也可直接输入任意可写目录的绝对路径，不需要提前配置允许目录。选择器取消后保留原路径。任务完成后，点击“打开导出文件夹”即可在访达中查看文件；图像序列导出也支持这两个操作。已有文件不会被覆盖。

网页、MCP 与 CLI 共用 `choose_export_directory`（可选 `initial_directory`）和 `open_export_directory`（`directory`）操作，例如 `chromascript call choose_export_directory`。系统文件夹选择器目前用于 macOS；其他系统可直接输入目录。旧脚本的 `--export-root` 参数仍可解析，但不再限制导出位置。

## 开发与验证

使用 Python 3.12 锁定环境。修改设计规范后由脚本生成 UI tokens；测试产物和工作区数据不纳入 Git。

```sh
.venv/bin/python -m pytest tests/test_rendering.py tests/test_live_preview.py -q
.venv/bin/ruff check src tests
.venv/bin/mypy src
npm --prefix frontend run test:unit
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run build
```

网页交互验证优先使用内置浏览器；自动化配置为 **Playwright WebKit、headless**，不连接个人浏览器配置。启动独立测试工作区后，可运行原生预览回归：

```sh
.venv/bin/chromascript --workspace /tmp/chromascript-test serve --port 8875
# 在另一个终端运行：
CHROMASCRIPT_URL=http://127.0.0.1:8875 npm --prefix frontend run test:e2e -- tests/live-preview.spec.ts
```

截图、日志和报告保存在 `reports/`、`frontend/test-results/` 与 `frontend/playwright-report/`。动画离线导出所需的浏览器运行时与网页测试引擎是不同路径；执行测试前检查用例是否会触发离线导出。

正式浮点导出可使用只启动 headless WebKit 的测试驱动，覆盖最终采样尺寸、Node/Python ACK、真实 EXR 和视频完整解码，不启动 Chrome：

```sh
.venv/bin/python -m pytest tests/test_export_pipeline.py tests/test_frame_manifest.py -q
# BASELINE_COMMIT 指向优化前修订；输出目录必须尚不存在。
.venv/bin/python scripts/benchmark_exports.py --baseline-ref BASELINE_COMMIT --output /tmp/chromascript-export-benchmark
```

基准交替执行修改前后版本，比较仅母版、仅视频及母版附带视频的完整任务耗时，并逐帧核对 EXR 像素和视频解码校验和。默认使用合成场景、640×360、2×2 采样和 30000/1001 fps；可通过参数调整。WebKit 结果不等于生产 Chromium 驱动或真实作品的速度认证。


### 声音工作区与 Agent 试听协作

顶部“声音”阶段包含“方向试听”“时间线”“音频素材”。Agent 先导入或生成素材，再使用 `create_audition` 提供 1–8 个、0.5–30 秒的候选片段。用户可普通试听或盲听，保存偏好、文字反馈及可选五项评分，确认单一方向、结合多个参考或重新探索。确认方向不会自动生成音频或修改时间线。

轮次冻结画面版本、素材片段、原配乐和片段响度；切换候选只切换声音增益。反馈、方向、揭晓和归档都带修订检查，并保留历史。草稿在当前会话内跨候选、轮次、分页和阶段保留，刷新页面不保证恢复。新候选请创建新轮次；可关联上一轮已确认的方向版本。

选择“添加候选到时间线…”会追加到现有音轨草稿，保留其他编辑，核对起点、裁剪和增益后才通过“保存音轨”生效。素材可以同时被试听轮次和时间线引用。所有能力均由同一注册表提供 HTTP、MCP 与 CLI；详见 MCP 资源 `chromascript://docs/auditions`。

```sh
.venv/bin/chromascript call create_audition --arguments @examples/audition.json
.venv/bin/chromascript call get_audition --arguments '{"project_id":"PROJECT_ID","audition_id":"AUDITION_ID"}'
```

盲听用于避免界面中的来源暗示，不是针对项目拥有者的权限隔离。暂无手机访问或自动付费生成。

### Finder 项目目录与两级数据库

执行 `migrate_workspace_storage` 后，当前工作区启用 `project-folders-v1`：

- `workspace/state.sqlite3`：项目位置、记录路由、跨项目请求去重索引、全局配置，以及无法确定归属的旧请求记录。
- `workspace/项目/项目名称__短ID/project.sqlite3`：该项目的真实数据源，包含项目、作品、全部修订、音频、试听、任务、快照、请求记录和文件路径映射。
- 已归档项目位于 `workspace/已归档/`。新建和复制的项目自动使用独立文件夹；项目改名和归档在空闲时同步移动文件夹。
- 每个项目生成 `项目说明.md`、当前作品源码/配置、时间线及试听轮次 JSON。它们是便于查看的导出，数据库是修订来源；手动编辑导出文件后，同步会报告冲突并保留文本。

```sh
.venv/bin/chromascript call get_project_storage
.venv/bin/chromascript call open_project_folder --arguments '{"project_id":"PROJECT_ID"}'
.venv/bin/chromascript call sync_project_folder --arguments '{"project_id":"PROJECT_ID"}'
.venv/bin/chromascript call export_project_folder --arguments '{"project_id":"PROJECT_ID","destination":"/absolute/path/项目备份"}'
```

这些操作同样提供 HTTP 和 MCP。整体备份请使用 `export_project_folder`，或先停止服务再复制完整项目文件夹，不要只复制运行中的 SQLite 主文件。备份目标必须不存在。恢复时将完整文件夹放入新工作区的“项目”目录，项目身份由 `project.json` 与项目数据库共同确认；缺失的全局索引可以重建。相同项目 ID 的两份副本不能同时放在“项目”/“已归档”内，创建独立副本使用 `clone_project`。

迁移不改写历史快照或修订内容。旧逻辑路径通过项目库的路径表解析到新文件夹，迁移清单记录校验值与缺失项。外部交付仍使用原位置；项目内的新交付可以随完整目录搬移后读取。已冻结的未完成任务若目标目录搬移，应新提交交付。旧数据库及已移动源文件保留在 `workspace/迁移备份/`，未归属资料保留原位。
