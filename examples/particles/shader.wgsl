// Return scene-linear ACEScg RGB with straight alpha. Time is ctx.time.
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
