# Resolve 手动导入验收

当前新视频交付要求完整且精确的原生色域及传递函数标签。下文旧 MOV Gamma 2.4/2.6、Rec.709 Gamma 2.4 及 ACEScct 视频声明说明仅用于解释历史文件，不再是可提交的交付组合。新交付可使用受支持的 sRGB、Gamma 2.2、PQ/HLG；场景参考调色母版继续使用 ACEScg EXR。备注或 sidecar 不能替代原生视频标签。缺失或不符的码流标签及 MOV/MP4 colr 会阻止发布。

按照你的选择，Resolve 应用内导入由你自行完成。下面是工具输出的预期解释；应用内检查尚未执行，也未测量 HDR 显示器。

## 文件中的色彩空间与 Gamma

交付规格对照本机 DaVinci Resolve 21.0.3 附带的《DaVinci Resolve 21 Reference Manual》（July 2026）：第 4197–4199 页的 Format / Codec / Type，第 4201 页的 Auto / Video / Full 数据级别，以及第 4202 页的 Color Space Tag / Gamma Tag。官方[编码表（Resolve 21.1，September 2026）](https://documents.blackmagicdesign.com/SupportNotes/DaVinci_Resolve_21_Supported_Codec_List.pdf)用于对照格式名称、ProRes 类型和精度语义；可用组合仍取本机 FFmpeg、OCIO 和写入器实际支持且能回读验证的子集，不能把 Resolve 的 12-bit 编码能力套在本机 10-bit 软件输入上。

交付面板分别设置“输出色彩空间”和“Gamma”，选择实际像素变换。“色彩空间标签”和“Gamma 标签”为与输出一致的只读文件标记；标签不会再对像素应用一次变换。P3-D65 是色域，不能等同于 Display P3：P3-D65 + sRGB 才组成 Display P3，Blackmagic 的[官方 Colorist Guide（第 172 页）](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf)明确说明这一组合。ProRes 是编码格式，本身不固定 Gamma。切换色域会保留兼容的 Gamma 2.4；P3-D65 也提供 Gamma 2.2、Gamma 2.6 和 PQ。

当前 P3 Gamma 2.4/2.6、Rec.709 Gamma 2.6 保留 MOV 声明流程，不能冒用其他传递函数代码。MOV Gamma 2.4/2.6 的 colr 传递函数为未指定，原生 comment 记录色域、Gamma 和范围；导入时须手动指定，不宣称自动识别。依据 [Apple 规范](https://developer.apple.com/documentation/quicktime-file-format/color_parameter_atom)，colr 会覆盖 gama，因此不再写入 gama。Gamma 2.2 使用原生 H.273 transfer=4。其他容器中尚未支持的组合仍为禁用项，并由 API / MCP / CLI 拒绝。

| 输出设置 | 文件中的实际标记 | Resolve 输入解释 |
|---|---|---|
| ACEScg / Linear | EXR AP1/D60 chromaticities、colorInteropID=lin_ap1_scene | ACEScg，Linear |
| ACES2065-1 / Linear | EXR AP0/D60 chromaticities、colorInteropID=lin_ap0_scene；无压缩 half 存档另带 ACES 容器旗标 | ACES2065-1，Linear |
| Rec.709 / Gamma 2.4 | MOV colr=1-2-1 加 comment 声明，须手动解释；DPX Rec.709 色度、用户定义 TRC、电视头 Gamma=2.4 | Rec.709，Gamma 2.4 |
| Rec.709 / sRGB | 视频 colr=1-13-1；PNG sRGB/gAMA/cHRM；TIFF sRGB ICC | sRGB |
| Rec.709 / Gamma 2.2 | 视频 colr=1-4-1 | Rec.709，Gamma 2.2 |
| Rec.709 / Gamma 2.6 | MOV colr=1-2-1 加 comment 声明 | Rec.709，Gamma 2.6，核对后手动解释 |
| P3-D65 / Gamma 2.2 | 视频 colr=12-4-1 | P3-D65，Gamma 2.2 |
| P3-D65 / Gamma 2.4 | MOV colr=12-2-1 加 comment 声明 | P3-D65，Gamma 2.4，核对后手动解释 |
| P3-D65 / Gamma 2.6 | MOV colr=12-2-1 加 comment 声明 | P3-D65，Gamma 2.6，核对后手动解释 |
| P3-D65 / sRGB（Display P3） | 视频 colr=12-13-1 | P3-D65，sRGB |
| P3-D65 / ST2084 | 视频 colr=12-16-1 | P3-D65，ST2084 |
| Rec.2020 / ST.2084 | 视频 colr=9-16-9 | Rec.2020，ST2084 |
| Rec.2020 / HLG | 视频 colr=9-18-9 | Rec.2020，HLG |

表中的视频矩阵代码适用于 YCbCr；RGB 视频的矩阵代码为 0。NCLC 无精确的纯 Gamma 2.4/2.6 枚举，MOV 的 comment 仅记录创作意图，不能代替标准 TRC 或应用内解释；MP4/MXF/MKV 的 Rec.709 仍使用 BT.709 信号约定，不能把它当成对 Rec.1886 的唯一证明，应明确核对 Gamma 2.4。NCLC 不携带独立范围位，范围仍须按编码约定及报告核对。

“数据级别”默认 Auto；界面同时说明实际解析为 Video（Limited）或 Full。提交时即冻结为具体范围，不在工作进程中重新猜测。RGB/图像序列使用 Full；ProRes/DNxHR/v210 的当前实现使用 Video，无法可靠自描述的 Full 覆盖仍禁用并注明原因。H.264/HEVC 可显式选择已支持的 Full/Video。旧 full/limited/limit 接口值继续兼容。

纯 Gamma 的改变只重新编码同一幅显示线性图像，ACES 呈现只执行一次，Alpha 不进入 OCIO。当前 SDR 仍使用 100 nit ACES 2.0 视图；选择 Gamma 2.6 不会自动变成 DCI 白点、48 nit 院线视图或 DCP。HDR 使用实际已实现的 1000 nit 视图，图像预览仍为独立 sRGB 显示。

Blackmagic 的[官方色彩管理指南，第 113 页起](https://documents.blackmagicdesign.com/SupportNotes/DaVinci_Resolve_17_New_Features_Guide.pdf)说明 Resolve 读取 QuickTime NCLC、MXF 元数据及 ACES XML 信息。标准字段的存在不等于所有版本都能自动分配图像序列的输入空间；尤其不要把自定义 `chromascript:*` 字段或普通 `sequence.json` 当作 Resolve 自动读取的格式。项目启用 DaVinci YRGB Color Managed 后，检查媒体池输入色彩空间；必要时按表指定。EXR 的 AP1 数据绝不能为了“自动识别”而假标 AP0。

`verification.json` 记录文件回读结果与 `color_encoding`，其中 `resolve_input_color_space` / `resolve_input_gamma` 提供明确输入建议。`resolve_import_verified=false` 表示尚未在 Resolve 应用内验收。MOV/MP4 还记录实际 `container_color` 的数值，检查器同时验证容器字段与解码流字段，并对照实际档位、色度采样和通道；comment 声明用于检查所记录的意图，不作为自动导入认证。

## 重新生成 4K 60 fps 样片

原测试样片已于 2026-10-12 按要求清理。以下为重新生成后的规格约定；新成果位置通过 `get_artifact.local_path` 或网页序列导出获取。

| 文件或目录 | 输入解释 | 数据范围 / Alpha |
|---|---|---|
| `exr-16-zip/00000000.exr` 至 `00000299.exr` | ACEScg，AP1，场景线性，无显示变换 | 浮点；直通 Alpha |
| `prores-4.mov` | 已呈现的 Rec.709 / Rec.1886 SDR | YCbCr 合法范围；直通 Alpha |
| `h264-review.mp4` | 已呈现的 Rec.709 / Rec.1886 SDR | YCbCr 合法范围；无 Alpha |
| `hdr-pq.mp4` | Rec.2020 / ST.2084 PQ，ACES 1000 nit 输出 | 10-bit YCbCr 合法范围；无 Alpha |

统一尺寸 3840 × 2160，60/1 fps，300 帧，时长 5 秒。起始时间码 `00:00:00:00`，末帧 `00:00:04:59`。无音频。EXR 的 `sequence.json` 保存精确帧率、时间码及色彩说明；`verification.json` 保存原生文件检查结果。

ProRes 4444 的解码存储可报告 12-bit，本机软件编码链路的有效输入为 10-bit。报告分别列出两个值。

## 导入检查

1. 新建用于检查的项目，明确时间线为 3840 × 2160、60 fps。导入 EXR 时作为图像序列读取，核对 300 帧和 5 秒。
2. 明确把 EXR 指定为 ACEScg 场景线性输入，在项目的色彩管理中选择目标显示输出。不要把该母版作为 sRGB 或已呈现的 Rec.709 读取。
3. ProRes/H.264 已烘焙 SDR 输出变换，作为 Rec.709 / Rec.1886 显示素材解释；PQ 文件作为 Rec.2020 / ST.2084 显示素材解释。检查文件时避免再把这些文件当作 ACEScg 场景源套用相同输出变换。
4. 检查尺寸、帧率、首末帧、时间码、数据范围和解码规格。若自动解释与表格不同，明确指定输入设置再比较。
5. 有 Alpha 的素材按直通（Straight / Unassociated）解释。主渐变样片 Alpha 为 1，适合检查通道存在；真正的透明度测试由 Python 原生编码/解码测试覆盖，原透明边缘样片已清理；可使用“几何与透明度”模板重新生成 1920 × 1080、24 fps、24 帧的 ProRes 4444、EXR、PNG 16-bit 和 TIFF 16-bit。
6. HDR 记录实际显示器、连接方式、显示模式及是否经过校准。文件标记检查通过，不能替代显示端测量。

默认中性灰网页只提供 sRGB 预览。ACES 版本、显示视图和显示管理不同可能产生不同观看结果；比较前先对齐实际变换。

Blackmagic 的[官方色彩管理说明](https://www.blackmagicdesign.com/products/davinciresolve/color)介绍输入、时间线和输出变换设置。本文不声称已在本机 Resolve 中验证对应菜单或导入结果。

## 其他格式

原 40 个预设、8K 静帧及 120 fps 短片的测试产物均已清理。历史结论保留在 [验收报告](../reports/VALIDATION.md)；需要素材时，可运行 `scripts/acceptance.py --formats --high` 重新生成。

整数图像序列统一带有 `sequence.json`，并在原生文件头写入上表所列的色彩信息。帧率、时间码与自定义信息的读取仍取决于软件，应依据 sidecar 核对。TIFF 的 ICC 标记不改变 Alpha 关联；直通与预乘按标准 ExtraSamples 字段解释。
