# ChromaScript

本地运行的 ACES 2.0 程序化视频工作台，通过网页、MCP 或 CLI 创建画面、预览动画并导出视频与图像序列。

## 功能介绍

- **场景创作**：从模板开始，用 JSON 和 WGSL 定义画面，调整参数、颜色与关键帧。
- **HDR / SDR 动画代码**：同时提供浮点 Three.js、SDR React 文字和 ThreeCanvas 模板；统一预览，按源类型导出 HDR/SDR 视频或浮点母版。
- **实时预览**：播放、暂停和逐帧查看动画，检查原始像素值。
- **色彩管理**：代码生成与内部处理固定为 float32 线性 ACEScg（AP1），网页使用 sRGB 预览，输出色彩空间在交付时选择；统一通过 OCIO / ACES 2.0 转换，原始浮点数据与显示预览分别保留。
- **多格式交付**：导出 EXR、PNG、TIFF、DPX 图像序列，以及 H.264、HEVC、ProRes、DNxHR 等视频；可用组合由本机能力决定。
- **任务管理**：查看渲染进度、下载成果，恢复符合条件的中断任务。
- **模型调用**：所有产品操作均提供 MCP 工具和 CLI 调用。场景示例见 [examples](examples/README.md)。

## 色彩工作流程

“动画代码”使用 `Three.js RGBA32F → 浮点读取 → OCIO → ACEScg EXR`，母版不烘焙显示变换。预览使用同一套 ACES 2.0 SDR sRGB 变换；HDR 视频使用 ACES 1000-nit PQ/HLG 输出变换与 HEVC Main10。GPU 会实际测试负值、超过 1 的高光和 float32 读回；无法满足时明确失败。同时支持 `remotion-sdr-v1`：React/DOM/SVG/ThreeCanvas → sRGB 截图 → SDR 视频。历史未标记引擎的动画按 SDR 打开，源码与修订记录不变。

`WGSL 代码 → 线性 ACEScg 生成与处理 → ACES 变换 → sRGB 网页预览 / 用户选择的交付空间`

`shade(ctx)` 返回非预乘的线性 ACEScg RGBA：RGB 可为负值或大于 1，Alpha 为 0–1，始终不参与色彩转换。代码中的 RGB 常量按 ACEScg 解释，不需要另选输入色彩空间或在代码中烘焙显示变换。

颜色参数的 `color_space` 仅声明参数的存储编码（ACEScg 或 sRGB）；参数进入 `ctx.params` 前统一转换为线性 ACEScg，不会改变整个场景的生成空间。网页预览固定使用 ACES 2.0 sRGB SDR 显示变换，不随交付设置改变，也不替代 HDR 校准监看。

在交付面板分别选择输出色彩空间与 Gamma。显示交付应用一次 ACES 输出变换；ACEScg、ACES2065-1 或 ACEScct 场景编码仅做色彩空间转换，不烘焙显示效果。可用组合仍受封装、编码器与位深限制。

## 安装方法

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

制作动画时，点击顶部“动画代码”，选择 SDR 网页或浮点渲染类型，点击“新建空白动画”；“示例模板（可选）”中保留文字、几何、光能等起点。修改 TypeScript 后自动预览；保存后选择 EXR 16-bit Half 或 32-bit Float，以及仅母版、PQ、HLG、sRGB 或 Rec.709 视频，点击“导出动画”。EXR 保留直通 Alpha，视频在场景线性空间合成黑底。视频要求偶数宽高与本机 libx265；EXR 可使用奇数尺寸。浏览器预览始终为 sRGB SDR，不作为 HDR 亮度参考。

HDR 浮点代码仅导入 `three`，默认导出同步 `createScene({width,height,frames,fps,frameRate})`，返回 `{scene,camera,update({frame,time,fps,width,height})}`。每帧直接设置状态，时间为 `frame × 帧率分母 / 分子`。Three 数值颜色采用线性 Rec.709/D65，允许超过 1；交由宿主转换为 ACEScg/AP1。材质不得烘焙 tone mapping 或显示编码。透明材质使用 NormalBlending，输出小于 1 的 Alpha 时必须启用 transparent。禁止网络素材与文件导入，代码只在隔离浏览器运行。

本地创作源码可放在 `workspace/authored/`，使用本地 `workspace/tsconfig.json` 指向 `frontend` 的 TypeScript 设置与已安装的 Three 类型声明。工作区不纳入 Git 或前端应用构建。

MCP/CLI 使用 `get_motion_delivery_options` 发现输出，`render_motion_frame` 检查未保存草稿的 sRGB 图片和原始浮点范围，`submit_motion_render` 的 `output` 为 `{"master_bit_depth":32,"video":"pq"}`（video 可选 none/pq/hlg）。浮点动画直接在导出盘的任务临时目录生成逐帧 EXR、源码、快照、缩略图、可选视频与验证报告，验证后原地发布；workspace 不再复制母版。失败或中断保留已验证帧，重试校验帧哈希和冻结的 GPU/运行环境/色彩配置。`inspect_frame` / `sample_pixels` 可检查导出目录中的原始母版。

“下载 ACEScg 母版 ZIP”在下载时流式打包，不在服务端保存 ZIP；浏览器下载位置会保存用户请求的归档。MCP/CLI 可通过 `export_artifact` 将同一虚拟 `master-archive` 写入指定文件。流式 ZIP 不支持断点下载，中断后需重新开始。旧任务文件和快照保留不迁移；升级改变运行时指纹后，旧任务若要继续渲染需重新提交。

模型/CLI 使用 `list_motion_templates`、`create_motion_scene`、`get_motion_scene`、`update_motion_scene`、`compile_motion_preview`、`submit_motion_render` 等共享操作，例如 `chromascript call list_motion_templates`。`get_documentation` 的 `motion` 主题提供完整调用约定。

如需通过模型调用，在 MCP 客户端中添加以下配置，将两处占位路径替换为实际绝对路径：

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

**如何更改数据或导出目录？**

使用 `./start.sh --workspace /path/to/data` 指定数据目录。导出时点击“选择文件夹”打开系统选择器，也可直接输入任意可写目录的绝对路径，不需要提前配置允许目录。选择器取消后保留原路径。任务完成后，点击“打开导出文件夹”即可在访达中查看文件；图像序列导出也支持这两个操作。已有文件不会被覆盖。

网页、MCP 与 CLI 共用 `choose_export_directory`（可选 `initial_directory`）和 `open_export_directory`（`directory`）操作，例如 `chromascript call choose_export_directory`。系统文件夹选择器目前用于 macOS；其他系统可直接输入目录。旧脚本的 `--export-root` 参数仍可解析，但不再限制导出位置。

SDR 模板使用 `engine=remotion-sdr-v1`，默认导出 React 组件。允许原有 React、Remotion、ThreeCanvas 与 Three.js 导入。`get_motion_delivery_options(engine)` 返回源类型对应的选项；SDR 的 `output` 为 `{"master_bit_depth":null,"video":"srgb"}` 或 `rec709`。浏览器 SDR 源不会被重新标记为 HDR 或 ACEScg 母版。

不需要通过模板开始：`create_scene` 仅传 `project_id` 即创建空白 WGSL 场景；`create_motion_scene` 仅传 `project_id` 创建空白 SDR 动画，增加 `engine: "three-float-v1"` 则创建空白浮点动画。两者均可直接传入 `spec`，也可在创建后使用修订号通过 MCP/CLI 修改源码。空白场景只含可运行的最小透明画面代码，没有示例图形或动画；项目用于归属与历史管理，模板不构成依赖。
