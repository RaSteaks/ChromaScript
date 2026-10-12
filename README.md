# ChromaScript

本地、可由模型调用的 ACES 2.0 程序化视频工作台。用 JSON 和 WGSL 定义画面，使用 Metal / WebGPU 渲染 float32 ACEScg，生成 EXR 母版与标准视频交付。

## 启动

需要 macOS、Python 3.12、Node.js，以及包含 libx264、libx265、prores_ks、dnxhd、zscale 的 FFmpeg。

    cd /Users/rasteaks/Desktop/个人/GitHub/ChromaScript
    python3.12 -m venv .venv
    .venv/bin/python -m pip install -r requirements.lock
    .venv/bin/python -m pip install -e . --no-deps
    npm --prefix frontend ci
    npm --prefix frontend run build
    .venv/bin/chromascript serve

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。默认数据目录为仓库下的 workspace，已排除 Git 跟踪。

    .venv/bin/chromascript --workspace /path/to/data serve --port 8765
    .venv/bin/chromascript doctor
    .venv/bin/chromascript operations
    .venv/bin/chromascript call list_templates '{}'

CLI 的 --workspace 在子命令之前。所有产品操作均可通过 call 使用 JSON，参数以 @file.json 读取也可。守护服务由 doctor / MCP 自动启动时可能使用空闲端口，实际地址记录在 workspace/server.json；该文件含本地访问令牌，不应分享或提交。

## 模型通过 MCP 使用

本地客户端配置示例：

    {
      "mcpServers": {
        "chromascript": {
          "command": "/absolute/path/ChromaScript/.venv/bin/chromascript",
          "args": ["--workspace", "/absolute/path/ChromaScript/workspace", "mcp"]
        }
      }
    }

服务也提供 Streamable HTTP：本地服务地址加 /mcp/，使用 server.json 中的令牌作为 Bearer 认证。不同客户端的连接配置格式可能不同。

所有工具使用 chromascript. 命名空间，参数放在 request 对象内。例如：

    chromascript.create_project
    {"request": {"name": "色彩实验"}}

    chromascript.create_scene
    {"request": {"project_id": "<项目 ID>", "template_id": "particles"}}

创作流程：查询能力与模板 → 创建场景 → 编译验证 → 关键帧和短片预览 → 获取图像及原始像素 → 修改 → 验证交付配置 → 提交任务 → 检查成果。

全部能力均提供 MCP 工具，网页 MCP 页可复制本机实际客户端配置；完整示例见 [examples](examples/README.md)。

长任务立即返回 job_id。使用 get_job 查看进度，get_artifact(include_image=true) 取得原生 PNG 图像，inspect_frame / sample_pixels 检查 EXR 数值。MCP 连接断开不会停止后台任务。get_artifact 同时返回本地绝对路径与受本地会话保护的媒体 URL。图像序列可通过 export_artifact 整体复制；网页任务面板提供序列导出、EXR 和说明文件下载。

文档资源包括 chromascript://docs/workflow、scene、color、delivery；提示词 create_procedural_video 可向模型提供创作流程。完整操作 Schema 可通过 operations 命令或网页 MCP 接口页查询。


自主交付的 MCP 示例：

    chromascript.get_delivery_options
    {"request": {"configuration": {"container": "exr"}, "changed_field": "container"}}

将返回的 `configuration` 对象放入 `configurations` 数组提交；下面示例为直通 Alpha 的 ACEScg EXR：

    chromascript.submit_configured_delivery
    {"request": {"scene_id": "<场景 ID>", "settings": {"width": 1920, "height": 1080, "frames": 60, "fps": {"numerator": 30, "denominator": 1}}, "configurations": [{"container": "exr", "codec": "exr", "profile": null, "bit_depth": 16, "sampling": "RGB", "alpha_mode": "straight", "color_output": "ACEScg", "data_range": "full", "compression": "zip", "quality_mode": "fixed", "crf": null}], "idempotency_key": "my-delivery-1"}}

## 场景约定

场景入口：

    fn shade(ctx: SceneContext) -> vec4<f32> {
        let brightness = 0.18 * exp2(ctx.params.exposure);
        return vec4<f32>(brightness, brightness, brightness, 1.0);
    }

返回直通 Alpha 的线性 ACEScg RGBA。Alpha 必须为 0..1；RGB 可为负数或大于 1。工具验证每个采样值，禁止 NaN / infinity。参数由 SceneSpec.parameters 定义，使用 ctx.params.NAME 访问。

ctx.pixel 为左上原点的全局像素坐标；ctx.uv 为左下原点的归一化坐标。ctx.time 来自精确有理数帧率，送入 GPU 的时间为 float32；ctx.frame、ctx.seed 为整数。场景不能依赖真实时钟或上一帧。

采样在线性空间累积预乘颜色，最后恢复直通 Alpha。无 Alpha 输出在线性空间合成到黑色后执行输出变换。PNG 使用直通 Alpha；EXR/TIFF/支持的专业视频提供明确的关联方式。

    .venv/bin/chromascript preview <scene-id> --frame 60
    .venv/bin/chromascript render <scene-id> --container exr --bit-depth 32 --compression zip --alpha-mode straight --width 3840 --height 2160 --frames 300
    .venv/bin/chromascript render <scene-id> --container mp4 --codec hevc --color-output PQ --width 3840 --height 2160 --frames 300
    .venv/bin/chromascript resume <job-id>
    .venv/bin/chromascript inspect <artifact-id>

## 色彩与交付

固定使用 OpenColorIO 2.5.0、ACES 2.0 CG Config v4.0.0，保存配置校验值、场景版本、依赖版本和数值实现指纹。

- EXR：ACEScg 或 ACES2065-1；half / float，ZIP / PIZ；正式 ACES2065-1 存档为无压缩 half。保留有效负值与高光，half 溢出明确报错。
- SDR：ACES 2.0 100 nit Rec.709 输出视图、Rec.1886 显示编码。
- PQ：ACES 2.0 1000 nit、Rec.2020 限制色域输出。
- HLG：使用同一 HDR 画面呈现结果，通过 OCIO 官方 1000 nit HLG 显示变换编码。
- sRGB 预览与原始 ACES 浮点数值分别提供；Alpha 不进入颜色处理器。
- 工具空间转换和显示输出变换不同：ACEScct 等工作空间转换不应用显示呈现；sRGB / Rec.709 / PQ / HLG 别名使用明确的 ACES 输出视图。

交付面板由用户通过联动下拉列表选择封装、编码、档位、位深、色度采样、输出色彩、Alpha、范围、压缩和质量控制，覆盖 MP4/MOV H.264、HEVC、全部六档 ProRes、DNxHR MOV/MXF、EXR、PNG/TIFF/DPX、FFV1、无压缩 RGB/YUV 及平台构图。可添加多个独立交付目标。更改选项会更新其他可选项，并提示兼容性调整；任务提交前会再次校验。旧预设接口仅为已有脚本及历史任务保留，网页不再使用预设。

每项能力记录实际已验证组合；编码器存在不等于整个规格已验证。ProRes / DNxHR 软件编码输入最高 10-bit，解码器显示 12-bit 存储不代表端到端 12-bit。自定义预设也会检查输入像素格式精度。

submit_configured_delivery 直接接受 configurations 数组，无需保存预设。get_delivery_options 返回合法选项、联动调整和允许帧率；validate_delivery_configuration 与 estimate_delivery 分别校验和估算当前配置。项目默认色彩和旧批量接口保留兼容，显式交付配置决定本次输出。

图像序列附带 sequence.json，记录精确帧率、帧索引、时间码起点和色彩配置；EXR 也写入标准有理数 FramesPerSecond 元数据。不同后期软件对整数序列元数据的支持不同，应根据 sidecar 明确指定输入色彩和时间码。

ProRes / DNxHR 的部分容器不单独暴露范围标记，报告区分显式信号与编码约定；测试使用实际黑白阶梯验证合法范围。HDR 不写未经测量的 mastering / MaxCLL / MaxFALL，文件检查不等同于显示器校准。

## 数据与恢复

workspace 中保存 SQLite 状态、不可变快照、共享 float32 EXR 缓存、任务日志、预览和输出。依赖或数值实现变化后拒绝恢复旧任务，使用新幂等键创建新任务。取消保留已完成母版；编码失败可以重试。

导出限制在工作区和启动时显式指定的 --export-root 内，已有文件不会被覆盖。仅接受场景 JSON/WGSL，不执行场景附带的 Python / Shell。

首版为本地二维程序化图形与素材工具，不包括音频、三维渲染、DCP、IMF、云端运行和内置付费模型服务。

## 验证

    .venv/bin/pytest -q
    .venv/bin/ruff check src tests scripts
    .venv/bin/mypy src/chromascript
    npm --prefix frontend run typecheck
    npm --prefix frontend run lint
    npm --prefix frontend run build
    npm --prefix frontend run test:e2e
    .venv/bin/python scripts/design_tokens.py --check
    .venv/bin/python scripts/acceptance.py --full --formats --high

原格式回归脚本通过真实 MCP stdio 与 HTTP 验证创建、修改、预览图像、幂等、取消/恢复、4K60 300 帧、1080p 预设、8K 静帧与 120fps。结果按工作区写入 reports/acceptance-<workspace名称>.json，可通过 --report 指定文件。

    .venv/bin/python scripts/acceptance_configured.py
    .venv/bin/python scripts/recovery_acceptance.py
    .venv/bin/python scripts/audit_samples.py --workspace workspace/acceptance --acceptance reports/acceptance-acceptance.json

开发验收产生的样片、缓存和原始报告已于 2026-10-12 按要求清理；保留文字验收记录。重新执行以上命令会生成新的测试产物。

自主交付验收脚本覆盖联动选项、直接提交、四类输出、幂等、冻结版本及 stdio / HTTP 一致性。

恢复验收使用独立测试工作区，覆盖断连、服务重启、进程中断、缓存损坏和编码失败。实际执行记录见 [reports/VALIDATION.md](reports/VALIDATION.md)；[Resolve 导入说明](docs/RESOLVE_IMPORT.md)供你手动验收。

MIT License，保留原仓库许可证。
