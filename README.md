# ChromaScript

本地运行的 ACES 2.0 程序化视频工作台，通过网页、MCP 或 CLI 创建画面、预览动画并导出视频与图像序列。

## 功能介绍

- **场景创作**：从模板开始，用 JSON 和 WGSL 定义画面，调整参数、颜色与关键帧。
- **动画代码**：左侧直接编辑 React/TSX，右侧通过 Remotion Player 播放、暂停和逐帧查看；支持文字动画与 Three.js 三维场景，并在本机导出 MP4。
- **实时预览**：播放、暂停和逐帧查看动画，检查原始像素值。
- **色彩管理**：代码生成与内部处理固定为 float32 线性 ACEScg（AP1），网页使用 sRGB 预览，输出色彩空间在交付时选择；统一通过 OCIO / ACES 2.0 转换，原始浮点数据与显示预览分别保留。
- **多格式交付**：导出 EXR、PNG、TIFF、DPX 图像序列，以及 H.264、HEVC、ProRes、DNxHR 等视频；可用组合由本机能力决定。
- **任务管理**：查看渲染进度、下载成果，恢复符合条件的中断任务。
- **模型调用**：所有产品操作均提供 MCP 工具和 CLI 调用。场景示例见 [examples](examples/README.md)。

## 色彩工作流程

“动画代码”使用独立路径：`React/TSX + Remotion/ThreeCanvas → 浏览器逐帧 PNG → FFmpeg → MP4`。输出为 8-bit sRGB / H.264，透明背景合成黑色；不生成 ACEScg 浮点母版，不重复应用 ACES 显示变换。下面的 ACEScg 流程适用于原有 WGSL 工作区。

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

日常只需运行 `./start.sh`，打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，创建项目并选择场景模板。启动不联网安装依赖，数据默认保存在仓库下的 `workspace`，按 `Ctrl+C` 停止服务。

制作 Remotion / Three.js 动画时，点击顶部“动画代码”，选择项目并新建文字或三维动画。修改左侧 TSX 后，右侧自动编译预览；下方可修改尺寸、帧数与有理数帧率。点击“保存动画”后再“导出 MP4”，在同页查看任务进度、下载视频和验证报告。导出文件夹还保留 TSX 源码与 JSON 规格。

在 VS Code 中编辑本地动画时，将源码放在 `workspace/authored/` 下。本地 `workspace/tsconfig.json` 复用 `frontend` 的 TypeScript 设置和已安装依赖，为 React、Remotion 与 JSX 提供类型检查；无需在工作区重复安装依赖。可运行 `frontend/node_modules/.bin/tsc --project workspace/tsconfig.json --noEmit` 检查这些源码。该配置与动画源码均属于本地工作区，不纳入 Git 或前端应用构建。

代码默认导出一个 React 组件，用 `useCurrentFrame()` 和 `useVideoConfig()` 计算每帧状态。三维场景使用 `@remotion/three` 的 `ThreeCanvas`，避免 `useFrame()` 累加时间、`Date.now()` 或非固定随机数。允许导入 React、Remotion、Three.js、React Three Fiber 和 `@remotion/three`，不支持网络素材或任意文件导入。编译错误会保留上一版可用画面和当前草稿。

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
