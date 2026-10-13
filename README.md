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

## 网页工作流

1. **选择场景**：新建或选择项目，再从模板创建场景；已有场景可继续编辑。
2. **编辑与预览**：在预览旁的“画面规格”中设置尺寸、宽高、帧率、总帧数、采样与时间码。修改参数、颜色或 WGSL 后自动显示当前草稿，无需先保存。实时预览长边最多 480 像素、每像素一次采样；可播放、暂停或拖动时间轴查看运动。保存后仍可渲染当前帧、三帧或前 2 秒动画，并检查原始像素。
3. **设置交付**：沿用预览中的共用画面规格，分别选择封装、编码、色彩和质量。需要改规格时可返回预览；检查汇总后提交交付。
4. **任务与成果**：查看进度，选择任务下载文件，或通过“导出序列”复制全部图像帧。

可随时通过顶部步骤返回修改；切换步骤保留本次页面中的草稿。交付成功提交后自动进入任务页，后台渲染不受步骤切换影响。

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

`render_live_preview` 通过同一 HTTP / MCP / CLI 操作接收 `request.spec`（完整草稿）、`width`、`height`、有理数 `fps` 和整数 `frame`，直接返回低分辨率 sRGB PNG（MCP 原生图像），不保存场景或创建任务。CLI 可使用 `.venv/bin/chromascript call render_live_preview --arguments @draft-preview.json`。实时预览只降低空间分辨率和采样数；ACES 显示变换与 Alpha 规则一致，正式交付精度不受影响。

文档资源包括 chromascript://docs/workflow、scene、color、delivery；提示词 create_procedural_video 可向模型提供创作流程。完整操作 Schema 可通过 operations 命令或网页 MCP 接口页查询。


自主交付的 MCP 示例：

    chromascript.get_delivery_options
    {"request": {"configuration": {"container": "exr"}, "changed_field": "container"}}

将返回的 `configuration` 对象放入 `configurations` 数组提交；下面示例为直通 Alpha 的 ACEScg EXR：

    chromascript.submit_configured_delivery
    {"request": {"scene_id": "<场景 ID>", "settings": {"width": 1920, "height": 1080, "frames": 60, "fps": {"numerator": 30, "denominator": 1}}, "configurations": [{"container": "exr", "codec": "exr", "profile": null, "bit_depth": 16, "sampling": "RGB", "alpha_mode": "straight", "color_space": "ACEScg", "gamma": "linear", "data_range": "full", "compression": "zip", "quality_mode": "fixed", "crf": null}], "idempotency_key": "my-delivery-1"}}

## 输出色彩设置

输出设置分别记录色彩空间 / 色域、传递函数 / Gamma、目标显示峰值亮度和数据级别。传递函数名称为 Linear、Gamma 2.2、Gamma 2.4、Gamma 2.6、sRGB（IEC 61966-2-1）、PQ（SMPTE ST 2084）及 HLG（ARIB STD-B67）；具体组合由本机能力矩阵约束。

P3 采用 **P3-D65**，色域与 Gamma 分别选择，支持本机已实现的 Gamma 2.2 / 2.4 / 2.6、sRGB 和 PQ 组合；P3-D65 + sRGB 对应 Display P3。ProRes 不固定 Gamma，切换到 P3-D65 会保留兼容的 Gamma 2.4。P3 Gamma 2.4/2.6 和 Rec.709 Gamma 2.6 当前限定于 MOV 声明流程：传递函数标记为未指定，原生 comment 和报告记录准确编码，导入时须手动解释；其他容器中的不可用项变灰并注明原因。只读色彩空间标签和 Gamma 标签随实际像素输出生成。规格与本机 Resolve 21 手册的对应关系见[交付和导入说明](docs/RESOLVE_IMPORT.md)。BT.2020 与 Rec.2020 为同一色域。ACEScct 使用 AP1 对数编码，可导出 10-bit 及以上视频或浮点 EXR；ACEScg/AP0 线性母版使用 EXR。

数据级别按 Resolve 显示 **Auto / Video（Limited）/ Full**，默认 Auto 并说明实际范围；提交时冻结具体范围。RGB/浮点图像采用 Full，ProRes/DNxHR/v210 的当前 YCbCr 输出采用 Video（Limited）。不可用范围仍显示为禁用项。接口兼容 `full` / `limited` / `limit`，并接受 `auto` / `video`。ACEScct 视频没有可准确表达 AP1/ACEScct 的 H.273 标记，采用未指定原色/传递函数并写入原生 comment 声明；调色软件中需显式指定解释。超出整数编码范围的对数值会报错，使用 float EXR 可以保留。

当前 ACES 显示输出固定使用 SDR 100 cd/m² 或 HDR 1000 cd/m²；亮度独立显示并写入 `color_encoding.target_display_peak_nits`。这是所选输出变换的目标显示参数，不能解读为 PQ 或 HLG 标准本身的亮度上限。ACEScg / ACES2065-1 线性母版的该字段为 null，不烘焙显示变换。选项定义参考 [ACES 输出变换参数](https://docs.acescentral.com/system-components/output-transforms/parameters/)；HLG 显示依赖见 [ITU-R BT.2100](https://www.itu.int/rec/R-REC-BT.2100)。

网页要求当前服务提供规范色彩能力；旧服务显示更新提示，不再回退到混合菜单。旧 `color_output` 接口仍兼容现有脚本。

## 场景约定

场景入口：

    fn shade(ctx: SceneContext) -> vec4<f32> {
        let brightness = 0.18 * exp2(ctx.params.exposure);
        return vec4<f32>(brightness, brightness, brightness, 1.0);
    }

返回直通 Alpha 的线性 ACEScg RGBA。Alpha 必须为 0..1；RGB 可为负数或大于 1。工具验证每个采样值，禁止 NaN / infinity。参数由 SceneSpec.parameters 定义，使用 ctx.params.NAME 访问。

颜色参数默认通过 OKLCH 明度 / 彩度选色板和色相滑块选取，显示原颜色与新颜色。OKLCH 是适合当前 SDR 选色的 D65 感知空间，不模拟 HDR 观看条件。色样通过实际 ACES 2.0 sRGB 预览生成；选取颜色经同一视图的 OCIO 逆变换换算为参数声明空间，再正向渲染核对。超出 sRGB 色域时，保持明度与色相并降低彩度，界面用斜线和文字说明。打开、取消或 Escape 不会改写原值；“应用颜色”只更新本地草稿，仍需保存场景。高级 RGB 输入保留负值和大于 1 的浮点值。转换失败可重试并保留候选颜色。

同一选色能力通过 `chromascript.resolve_color` MCP 工具和 CLI 提供；`request.rgb` 用于只读检查原值，`request.oklch` 用于换算选色，两者只能提供一个。示例：

    .venv/bin/chromascript call resolve_color --arguments '{"request":{"color_space":"ACEScg","oklch":{"lightness":0.65,"chroma":0.15,"hue":270}}}'

返回 `rgb` 为可写入场景的浮点值，`display_rgb` 为独立显示色样，另含实际 OKLCH、色域调整、往返误差及 OCIO 配置校验值。模型与矩阵依据 [Oklab 原始说明](https://bottosson.github.io/posts/oklab/)；坐标和 D65 语义见 [CSS Color 4](https://www.w3.org/TR/css-color-4/#ok-lab)。

ctx.pixel 为左上原点的全局像素坐标；ctx.uv 为左下原点的归一化坐标。ctx.time 来自精确有理数帧率，送入 GPU 的时间为 float32；ctx.frame、ctx.seed 为整数。场景不能依赖真实时钟或上一帧。

采样在线性空间对预乘颜色逐级成对平均，最后恢复直通 Alpha；减半操作避免 GPU 快速数学重排造成有限大数的中间和溢出。float32 舍入和下溢限制仍然适用。无 Alpha 输出在线性空间合成到黑色后执行输出变换。PNG 使用直通 Alpha；EXR/TIFF/支持的专业视频提供明确的关联方式。

    .venv/bin/chromascript preview <scene-id> --frame 60
    .venv/bin/chromascript render <scene-id> --container exr --bit-depth 32 --compression zip --alpha-mode straight --width 3840 --height 2160 --frames 300
    .venv/bin/chromascript render <scene-id> --container mp4 --codec hevc --color-output PQ --width 3840 --height 2160 --frames 300
    .venv/bin/chromascript render <scene-id> --container mov --codec hevc --color-space Rec.2020 --gamma st2084 --width 3840 --height 2160 --frames 300
    .venv/bin/chromascript resume <job-id>
    .venv/bin/chromascript inspect <artifact-id>

## 色彩与交付

固定使用 OpenColorIO 2.5.0、ACES 2.0 CG Config v4.0.0，保存配置校验值、场景版本、依赖版本和数值实现指纹。

- EXR：ACEScg 或 ACES2065-1；half / float，ZIP / PIZ；正式 ACES2065-1 存档为无压缩 half。保留有效负值与高光，half 溢出明确报错。
- SDR：ACES 2.0 100 nit Rec.709 输出视图、Rec.1886 显示编码。
- PQ：ACES 2.0 1000 nit、Rec.2020 限制色域输出。
- HLG：使用同一 HDR 画面呈现结果，通过 OCIO 官方 1000 nit HLG 显示变换编码。
- sRGB 预览与原始 ACES 浮点数值分别提供；Alpha 不进入颜色处理器。长边超过 1280 的存储预览先解码到显示线性光，以预乘 Alpha 缩小，再恢复直通、编码并量化；不重复应用 ACES 呈现。
- 工具空间转换和显示输出变换不同：ACEScct 等工作空间转换不应用显示呈现；sRGB / Rec.709 / PQ / HLG 别名使用明确的 ACES 输出视图。

交付面板由用户通过联动下拉列表选择封装、编码、档位、位深、色度采样、输出色彩空间、Gamma、Alpha、范围、压缩和质量控制，覆盖 MP4/MOV H.264、HEVC、全部六档 ProRes、DNxHR MOV/MXF、EXR、PNG/TIFF/DPX、FFV1、无压缩 RGB/YUV 及平台构图。可添加多个独立交付目标。更改选项会更新其他可选项，并提示兼容性调整；任务提交前会再次校验。旧预设接口仅为已有脚本及历史任务保留，网页不再使用预设。

色彩空间与 Gamma 分开指定：`Rec.709` 搭配 `gamma2.4`（Rec.1886）或 `srgb`；`Rec.2020` 搭配 `st2084`（PQ）或 `hlg`；`ACEScg` / `ACES2065-1` 搭配 `linear`。选择 Rec.2020 HDR 会联动到兼容的位深。MCP / HTTP 的 `DeliveryConfiguration` 与 CLI 的 `--color-space`、`--gamma` 使用同一约束；旧 `color_output` / `--color-output` 仍兼容，不能与矛盾的新字段一起提交。

每项能力记录实际已验证组合；编码器存在不等于整个规格已验证。交付检查对照实际解码档位、RGB/YCbCr 类型、色度采样和所需通道，不仅检查最低位深；4:2:2 文件不能通过 4:4:4 规格检查。ProRes / DNxHR 软件编码输入最高 10-bit，解码器显示 12-bit 存储不代表端到端 12-bit。自定义预设也会检查输入像素格式精度。

submit_configured_delivery 直接接受 configurations 数组，无需保存预设。get_delivery_options 返回合法选项、联动调整和允许帧率；validate_delivery_configuration 与 estimate_delivery 分别校验和估算当前配置。项目默认色彩和旧批量接口保留兼容，显式交付配置决定本次输出。

文件本身包含标准色彩元数据：EXR 写入 AP1/AP0 `chromaticities` 与 `colorInteropID`；PNG 写入 `sRGB` / `gAMA` / `cHRM`；TIFF 嵌入真实 sRGB ICC 配置；DPX 写入 Rec.709 色度定义、用户定义传递函数和数值 Gamma 2.4。PNG/TIFF 交付限定 sRGB，DPX 限定 Rec.709 / Gamma 2.4，避免原生写入器静默采用错误默认值。检查会回读实际字段，缺失或不匹配时交付失败。

MOV/MP4 写入标准 `colr`（NCLC/NCLX）标记，并与编码器的色彩标记一起检查。依照 [Apple 规范](https://developer.apple.com/documentation/quicktime-file-format/color_parameter_atom)，存在 `colr` 时读取器应忽略 `gama`，因此不再同时写入两者。MOV 的 Gamma 2.4/2.6 使用未指定传递函数（代码 2），另以原生 comment 和报告记录色域、Gamma 及范围；这些声明不构成自动识别认证，须在导入端手动核对。MP4 等格式的 Rec.709 沿用 BT.709 信号约定，同样需核对 Gamma 2.4。Gamma 2.2 / PQ / HLG / sRGB 分别使用传递函数代码 4 / 16 / 18 / 13。

图像序列附带 sequence.json，记录精确帧率、帧索引、时间码起点及独立的 `color_encoding.color_space` / `gamma`；EXR 也写入标准有理数 FramesPerSecond 元数据。报告记录 Resolve 输入空间/Gamma 建议和实际原生检查结果；应用内导入没有自动认证，`resolve_import_verified` 为 false。不同 Resolve 版本和项目色彩管理对图像元数据的自动解释不同，导入后按[说明](docs/RESOLVE_IMPORT.md)核对输入设置和时间码。

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
