# ChromaScript

本地运行的 ACES 2.0 程序化视频工作台，通过网页、MCP 或 CLI 创建画面、预览动画并导出视频与图像序列。

## 功能介绍

- **场景创作**：从模板开始，用 JSON 和 WGSL 定义画面，调整参数、颜色与关键帧。
- **实时预览**：播放、暂停和逐帧查看动画，检查原始像素值。
- **色彩管理**：以 float32 ACEScg 渲染，支持 SDR、HDR 和透明通道；原始浮点数据与显示预览分别保留。
- **多格式交付**：导出 EXR、PNG、TIFF、DPX 图像序列，以及 H.264、HEVC、ProRes、DNxHR 等视频；可用组合由本机能力决定。
- **任务管理**：查看渲染进度、下载成果，恢复符合条件的中断任务。
- **模型调用**：所有产品操作均提供 MCP 工具和 CLI 调用。场景示例见 [examples](examples/README.md)。

## 安装方法

需要 macOS、Python 3.12、Node.js 22.12+（含 npm），以及包含 `libx264`、`libx265`、`prores_ks`、`dnxhd` 编码器和 `zscale` 滤镜的 FFmpeg。确保 `ffmpeg` 和 `ffprobe` 可在终端调用。

下载仓库后，在终端执行：

```sh
# 将占位路径替换为本机仓库的实际路径。
cd /absolute/path/ChromaScript
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/chromascript serve
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，创建项目并选择场景模板，即可开始预览和交付。数据默认保存在当前目录下的 `workspace`。

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

查看启动终端的报错。端口被占用时，运行 `.venv/bin/chromascript serve --port 8766`，再打开对应端口。MCP 自动启动的服务地址记录在 `workspace/server.json`；该文件含访问令牌，请勿分享。

**网页提示需要构建，或更新后界面未变化？**

在仓库目录重新执行 `npm --prefix frontend ci` 和 `npm --prefix frontend run build`，然后重启服务并刷新网页。构建报 Node.js 版本错误时，确认版本满足上述要求。

**导出选项不可用或渲染失败？**

运行 `.venv/bin/chromascript doctor` 检查本机环境，并查看任务错误。缺少编码器或滤镜时，安装包含所需组件的 FFmpeg 后重启服务；规格不兼容时，按交付面板提示调整封装、编码、位深或色彩设置。

**导入 DaVinci Resolve 后颜色不一致？**

根据交付报告核对输入色彩空间、Gamma 和数据级别。部分组合需要手动指定，具体见 [Resolve 导入说明](docs/RESOLVE_IMPORT.md)。

**渲染中断后如何继续？**

运行 `.venv/bin/chromascript resume <job-id>`，将 `<job-id>` 替换为任务 ID。MCP 断开不会停止后台任务；依赖或数值实现变化导致恢复被拒绝时，需重新提交任务。

**如何更改数据或导出目录？**

使用 `.venv/bin/chromascript --workspace /path/to/data serve` 指定数据目录，`--workspace` 必须放在子命令之前。导出到工作区外时，启动服务需添加 `--export-root /path/to/exports`；已有文件不会被覆盖，请选择新路径。
