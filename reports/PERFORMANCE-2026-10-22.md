# 渲染、交互与预览优化验证

日期：2026-10-22（Asia/Taipei）。基线：`4215349`。设备：Apple M3 Max / Metal。

## 修改范围

- WGSL 原生渲染将最多四个分块合并提交、读回，输出缓冲上限 16 MiB；每块独立 uniform/storage 区间，保留原采样算法、Alpha、float32 和有理数帧时间。取消回调可在当前批次结束后阻止下一批。
- 实时 WGSL 预览从 125 ms 调度间隔调整到约 41.7 ms，渲染耗时计入刷新间隔；请求保持串行，慢帧不排队。
- 缓存未改变的源码序列化结果；编辑器、隐藏的动画适配器跳过无关播放更新，动画消息监听不再逐帧重新绑定。
- 返回未改动的动画复用原 iframe；显式重新编译仍创建候选并保留旧画面直到新画面就绪。
- HDR 跳转优先复用完整缓冲帧，循环中选择最近副本；已淘汰或未生成的帧仍重建片段。帧号在实际解码后确认。
- 精简重复说明，色彩与管线细节折叠；错误、冲突、保存状态、精度与规格仍可见。README 前置快速开始，以表格、导航和折叠章节整理参考内容。

## 原生渲染测量

同一设备、场景、帧 12、512 分块。各先热身一帧，再测量七次，比较中位数。计时只包含 `Renderer.render`，不含 OCIO、图像写盘、视频编码、GPU 冷启动或网络。

| 场景与规格 | 优化前 | 优化后 | 耗时减少 |
| --- | ---: | ---: | ---: |
| gradient · 1920×1080 · 4 样本 | 29.757 ms | 13.757 ms | 53.8% |
| noise · 1280×720 · 1 样本 | 14.725 ms | 7.422 ms | 49.6% |

这些样例不代表所有场景或完整交付的速度。原始数据保留在 `performance-before.json`、`performance-after.json`。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| Python：test_rendering、test_live_preview、test_color_appearance、test_interfaces | 80 项通过 |
| Node 单元检查 | 13 项通过，含 HDR 缓冲复用、循环定位、淘汰重建与音频缓冲门控 |
| WebKit：live-preview、unified-workspace、expanded-preview | 20 项通过 |
| WebKit：完整 workbench | 18 项通过 |
| WebKit：motion 定向检查 | 7 项通过，含新增复用检查和桌面/窄屏 GPU 错误恢复 |
| 独立 test:a11y：live-preview、unified-workspace | 2 项通过（为上述覆盖的复核） |
| Ruff / mypy / TypeScript / ESLint / Prettier | 通过 |
| Vite 生产构建 | 通过；仍有现有主包超过 500 kB 的体积提示 |
| UI 严格静态审查 / tokens 漂移检查 | 通过，0 项发现；重新生成的 tokens 与原值一致 |
| README 本地链接、代码围栏、折叠标签 | 通过 |

新增 GPU 检查以独立像素/时间参考覆盖九个不规则边缘分块、跨批次的两帧、六次读回、取消后缓冲复用。已有检查继续验证负值、高光、透明覆盖、非有限值拒绝、EXR 精度和视频实际解码。

首次浏览器启动发现默认缓存缺少匹配 WebKit；改用 `/tmp/chromascript-playwright` 的匹配运行时。焦点检查曾在 WebKit 完成原生滚动前读到旧位置，现等待真实命中目标，仍拒绝遮挡。定向执行时发现工作台旧用例依赖完整文件中的场景准备，已按完整工作台流程复核通过。未降低任何产品检查条件。

## 证据与边界

- 内置浏览器检查了项目创建、模板打开、实时播放/暂停及页面排版；自动化仅使用无头 WebKit，无个人浏览器配置，未启动 Chrome。
- 服务使用独立临时工作区 `/tmp/chromascript-performance-20261022`、端口 8875，没有对用户工作区创建测试项目。
- 日志：`performance-python.log`、`performance-unit.log`、`performance-browser.log`、`performance-workbench-browser.log`、`performance-motion-browser.log`、`performance-a11y.log`、`performance-build.log`；截图为本目录 `live-preview-*.png`、`motion-*.png`、`workbench-*.png`。
- HTML 报告和失败追踪保存在 `frontend/playwright-report/`、`frontend/test-results/`。这些运行产物按仓库规则忽略，文本报告保留。
- HDR 缓冲复用通过播放器单元检查；未认证实体 HDR 屏亮度。因本次不启动 Chrome，未运行依赖其离线浏览器的动画整段导出；WGSL 原生渲染、图像精度和编码回归已运行。
