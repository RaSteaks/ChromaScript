"""Auditable built-in shader templates; external models can retrieve and adapt these."""

from __future__ import annotations

from .models import Parameter, SceneSpec

BASE = {
    "exposure": Parameter(
        name="exposure", label="曝光（档）", default=0.0, minimum=-10, maximum=10
    ),
    "speed": Parameter(name="speed", label="速度", default=0.3, minimum=0, maximum=10),
    "color": Parameter(name="color", label="颜色", kind="color", default=[0.8, 0.25, 0.035]),
}

TEMPLATES: dict[str, SceneSpec] = {
    "exposure": SceneSpec(
        name="曝光阶梯",
        description="18% 中灰的 -6 至 +6 档曝光，保留高光与测试负值。",
        parameters=[BASE["exposure"]],
        code="""
fn shade(ctx: SceneContext) -> vec4<f32> {
    let step = floor(ctx.uv.x * 13.0) - 6.0;
    var v = 0.18 * exp2(step + ctx.params.exposure);
    if ctx.uv.y < 0.08 { v = -0.025; }
    return vec4<f32>(vec3<f32>(v), 1.0);
}
""",
    ),
    "gradient": SceneSpec(
        name="广色域渐变",
        description="线性 ACEScg 色彩、明暗渐变与柔和移动。",
        parameters=[BASE["color"], BASE["exposure"], BASE["speed"]],
        code="""
fn shade(ctx: SceneContext) -> vec4<f32> {
    let wave = 0.5 + 0.5 * sin(ctx.uv.x * 6.283185 + ctx.time * ctx.params.speed);
    let c = mix(vec3<f32>(0.01, 0.06, 0.18), ctx.params.color, wave);
    return vec4<f32>(c * exp2(ctx.params.exposure) * (0.1 + ctx.uv.y * 3.0), 1.0);
}
""",
    ),
    "shapes": SceneSpec(
        name="几何与透明度",
        description="解析形状、运动与透明边缘，用于检查 Alpha 合成。",
        parameters=[BASE["color"], BASE["speed"]],
        code="""
fn shade(ctx: SceneContext) -> vec4<f32> {
    let aspect = ctx.resolution.x / ctx.resolution.y;
    let p = (ctx.uv - vec2<f32>(0.5)) * vec2<f32>(aspect, 1.0);
    let center = vec2<f32>(sin(ctx.time * ctx.params.speed) * 0.25, 0.0);
    let d = length(p - center);
    let a = 1.0 - smoothstep(0.225, 0.235, d);
    return vec4<f32>(ctx.params.color * 2.5, a);
}
""",
    ),
    "particles": SceneSpec(
        name="螺旋粒子",
        description="固定种子的解析粒子轨道；任意时间可独立渲染。",
        parameters=[BASE["color"], BASE["speed"], BASE["exposure"]],
        code="""
fn shade(ctx: SceneContext) -> vec4<f32> {
    let p = (ctx.uv - vec2<f32>(0.5)) * vec2<f32>(ctx.resolution.x / ctx.resolution.y, 1.0);
    let r = length(p);
    let angle = atan2(p.y, p.x) + ctx.time * ctx.params.speed;
    let spiral = pow(max(0.0, cos(angle * 3.0 - r * 35.0)), 30.0);
    let beads = pow(max(0.0, cos(r * 160.0 + angle * 8.0)), 18.0);
    let falloff = exp(-r * 4.0);
    let glow = spiral * (0.2 + beads * 7.0) * falloff;
    return vec4<f32>((vec3<f32>(0.002, 0.004, 0.008) + ctx.params.color * glow) * exp2(ctx.params.exposure), 1.0);
}
""",
    ),
    "noise": SceneSpec(
        name="程序纹理",
        description="固定种子哈希噪声与平滑运动。",
        parameters=[BASE["color"], BASE["speed"]],
        code="""
fn hash_u32(x: u32) -> u32 {
    var n = x;
    n = (n ^ (n >> 16u)) * 2246822519u;
    n = (n ^ (n >> 13u)) * 3266489917u;
    return n ^ (n >> 16u);
}
fn shade(ctx: SceneContext) -> vec4<f32> {
    let p = vec2<u32>(floor(ctx.pixel / 5.0 + vec2<f32>(ctx.time * ctx.params.speed * 20.0, 0.0)));
    let n = f32(hash_u32(p.x + p.y * 65537u + ctx.seed) & 65535u) / 65535.0;
    return vec4<f32>(ctx.params.color * (0.02 + n * 2.0), 1.0);
}
""",
    ),
    "chart": SceneSpec(
        name="动态图表",
        description="解析时间曲线；时间与数值由代码控制。",
        parameters=[BASE["color"], BASE["speed"]],
        code="""
fn shade(ctx: SceneContext) -> vec4<f32> {
    let p = ctx.uv;
    let grid = select(0.0, 0.02, fract(p.x * 10.0) < 0.015 || fract(p.y * 10.0) < 0.015);
    let y = 0.5 + sin(p.x * 10.0 + ctx.time * ctx.params.speed) * 0.26;
    let line = 1.0 - smoothstep(0.002, 0.006, abs(p.y - y));
    return vec4<f32>(vec3<f32>(0.008 + grid) + ctx.params.color * line * 2.0, 1.0);
}
""",
    ),
}


def template_list() -> list[dict[str, str]]:
    return [
        {"id": key, "name": scene.name, "description": scene.description}
        for key, scene in TEMPLATES.items()
    ]
