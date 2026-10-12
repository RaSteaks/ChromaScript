// Return scene-linear ACEScg RGB with straight alpha. Time is ctx.time.
fn shade(ctx: SceneContext) -> vec4<f32> {
    let p = ctx.uv;
    let grid = select(0.0, 0.02, fract(p.x * 10.0) < 0.015 || fract(p.y * 10.0) < 0.015);
    let y = 0.5 + sin(p.x * 10.0 + ctx.time * ctx.params.speed) * 0.26;
    let line = 1.0 - smoothstep(0.002, 0.006, abs(p.y - y));
    return vec4<f32>(vec3<f32>(0.008 + grid) + ctx.params.color * line * 2.0, 1.0);
}
