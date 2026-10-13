# ChromaScript 数学逻辑与色彩科学审查

审查日期：2026-10-13，Asia/Taipei。代码基线：`ecae6abe6e2d7d1a3b7ccd108f6abf540b4d78a3`。

修复状态：本报告的四项发现已在后续工作区修改中处理，修复和针对性验证见文末“修复记录”。以下审查正文保留发现时的代码基线与复现证据，不表示当前实现仍有这些缺陷。

核心色彩架构合理：以 ACEScg 线性浮点为母版，区分工作空间转换、ACES 呈现和显示编码，Alpha 独立处理；AP1/AP0 矩阵、ACEScct 曲线、1000 nit PQ/HLG 链路及 SDR OKLCH 选色通过了本次核对。确认了四项问题：MOV Gamma 标记的互操作假设、非线性空间缩略图滤波、交付检查遗漏档位和色度采样，以及极端有限浮点值的采样累积溢出。前三项影响常规审阅或交付可信度，最后一项是低优先级数值边界问题。

初次审查仅新增报告和隔离的复现产物；后续按用户请求实施修复。结论范围为项目自身的数学和色彩处理，不包含 OCIO 内部 ACES 2 算法的逐行重实现认证、Resolve 应用内导入认证或显示器测量。

## 已确认的问题

### P1 MOV 的 Gamma 表达依赖会被标准读取器忽略的 gama

位置：`src/chromascript/encoding.py:35`、`:189`；关联 `check_probe()` 对 `gama` 的必需检查，以及 `src/chromascript/color.py:290` 的手动解释标志。

`_video_tags()` 将 MOV Gamma 2.4/2.6 的传递特性设为 unspecified；编码命令同时启用 `write_colr` 和 `write_gama`。当前检查器把两个字段都存在当成成功条件。实际生成的 Rec.709 ProRes MOV 包含 `nclc=1-2-1` 与 `gamma=2.399993896484375`，符合当前测试的期望。

但 Apple 的 QuickTime 规范明确规定：`colr` 取代 `gama`，写入器不应同时写两者，存在 `colr` 时读取器应忽略 `gama`。因此，符合该规则的读取器看到的是未指定传递函数，不能依据这个组合恢复 Gamma 2.4/2.6。记录数值字段存在，不能证明文件已准确自描述。[Apple 色彩参数原子规范](https://developer.apple.com/documentation/quicktime-file-format/color_parameter_atom)

影响：Rec.709 Gamma 2.4 MOV、P3 Gamma 2.4/2.6 MOV 等依赖导入端的默认值或人工设置；默认 ProRes 也受影响。尚未实测 Resolve 是否采用额外兼容逻辑，因此这里的结论是“标准互操作保证不成立”，不是“所有播放器一定显示错误”。Gamma 2.2 有独立的 transfer=4，风险不能与 unspecified 曲线完全等同。

建议：停止将“双字段存在”作为 Gamma 可移植性认证；明确提示需要手动解释的组合，修正 README、Resolve 导入说明和能力矩阵中的确定性表述。后续若保留针对特定软件的兼容写法，需要记录软件版本和实际输入解释证据。不要简单改用一个不匹配像素语义的传递函数代码。

### P2 大图缩略图在 sRGB 编码值上滤波

位置：`src/chromascript/worker.py:181` 至 `:185`。

长边超过 1280 时，worker 先执行 ACES sRGB 输出变换并量化到 8-bit，再用 Pillow `thumbnail()` 缩小。ICC 附加操作没有执行 EOTF 解码；滤波平均的是非线性的 sRGB 代码值。

复现了相同 Pillow 路径：2560×16 的不透明黑白相间像素缩成 1280×8，内部像素为 `128/255 = 0.5019608`。等面积黑白的平均显示线性光应为 0.5，再经 sRGB 编码得到 `0.735357`，约为 188/255。该差异由独立传递函数推导，不是观察者评分或显示器实测。[sRGB 转换公式](https://www.w3.org/TR/css-color-4/#color-conversion-code)

影响：高频网格、细线、粒子与高光边缘在提供给用户或模型的预览中偏暗；同一张图只因超过缩略图阈值就采用不同的滤波语义。原始 EXR 母版不受影响。这里没有把 Pillow 的 RGBA 处理误判为“没有预乘 Alpha”：即使完全不透明，错误仍可复现。

建议：若目标是忠实缩小已经呈现的画面，应将 sRGB 解码为显示线性 RGB，按预乘 Alpha 滤波，恢复直通，再编码并最终量化；整个路径只执行一次 ACES 呈现。若产品选择先缩小场景线性母版再呈现，应将其视为另一种有明确语义的预览策略，因为非线性呈现与缩放不交换。新增黑白高频纹理和透明边缘的独立参考测试。

### P2 交付验证遗漏实际档位与色度采样

位置：`src/chromascript/encoding.py:430` 至 `:446`，以及 `check_probe()` 整体。

检查器核对编码器、尺寸、帧率、帧数、色彩标签、最低位深和 Alpha 是否存在，但没有对照实际 `profile`，也没有核对像素格式对应的色度水平/垂直降采样。

独立生成的文件实际是 ProRes Standard、`yuv422p10le`。将它交给检查器，期望规格设为 ProRes 4444、`yuv444p10le`、无 Alpha，结果仍为 `passed=true`、`issues=[]`。此复现使用真实文件的 ffprobe 输出，没有篡改实际探测字段。它证明验证器无法发现这类错误，不能据此推断所有当前正常交付都已发生格式降级。

影响：只要档位或采样出现编码端回退、配置错误或后续回归，报告仍可能宣称验证成功；4:4:4 与 4:2:2 的色彩空间分辨率差别无法被发现。这与项目“认证实际精确组合”的契约不一致。

建议：使用已有 `pixel_format_info()` 描述符比较色度采样、RGB/YCbCr 类型和通道；建立各编码器 profile 名称的明确对应关系。保留 ProRes 10-bit 输入、12-bit 解码存储的既有区分，不应简单要求两个像素格式字符串完全相等。补充实际 4:2:2 文件不能通过 4:4:4 验证的负例。

### P3 有限 float32 常量会因采样次数增加而溢出

位置：`src/chromascript/gpu.py:63` 至 `:68`。

当前公式先在 float32 中累加，再除以样本数。数学上的平均值可表示，不代表未归一化的中间和可表示。

在 Apple M3 Max / Metal 上实际运行返回恒定 `[1e38, 0, 0, 1]` 的 shader：1 次采样得到约 `9.99999968e37`；4 次和 16 次采样均报 `non_finite_pixels`。每个输入样本都有限且符合当前输入范围，结果却依赖采样设置。

影响局限于接近 float32 极值的输入，远超一般色彩素材范围，因此不列为常规 HDR 阻断项；这是可以独立修复的数值健壮性缺陷。

建议：采用有界的加权累积或显式定义受支持的渲染数值范围，并保留非有限数检测。验证时覆盖正负极值、混合符号、极小 Alpha 和所有采样数；不要通过关闭检测让溢出结果继续传播。GPU 对溢出、次正规数和运算精度的允许行为还应遵守 [WGSL 浮点规则](https://www.w3.org/TR/WGSL/#floating-point-evaluation)。

## 合理设置及数值依据

| 项目 | 判断与依据 |
| --- | --- |
| ACEScg 工作空间和 AP1/AP0 | 原色及白点与 ACES 规范一致；AP1 绿色 y=0.830，白点 xy=(0.32168, 0.33767)。从原色独立建立 RGB→XYZ 矩阵后计算 AP1→AP0，1024 个随机样本的最大绝对误差为 `1.643e-6`。负值和大于 1 的场景数值未被工作空间转换统一裁掉。[ACEScg 规范](https://docs.acescentral.com/encodings/acescg/) |
| ACEScct | 作为场景 AP1 对数编码处理，没有套用显示色调映射。独立分段公式覆盖负值、零、0.0078125 分界两侧、中灰和 65504，最大绝对误差为 `7.678e-7`。[ACEScct 规范](https://docs.acescentral.com/encodings/acescct/) |
| ACES 呈现与显示编码 | 当前调用结构将两者分开；P3 采用实际 P3 限制色域，Gamma 变更重新编码同一呈现结果，没有发现重复 ACES tone mapping。官方 ACES 2 使用由 Hellwig 2022 简化而来的 JMh 中间空间，项目委托固定 OCIO 实现符合这种架构。[ACES 输出结构](https://docs.acescentral.com/system-components/output-transforms/) |
| PQ 与 HLG | 将项目输出分别通过独立 PQ EOTF、HLG 逆 OETF 加 OOTF 解码。在 1000 nit、零黑位、HLG system gamma=1.2 的相同参考条件下，1024 个随机颜色最大显示光分量差为 `0.000759 cd/m²`。证明本次覆盖范围内 HLG 的 OOTF 放置和亮度缩放一致；不等于物理显示认证。[BT.2100-3 表 4、表 5](https://www.itu.int/dms_pubrec/itu-r/rec/bt/R-REC-BT.2100-3-202502-I!!PDF-E.pdf) |
| SDR 100 nit 与 HDR 1000 nit | 作为固定 ACES 输出视图的目标参数合理，不能推广为标准固有上限或素材实测 MaxCLL。P3-D65 Gamma 2.6/100 nit 也不能称作 DCI 白点/48 nit 影院输出。当前项目文档已作区分。[ACES 参数](https://docs.acescentral.com/system-components/output-transforms/parameters/) |
| Alpha 和空间采样 | 核心核函数相当于 `Ā=Σa/N`、`C̄=Σ(aC)/Σa`；在 Σa>0 时正确，避免透明样本的隐藏 RGB 污染。OCIO 只接收 RGB，无 Alpha 输出先在线性场景中合黑再呈现。这与覆盖率加权的合成代数一致。[Porter 与 Duff 1984，第 254 页](https://keithp.com/~keithp/porterduff/p253-porter.pdf) |
| OKLCH 选色 | 系数吻合 Ottosson 的 2021-01-25 版本；固定 L/h 降低 C 是明确的严格色域容纳策略，不必冒充 CSS 的另一种映射算法。额外 2000 个随机颜色全部通过，最大显示往返误差 `0.00059253`，低于既定 `0.5/255`。该阈值是代码值误差，不是感知色差 ΔE。[Oklab 原始说明](https://bottosson.github.io/posts/oklab/) |
| 时间与曝光 | 有理数时间基、整数帧索引和 `time=frame×denominator/numerator` 合理；曝光以 `exp2(stops)` 缩放线性值合理。GPU float32 时间仍有下文所述边界。 |
| 存储与数据级别 | 原生 EXR、PNG、TIFF、DPX 和视频回读测试通过；alpha 不进入色彩变换，量化发生在呈现后。合法/全范围测试及软件编码输入精度的区分合理，但验证结论须受前述 profile/采样遗漏限制。 |

矩阵核对使用独立的双精度构造：对于每个原色 xy，组成列向量 `(x/y, 1, (1-x-y)/y)`，解白点缩放系数得到归一化原色矩阵；再计算 `M_AP0⁻¹ M_AP1`。因此这项数值结果不是用同一 OCIO 处理器和它自己比较。

## 应保留的边界与解释

**透明 PNG 预览不是合黑交付的亮度证明。** `preview.py:41` 对直通 RGB 做呈现，然后保留 alpha 给浏览器叠加背景；无 Alpha 交付则先合黑再呈现。若 `F` 为完整 ACES sRGB 输出变换，通常 `αF(C) ≠ F(αC)`。例如 `C=(0.18,0.18,0.18)`、`α=0.25`，直通预览 RGB 约为 0.349188；在编码 sRGB 中叠黑得到 0.087297，而项目实际线性合黑后呈现为 0.120062。此处黑底仅用于隔离数学差异，不是声称当前 UI 棋盘背景为黑。保留透明预览是合理的素材检查选择；若要承诺所见即所得的无 Alpha 交付，需要另提供线性背景合成的审阅模式。

**线性采样不等于没有混叠。** 当前为每像素 1、4 或 16 个规则空间样本，无时间积分，也没有可调快门。高速运动、细周期纹理仍可能闪烁。README 已标明实时预览只用一次采样，因此这不作为本次缺陷；`samples` 不应被解释为运动模糊或带限重建保证。

**有理数时间戳不等于无限精度 shader 时间。** `gpu.py:208` 最终把绝对秒数打包成 float32。例如允许的 75 fps、帧 9,999,998 和 9,999,999 都得到 `ctx.time=133333.3125`。这主要影响非常大的起始帧位置；整数 `ctx.frame` 在目前范围仍可精确表示。已有文档披露 float32 时间限制，可进一步列出边界，或在需要长时间高频动画时采用相对时间/分段时间。

**色貌和代码值误差有不同含义。** 本地 Hellwig 与 Fairchild 论文区分 brightness、lightness、colorfulness 和 chroma，也讨论适应亮度及模型取舍；不能把一个 RGB 最大误差阈值宣称为任意 HDR 观看条件下的感知一致性。当前 OKLCH 明确限定 SDR 选色，没有必要为了“模型更复杂”而替换成完整色貌模型。本次直接阅读本地论文的 PDF 第 1、2、11、12 页，并核看附录 A 的公式页。[Hellwig 与 Fairchild 2022](https://doi.org/10.1002/col.22792)

**直通 EXR 是需要显式解释的约定。** OpenEXR 常规合成使用预乘通道；项目明确支持直通并在 Resolve 导入说明中要求人工指定，不能仅凭本项目自定义字段假设任意外部软件都理解关联方式。本次没有将已披露的直通选择判为错误。[OpenEXR Alpha 约定](https://openexr.com/en/latest/TechnicalIntroduction.html#premultiplied-vs-un-premultiplied-color-channels)

## 已有文档与论文的对应关系

项目依据包括 [README](../README.md)、[色彩设置说明](COLOR-SETTINGS.md)、[色域和范围验收](GAMUT-RANGE.md)、[架构说明](../docs/ARCHITECTURE.md)及 [Resolve 导入说明](../docs/RESOLVE_IMPORT.md)。历史验收数字仅作为背景，本次结果单独列在下一节。

本地学习资料用于确认概念与项目目标，具体常数和文件规范由原始资料交叉核对：

- [基础色度学笔记](/Users/rasteaks/Desktop/个人/ColorScienceNotes/01_基础色度学与数学基础/notes.md)第 1.4/1.5 节：光的相加、原色/白点/传递函数。
- [显示与色彩还原笔记](/Users/rasteaks/Desktop/个人/ColorScienceNotes/05_显示与色彩还原/notes.md)第 5.3 节：PQ 绝对亮度、HLG 显示依赖以及先解码再做线性矩阵。
- [色彩管理笔记](/Users/rasteaks/Desktop/个人/ColorScienceNotes/07_色彩管理系统/notes.md)第 7.3/7.4 节：ACES 输入/输出、JMh 和 OCIO 分工。
- 本地 [Hellwig 2022 论文](</Users/rasteaks/Desktop/ColorScience/高等色度学 参考书目和资料/色度学参考文献/Hellwig 2022 色貌模型.pdf>)实际标题为 *Brightness, lightness, colorfulness, and chroma in CIECAM02 and CAM16*，作者 Luke Hellwig、Mark D. Fairchild，*Color Research & Application* 47(5)，1083–1095，DOI `10.1002/col.22792`。它为色貌属性和 ACES 2 理论背景提供依据，不直接证明本项目实现正确。
- Porter、Duff，*Compositing Digital Images*，SIGGRAPH 1984，253–259，DOI `10.1145/800031.808606`。第 2–4 节用于核对覆盖率、预乘及合成公式；[原始论文](https://keithp.com/~keithp/porterduff/p253-porter.pdf)。

## 本次验证记录

运行环境：Python 3.12.7，NumPy 2.5.4，OpenColorIO 2.5.0，OpenImageIO 3.1.18.1，wgpu 0.32.0，Apple M3 Max / Metal。固定 OCIO 配置校验值：`50fbd01c0e20fa56b209e1d6943a8f5ab53d9bf93d666100e3a22be60e094c13`。

| 检查 | 本次结果 |
| --- | --- |
| `pytest`：rendering、encoding、gamut_delivery、color_appearance、live_preview | 144 passed，9.55 秒 |
| 独立公式和缺陷复现脚本 | 完成；上述矩阵、曲线、HDR 及 2000 颜色结果，同时记录缩略图、误认证和 GPU 溢出证据 |
| Ruff：`src tests scripts` | 通过 |
| mypy：`src/chromascript` | 23 个源文件通过 |
| 前端 typecheck、lint、build | 全部通过 |
| 浏览器数值路径 | 1 项通过，5.7 秒；实际检查选色保存、Canvas 调色板及真实 GPU 预览像素一致性 |

第一次在沙箱内执行测试时，135 项通过、9 项因无法枚举 Metal 设备而在初始化阶段报错；在获准访问本机 GPU 的环境中重跑，144 项全部通过。上表采用后一次完整结果。没有把现有测试通过当作四项新发现已经解决。

复现命令（从仓库根目录）：

```sh
.venv/bin/python workspace/audits/2026-10-13-math-color/audit.py
.venv/bin/pytest -q tests/test_rendering.py tests/test_encoding.py tests/test_gamut_delivery.py tests/test_color_appearance.py tests/test_live_preview.py
```

本地复现脚本和机器可读结果分别保存在 [audit.py](../workspace/audits/2026-10-13-math-color/audit.py) 与 [evidence.json](../workspace/audits/2026-10-13-math-color/evidence.json)。该目录受 `.gitignore` 排除，报告保留关键数字以便独立阅读。浏览器验证使用独立数据目录和临时端口；测试服务已关闭，没有写入用户原有项目。

## 修复记录

四项修复保留现有 ACES/OCIO 架构和共享 API。代码注释已更新，README、模型工具内置说明、Resolve 导入说明、架构文档、DESIGN.md 和 UX-CONTRACT.md 同步修正；生成设计令牌并检查一致性，视觉令牌数值未变化。

| 发现 | 已实施的修复 | 回归依据 |
| --- | --- | --- |
| MOV Gamma 互操作 | 只写 colr，不再并写 gama；Gamma 2.4/2.6 MOV 使用未指定 TRC、原生 comment 声明及手动导入提示；检查器拒绝冲突的 gama，缺少声明也失败。保留现有 MOV-only 组合范围，不把它描述成格式标准本身的限制。 | 更新真实编码测试：检查 colr、无 gama、声明内容、缺失声明和冲突字段；加入默认 Rec.709 Gamma 2.4，并保留实际像素解码比较。 |
| sRGB 缩略图 | 显示副本先解码到线性光，按预乘 Alpha 用浮点 BOX 滤波，再恢复直通、编码、最终量化写 PNG。 | 四个小型输入覆盖黑白平均、透明隐藏颜色、不同覆盖率和全透明像素；同时核对原输入不变与 PNG 读回。 |
| 档位与采样验证 | 从实际像素格式描述符检查 RGB/YCbCr、色度采样、通道和位深；按编码器映射比较实际 profile。保留解码器更宽存储和额外不透明 Alpha 的合法情况。 | 一份真实 ProRes 422 文件复用为正确规格、错误档位、错误 444 采样及缺失档位负例；原 ProRes 4444 解码存储测试继续通过。 |
| GPU 累积溢出 | 改为有界的成对平均。正常 float32 值通过指数位精确减半，避免 Metal 把乘法和加法重排为先求大和；靠近下溢边界仍遵循后端 float32 行为。 | 1/4/16 次采样覆盖最大正负 float32、1e38、极小 Alpha、符号抵消和隐藏 RGB；已有无效样本检查和透明边缘测试保留。 |

最终针对性验证：`test_rendering.py`、`test_encoding.py`、`test_gamut_delivery.py`、`test_delivery_options.py` 共 **130 项通过，17.06 秒**。新增的是三个测试函数（参数化后八个用例），另在已有 Gamma 编码测试中增加一个 Rec.709 默认组合；没有建立重复的全格式测试矩阵。

Ruff、mypy（23 个源文件）、前端 typecheck、lint、build 和设计令牌一致性检查均通过。浏览器只运行与本次标签变更直接相关的 ProRes/P3 Gamma、Auto 数据级别和窄屏联动用例，**1 项通过，4.5 秒**；使用独立测试工作区，服务在验证后关闭。没有重复运行全量 E2E、4K/8K 验收或无关选色测试。

Gamma 2.4/2.6 的人工输入解释、float32 的舍入/下溢限制，以及上文披露的显示器和 Resolve 应用内认证边界仍然适用。GPU 数值实现变化会改变既有运行环境指纹，旧任务/缓存继续遵守项目已有的拒绝跨版本恢复规则。
