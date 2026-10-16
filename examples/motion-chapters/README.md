# 单场景、多个分章文件

`float.json` 与 `sdr.json` 是可直接提交的完整 `MotionSpec`：前半段红色，后半段绿色。两者共享 1280 × 720、150 帧、30000/1001 fps 的画面与时间规格。

- 浮点示例：入口 `code` 创建唯一的 scene/camera，通过 `chapters/index.ts`、`chapters/intro.ts` 和 `shared/colors.js` 联合计算每帧状态；线性 RGB 包含大于 1 的值。
- SDR 示例：入口通过 Remotion `Sequence` 安排 `chapters/intro.tsx` 和 `chapters/ending.jsx`，分章可使用各自的局部帧号。

网页中用“添加分章文件”创建 `files` 内的路径，填写分章源码，然后在入口中用相对路径导入。文件列表顺序不等于播放顺序，顺序和时间由入口代码决定。浮点分章应根据绝对帧号/时间完整设置状态，确保跳帧、倒退和恢复渲染得到相同结果。

CLI 创建示例（在仓库根目录执行，替换项目 ID；中间文件留在被 Git 忽略的 workspace 中）：

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path

spec = json.loads(Path("examples/motion-chapters/float.json").read_text())
Path("workspace").mkdir(exist_ok=True)
Path("workspace/create-chapters.json").write_text(json.dumps({
    "project_id": "替换为项目 ID",
    "spec": spec,
}, ensure_ascii=False))
PY
.venv/bin/chromascript call create_motion_scene --arguments @workspace/create-chapters.json
```

MCP 对应 `chromascript.create_motion_scene`，参数为 `project_id` 与 `spec`。修改使用 `update_motion_scene(scene_id, expected_revision_id, spec)`，其中 `spec.files` 是完整文件集合，移除键即删除该分章。预览使用 `compile_motion_preview(spec)` 或 `render_motion_frame(spec, frame)`；保存后使用 `submit_motion_render`，不需要分别提交各章。

支持 `.ts`、`.tsx`、`.js`、`.jsx`，可用 `./chapter`、`./chapter.ts`、`./chapters`（匹配 `index`）导入。额外文件最多 128 个，每个最多 200000 字符，全部源码合计最多 2000000 字符。分章路径使用英文、数字、下划线、连字符和 `/`，必须包含扩展名，不得与入口或其他文件产生大小写冲突。相对导入只解析当前动画内的源码，不读取宿主文件、不下载模块。

预览、导出和恢复都使用同一份完整源码快照。多文件交付的 `sources/` 中包含入口和全部分章，`animation.json` 保存完整规格；原有单文件动画继续使用原来的接口与导出路径。
