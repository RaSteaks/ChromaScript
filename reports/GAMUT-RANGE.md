# 色域与数据范围扩展验收

日期：2026-10-12（Asia/Taipei）。

用户要求的 P3、709、BT.2020、ACEScct、ACEScg 已接入实际输出。P3 明确采用 P3-D65；Rec.709/BT.709 和 BT.2020/Rec.2020 使用规范名称并兼容常用输入别名。ACEScct 使用 AP1 对数曲线，ACEScg 使用 AP1 线性编码，保留既有 AP0 归档能力。

数据范围显示 Full / Limited，`limit` 输入规范为 `limited`。所有范围和色域选项保持可见，当前格式不可用的选项禁用并给出原因。H.264/HEVC 支持两种范围；RGB/浮点输出采用 Full，ProRes/DNxHR/v210 的 YCbCr 输出按 Limited 约定处理。没有将无法可靠标记的 Full 输出宣称为可互换交付。

## 实现与限制

- P3 sRGB/PQ 使用固定 CG Config v4 / ACES 2.0 中实际 P3-D65 显示与限制色域视图；像素不沿用 Rec.709 后改标签。
- P3 视频写入 SMPTE 432 原色标记，检查物理 colr 与 codec 标记。YCbCr 矩阵独立标明，保持可逆的 RGB 打包解释。
- ACEScct 整型视频至少 10-bit；浮点 EXR 保留其负值和大于 1 的代码值。超出整数存储范围的日志值返回错误，建议使用 float EXR。
- EXR ACEScct 写入 AP1 chromaticities、`ocio:acescct_ap1_scene` 与显式 gamma 信息，不使用 AP0 ACES image-container flag。
- H.273 没有 AP1/ACEScct 标识。ACEScct 视频使用未指定原色/传递函数、原生 comment 声明及报告解释，导入时需指定 ACES (AP1) / ACEScct；没有声称已完成 Resolve 导入认证。
- 模型、CLI、HTTP 保留显式字段意图，默认 8-bit 不再覆盖部分请求中的 HDR/ACEScct 选择。传输层保留未指定字段的区别；提交仍严格拒绝矛盾组合。
- 网页的 sRGB 审阅预览与 P3/对数交付区分；宽色域文件不会被标成 sRGB 运动预览。

## 验证结果

| 检查 | 结果 |
|---|---|
| Python 完整回归 | 132 项通过，16.21 秒 |
| 浏览器完整回归 | 25 项通过，34.3 秒；含色域/范围、精度联动、实际 P3 交付及独立预览 |
| 独立 accessibility 命令 | 2 项通过 |
| 类型、静态和构建 | Ruff、mypy 22 个模块、TypeScript、ESLint、Vite 全部通过 |
| 设计契约 | strict 审计、DESIGN lint、令牌一致性及 diff 检查通过，0 错误、0 警告 |
| 原始数值 | ACEScct 与官方分段公式匹配；float EXR 负值和高光精确读回；P3 与官方视图匹配且不同于 Rec.709 像素 |
| 实际范围 | 解码 P3 H.264 的原生 Y 样本，Full 为 0–255、Limited 为 16–235 |
| MCP | stdio / HTTP 各 57 个工具，色彩选择返回一致，未指定精度的 ACEScct 请求正确升至 10-bit |

仅通过 MCP 批量生成 320×180、30 fps、3 帧的七种实际输出并全部通过文件检查：P3 H.264 Full、P3 H.264 Limited、BT.2020 HEVC PQ、ACEScct ProRes MOV、ACEScct float EXR、ACEScg half EXR、P3 HEVC PQ。浮点对数 EXR 与原始 GPU 母版经 ACEScct 转换后的数值一致。

主服务 8765 已加载新接口，核对原有 1 个项目及 8 个任务的标识、状态和版本引用保留。临时工作区、样片和测试缓存在验收后清理；截图与静态/MCP 验证数据保留在已忽略的 reports 路径下。以上结果仅认证上述组合，没有重新执行完整 4K/60fps 母版验收。

## 设计契约对齐

色域及范围列表从隐藏不适用项调整为可见禁用项，符合本次要求的完整选项列表，并解释格式限制。复用 Field、原生 select、现有提示和视觉令牌，同步 DESIGN.md、UX-CONTRACT.md 与 MCP 文档。

## 参考

- [ACEScct 官方规范](https://docs.acescentral.com/encodings/acescct/)
- [ACES 官方输出参数](https://docs.acescentral.com/system-components/output-transforms/parameters/)
- [FFmpeg 7.1 原色/传递标记](https://www.ffmpeg.org/doxygen/7.1/prores__metadata_8c.html)
