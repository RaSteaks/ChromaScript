# 输出色彩设置规范化

日期：2026-10-12（Asia/Taipei）。

本轮将色域、传递函数、显示目标亮度和数据范围分开。用户界面不再把“Rec.709 / Rec.1886、sRGB、PQ · 1000 nit、HLG · 1000 nit”放在一个混合菜单中。

| 设置 | 规范 |
|---|---|
| 输出色彩空间 | Rec.709、Rec.2020、ACEScg（AP1）、ACES2065-1（AP0），按格式限制选项 |
| 传递函数 / Gamma | 线性（Linear）、Gamma 2.4（BT.1886）、sRGB（IEC 61966-2-1）、PQ（SMPTE ST 2084）、HLG（ARIB STD-B67） |
| 目标显示峰值亮度 | 独立只读字段：当前 SDR 输出变换 100 cd/m²，HDR 输出变换 1000 cd/m²，线性母版不适用 |
| 数据范围 | 视频范围（Limited）、全范围（Full），由格式约束联动 |

亮度是当前 ACES 输出变换的目标显示参数，不能视为 PQ/HLG 标准固有上限，也不代表新增可编辑的母版显示元数据。ACES 线性母版不烘焙显示变换。界面和模型接口共用标准名称；`get_delivery_options.color_encoding` 记录完整编码名称、白点、动态范围、限制色域、输出视图及 `target_display_peak_nits`。同一描述进入原生交付检查报告。输入 API 的 `gamma` 名称和旧 `color_output` 别名保留兼容。

旧版守护服务不再触发混合菜单回退；界面明确提示更新并阻止不完整配置提交。已在确认无活动渲染后更新本机 8765 服务，核对原有 1 个项目及 7 个任务的标识、任务状态和版本引用保持一致。

## 验证

- 90 项 Python 测试通过（12.46 秒），包括实际编码/图像读回和六种色彩描述。
- 18 项浏览器测试通过（25.7 秒）；最后的控件对齐修改另有 4 项相关测试通过。
- 独立可访问性检查通过；四个工作流步骤及 MCP 页的 Axe 相关规则无违规。
- TypeScript、ESLint、Vite 构建、Ruff、mypy 21 个模块通过。
- DESIGN.md lint、生成令牌一致性、strict 页面审计通过，0 错误、0 警告。
- 实际 stdio 与 Streamable HTTP 各 56 个 MCP 工具；同一 HDR 请求返回完全一致的规范色彩设置和 1000 nit 目标。
- 主服务实际页面显示独立色彩空间、传递函数、目标亮度和数据范围控件。
- 临时验收工作区及样片在完成后清理，保留界面截图和静态审计证据；不改动用户的工作区成果。

## 契约对齐

原先为运行中的旧服务保留混合菜单回退。本次用户明确要求规范输出色彩设置，因此改为标准字段必需、服务过旧时提示更新。同步修改 DESIGN.md、UX-CONTRACT.md、公共接口说明和恢复测试；中性灰视觉及生成令牌保持一致。

## 参考

- [ACES 输出变换参数](https://docs.acescentral.com/system-components/output-transforms/parameters/)：色域、峰值亮度与显示编码分别定义。
- [ITU-R BT.2100](https://www.itu.int/rec/R-REC-BT.2100)：PQ 与 HLG 的 HDR 信号参数及参考显示条件。
