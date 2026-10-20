# ChromaScript

本地运行的 ACES 2.0 程序化视频工作台，通过网页、MCP 或 CLI 创建画面、预览动画并导出视频与图像序列。

## 功能介绍

- **场景创作**：从模板开始，用 JSON 和 WGSL 定义画面，调整参数、颜色与关键帧。
- **HDR / SDR 动画代码**：同时提供浮点 Three.js、SDR React 文字和 ThreeCanvas 模板；统一预览，按源类型导出 HDR/SDR 视频或浮点母版。
- **实时预览**：播放、暂停和逐帧查看动画，检查原始像素值。
- **大屏查看**：场景与动画预览均可放大至网页视口，保留播放位置和监听状态；点击“退出大屏”或按 Esc 返回。实时 WGSL 与浮点动画采用最高 1280×720（竖屏 720×1280）的预览尺寸，小画面不放大渲染，交付仍使用原规格。
- **色彩管理**：WGSL 生成与母版处理使用 float32 线性 ACEScg（AP1）；Three.js 动画可原生使用线性 Rec.709、线性 BT.2020、ACEScg/AP1 或 ACES2065-1/AP0，再线性转换为 ACEScg，网页自动选择低分辨率 HDR 视频或 sRGB 预览，输出色彩空间在交付时选择；统一通过 OCIO / ACES 2.0 转换，原始浮点数据与显示预览分别保留。
- **多格式交付**：导出 EXR、PNG、TIFF、DPX 图像序列，以及 H.264、HEVC、ProRes、DNxHR 等视频；可用组合由本机能力决定。
- **项目音频**：导入本地音频，或用代码生成音调、节奏与音效；设置音轨裁切、起点和音量，随视频混音或作为独立 WAV 交付。
- **任务管理**：查看渲染进度、下载成果，恢复符合条件的中断任务。
- **模型调用**：所有产品操作均提供 MCP 工具和 CLI 调用。场景示例见 [examples](examples/README.md)。

## 音频制作与 MCP

“工作区”和“动画代码”均提供 **项目音频**：导入文件或生成音效，试听素材，点击“添加到音轨”，设置时间线起点、素材起点、播放时长及增益，再保存音轨。音轨用于该项目所有场景的新交付任务；保存后，网页实时场景与动画预览会随播放、暂停、跳转和循环同步播放音轨；首次需用户点击播放。Agent 保存的音轨约每 2 秒自动同步，可直接拖动预览音量条调整 0–100% 音量，0% 即静音，仅影响监听、不改变音轨或导出增益。浏览器预览用于监听，不替代交付的精确样本验证。

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

约束时间相对于音乐素材，编曲时从场景时间中减去预定的音轨起点；窗口含起点、不含终点。服务校验乐谱时间与落点，不分析画面或认证听感；阅读窗口不检查音源释放尾音。MIDI 时间使用每秒 48000 ticks，但 FluidSynth 内部调度仍有时间量化，音色本身也可能缓慢起音，需试听。音符时长指按键时间，应在总时长内给释放尾音留余量；最后按指定总时长裁切并淡出。当前固定关闭混响/合唱、合成器增益 0.2、256 发声数、单 CPU 核心、无延音踏板，原生渲染超时为 300 秒。

返回的 `generation_report` 同时保存在 `asset.metadata.generation_report`，记录实际样本数、时长、采样率、声道、PCM 样本峰值/dBFS、饱和前峰值、削波声道样本数、WAV 哈希、音源快照哈希与预设、完整乐谱及约束、落点检查、FluidSynth 版本/选项/输出探测和实现哈希。输出增益与淡出在浮点域完成后才转换为 32-bit 整数 PCM，不自动归一化。静音的 `peak_dbfs` 为 null；未进行 LUFS 或真峰值分析。保留原音源与乐谱以便重复生成，不保证跨 FluidSynth 版本或平台逐位一致。分轨、片段预览、公开 MIDI 导入/导出和多版本自动试配留待后续扩展。

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

结果直接反馈给调用 agent：`report.objective` 是源片段的实测样本峰值、每声道 RMS 和满幅样本计数；`report.feedback` 是模型的总结、优点、问题、建议和不确定性。满幅样本数不等于已证实削波。问题时间是片段内近似秒数，加上 `objective.start_seconds` 才是素材时间。`feedback_status="complete"` 仅表示结构有效；截断、无效 JSON、越界时间或模型切换会返回 `invalid`、原始回答和原因。网络或模型拒绝请求则明确报错，不伪造听审结果。模型意见可能出错，agent 应结合客观结果和制作要求判断；不会自动修改音符、覆盖素材或替换音轨。

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

## 色彩工作流程

“动画代码”使用 `Three.js RGBA32F → 浮点读取 → OCIO → ACEScg EXR`，母版不烘焙显示变换。预览使用同一套 ACES 2.0 SDR sRGB 变换；HDR 视频使用 ACES 1000-nit PQ/HLG 输出变换，可选 HEVC Main10 或 Apple ProRes 422 HQ（MOV、10-bit 4:2:2）。GPU 会实际测试负值、超过 1 的高光和 float32 读回；无法满足时明确失败。同时支持 `remotion-sdr-v1`：React/DOM/SVG/ThreeCanvas → sRGB 截图 → SDR 视频。历史未标记引擎的动画按 SDR 打开，源码与修订记录不变。

`WGSL 代码 → 线性 ACEScg 生成与处理 → ACES 变换 → sRGB 网页预览 / 用户选择的交付空间`

`shade(ctx)` 返回非预乘的线性 ACEScg RGBA：RGB 可为负值或大于 1，Alpha 为 0–1，始终不参与色彩转换。代码中的 RGB 常量按 ACEScg 解释，不需要另选输入色彩空间或在代码中烘焙显示变换。

颜色参数的 `color_space` 仅声明参数的存储编码（ACEScg 或 sRGB）；参数进入 `ctx.params` 前统一转换为线性 ACEScg，不会改变整个场景的生成空间。网页预览固定使用 ACES 2.0 sRGB SDR 显示变换，不随交付设置改变，也不替代 HDR 校准监看。

在交付面板分别选择输出色彩空间与 Gamma。显示交付应用一次 ACES 输出变换；ACEScg、ACES2065-1 或 ACEScct 场景编码仅做色彩空间转换，不烘焙显示效果。可用组合仍受封装、编码器与位深限制。

## 调色母版与完整色彩标签

动画的“视频输出”支持 **Apple ProRes 422 HQ · MOV**：浮点源可选 sRGB、PQ 或 HLG，SDR 网页源可选 sRGB。支持仅视频及 EXR 母版附带视频；视频合成到黑底，不携带 Alpha。需要本机 `prores_ks` 编码器，导出后检查实际 codec、HQ profile、位深、采样、帧率与原生色彩标签。SDR 网页截图仍为 8-bit 源，不因 ProRes 编码而增加源精度。CLI/MCP 的 `submit_motion_render` 使用 `output.video=prores-hq-srgb|prores-hq-pq|prores-hq-hlg`，通过 `get_motion_delivery_options` 查询当前源的可用选项。ProRes 视频空间按 `prores_ks` HQ 的压缩宏块目标码率计算，按帧数累积并加 20% 余量及封装开销；不再用未压缩帧体积预算压缩视频。3840×2160、60 fps、9 分 20 秒的仅视频预算约 145 GiB，实际文件随画面复杂度变化；不会为调整预算而降低编码质量。

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

所有新视频必须使用能准确标记色域和传递函数的组合。默认 SDR 为 Rec.709 色域 / sRGB，HDR 为 Rec.2020 / PQ 或 HLG；通用交付也保留受支持的 P3-D65 和 Gamma 2.2。纯 Gamma 2.4/2.6 及 AP1/ACEScct 没有精确的标准视频标签，不能通过备注或错误套用 BT.709 标签发布；这些旧视频组合禁用，EXR 与有原生数字 Gamma 字段的 DPX 不受影响。回读验证码流的 primaries、transfer、matrix、range，MOV/MP4 还核对实际 colr 字段；报告、CLI 和 MCP 使用同一规则。

HLG 高饱和高光可能产生大于 1 的编码值。Limited YCbCr 交付以 32-bit float RGB 送入矩阵／范围转换，保留窄范围允许的超白，不在 RGB 阶段裁切；Alpha 独立处理。Full／RGB 整数输出遇到无法表示的 HLG 超白会明确拒绝，请改用 Limited YCbCr；超出窄范围实际码位的信号同样拒绝。HDR 4:2:0 按 BT.2100 使用 top-left 色度共址，并核对原生位置标记。视频交付还须完整严格解码，解码错误即使不改变帧数也不能通过验证；报告以 `decode_verified` 单独记录结果。

Three.js 接受 sRGB 编码颜色（如 Hex/CSS），转换后在默认线性 Rec.709/D65 中计算；线性并不等于 SDR 或 0–1。当前宿主固定这一工作空间再由 OCIO 转为 ACEScg，禁止源码修改全局工作空间而破坏解释。其他色域/Log/PQ 输入需先正确转换，不能只改标签。

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

制作动画时，点击顶部“动画代码”，选择 SDR 网页或浮点渲染类型，点击“新建空白动画”；“示例模板（可选）”中保留文字、几何、光能等起点。修改 TypeScript 后自动预览；保存后选择“保留 EXR 母版”或“仅视频”。保留母版时可选 16-bit Half 或 32-bit Float，并可另输出视频；仅视频时选择 PQ、HLG 或 sRGB，点击“导出动画”。EXR 保留直通 Alpha，视频在场景线性空间合成黑底。视频要求偶数宽高及相应本机编码器（HDR 用 libx265，sRGB 用 libx264）；EXR 可使用奇数尺寸。浏览器「预览显示」默认自动 HDR：线性 BT.2020/AP1/AP0 源在报告 HDR 视频能力且支持实际 HEVC 配置的设备上，以最高 1280×720（竖屏 720×1280）、项目原始帧率 的 PQ/HEVC 10-bit 分段视频预览；其他情况回退 sRGB。可显式选择 sRGB SDR。点击播放开始，暂停/隐藏停止继续生成片段；拖动时间线重建缓冲。编码与解码需要短暂缓冲，不保证实时帧率；自动化测试不能证明实体屏幕 HDR 亮度。

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

新增“仅视频”模式使用 `output={"master_bit_depth":null,"video":"pq","render_scale":1}`（视频也可选 `hlg` / `srgb`），通过同一 `submit_motion_render` / `estimate_motion_render` 操作在界面、CLI 和 MCP 使用。每帧浮点读取、线性采样/Alpha 处理与 ACES 输出变换后直接送入 FFmpeg，不写 EXR，也不二次渲染。导出盘仅保留一帧临时 `.f32` 传输文件，消费后删除；成果仍附源码、规格、缩略图和验证报告。默认母版模式保持不变。仅视频不能同时设置 `video="none"`，中断后的显式重试从头开始；已原子发布的成果可直接恢复完成状态。

仅视频空间预算包含单帧中间数据、音轨、辅助文件以及压缩视频估算；规划码率为 `max(8 Mbps, 宽×高×fps×0.2)`，按精确有理帧率算时长，再加 20% 与封装余量。规划码率**不是编码器限制**，原有 CRF/质量不变，不能保证实际文件大小；运行时持续检查磁盘余量。4K UHD 60fps、10 分钟、1×1 无音轨的估算约 8.56 GiB，而不是按整段未压缩 EXR 计算。

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

SDR 模板使用 `engine=remotion-sdr-v1`，默认导出 React 组件。允许原有 React、Remotion、ThreeCanvas 与 Three.js 导入。`get_motion_delivery_options(engine)` 返回源类型对应的选项；SDR 的 `output` 为 `{"master_bit_depth":null,"video":"srgb"}` 。`rec709`（纯 Gamma 2.4）因缺少精确标准视频标签而禁用。浏览器 SDR 源不会被重新标记为 HDR 或 ACEScg 母版。

不需要通过模板开始：`create_scene` 仅传 `project_id` 即创建空白 WGSL 场景；`create_motion_scene` 仅传 `project_id` 创建空白浮点动画；显式传入 `engine: "remotion-sdr-v1"` 创建 SDR 网页动画。两者均可直接传入 `spec`，也可在创建后使用修订号通过 MCP/CLI 修改源码。空白场景只含可运行的最小透明画面代码，没有示例图形或动画；项目用于归属与历史管理，模板不构成依赖。

HDR 预览通过共享 `encode_motion_hdr_preview(chunk)` 操作在本机编码，HTTP、CLI 和 MCP 使用同一严格模型。输入可为原始线性 RGBA32F 的 Base64 帧，或 GPU 已经显示转换的 P010 PQ。每片段最多 64 帧且不超过 192 MiB，偶数尺寸最高 1280×720（或竖屏 720×1280），保留项目精确有理数帧率。P010 输入不会再执行色调映射。宿主将线性像素转为 ACEScg、合成黑底并应用一次 ACES 2.0 1000-nit PQ 输出变换；验证实际解码属性和 MP4 色彩标签后返回临时 fMP4。无后台导出任务，不保存片段，不修改母版/草稿。`compile_motion_preview(spec, hdr_preview=true)` 启用 720p GPU P010 采集协议；外层播放器负责编码调用和 MSE，源码 iframe 保持 opaque origin、禁止网络和媒体。默认编译调用仍返回独立可用的 sRGB 预览。

Safari 26.2 的 `video-dynamic-range` 查询可能尚未实现。自动 HDR 在 high/standard 均不匹配时改用 `dynamic-range: high`，再核对 HEVC/MSE 以及实际 PQ 编码；已明确报告 SDR 视频平面的设备仍回退 sRGB。

720p HDR 预览通过 transferable buffer 和鉴权二进制接口减少像素复制，优先使用通过实际回读验证的 VideoToolbox Main10；软件回退仍保留原帧率。采集下一片段与编码重叠，播放前缓冲约 2 秒，暂停或跳转只生成一帧清晰 HDR 图像。二进制地址为 `/api/operations/encode_motion_hdr_preview/binary`，`X-Chroma-HDR` 为规格 JSON（含 `frame_count`、`input_format` 和原帧率）；与 MCP/CLI 的 Base64 操作共用处理规则。
