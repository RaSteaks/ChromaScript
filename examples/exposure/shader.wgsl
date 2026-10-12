// Return scene-linear ACEScg RGB with straight alpha. Time is ctx.time.
fn shade(ctx: SceneContext) -> vec4<f32> {
    let step = floor(ctx.uv.x * 13.0) - 6.0;
    var v = 0.18 * exp2(step + ctx.params.exposure);
    if ctx.uv.y < 0.08 { v = -0.025; }
    return vec4<f32>(vec3<f32>(v), 1.0);
}
