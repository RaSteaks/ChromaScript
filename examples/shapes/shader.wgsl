// Return scene-linear ACEScg RGB with straight alpha. Time is ctx.time.
fn shade(ctx: SceneContext) -> vec4<f32> {
    let aspect = ctx.resolution.x / ctx.resolution.y;
    let p = (ctx.uv - vec2<f32>(0.5)) * vec2<f32>(aspect, 1.0);
    let center = vec2<f32>(sin(ctx.time * ctx.params.speed) * 0.25, 0.0);
    let d = length(p - center);
    let a = 1.0 - smoothstep(0.225, 0.235, d);
    return vec4<f32>(ctx.params.color * 2.5, a);
}
