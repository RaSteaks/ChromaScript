# Resolve 手动导入验收

按照你的选择，Resolve 应用内导入由你自行完成。下面是工具输出的预期解释；应用内检查尚未执行，也未测量 HDR 显示器。

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

整数图像序列统一带有 `sequence.json`。DPX/TIFF 等格式的文件头不一定携带全部色彩、时间及自定义信息，应依据 sidecar 指定输入解释。
