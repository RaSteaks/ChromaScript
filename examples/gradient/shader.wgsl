// Return scene-linear ACEScg RGB with straight alpha. Time is ctx.time.
fn shade(ctx: SceneContext) -> vec4<f32> {
    let wave = 0.5 + 0.5 * sin(ctx.uv.x * 6.283185 + ctx.time * ctx.params.speed);
    let c = mix(vec3<f32>(0.01, 0.06, 0.18), ctx.params.color, wave);
    return vec4<f32>(c * exp2(ctx.params.exposure) * (0.1 + ctx.uv.y * 3.0), 1.0);
}
