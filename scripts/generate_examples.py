"""Export model-readable scene JSON and standalone WGSL from the validated templates."""

from pathlib import Path

from chromascript.templates import TEMPLATES

root = Path(__file__).resolve().parents[1] / "examples"
for name, scene in TEMPLATES.items():
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    # JSON remains directly compatible with SceneSpec; WGSL is a separate editable copy.
    (folder / "scene.json").write_text(scene.model_dump_json(indent=2) + "\n")
    (folder / "shader.wgsl").write_text(
        "// Return scene-linear ACEScg RGB with straight alpha. Time is ctx.time.\n"
        + scene.code.lstrip()
    )
(root / "README.md").write_text("""# 场景示例

六个示例与内置模板一致：曝光阶梯、广色域渐变、几何与透明度、螺旋粒子、程序纹理、动态图表。

`scene.json` 是完整 `SceneSpec`，包含 WGSL、参数说明与固定种子。`shader.wgsl` 是供编辑的独立代码副本。修改独立文件后，把文本写入 `SceneSpec.code`，再通过 `create_scene` 或 `update_scene` 提交；工具不会执行辅助脚本。

MCP 创建示例：`chromascript.create_scene({\"request\": {\"project_id\": \"项目 ID\", \"spec\": <scene.json 内容>}})`。已有场景修改时须带上 `expected_revision_id`，防止覆盖其他客户端修改。

场景接口与创作流程通过 `chromascript://docs/scene` 和 `chromascript://docs/workflow` 读取。色彩参数可以声明为 sRGB，工具先转换到线性 ACEScg；示例中的 RGB 默认已是 ACEScg。
""")
