// Return scene-linear ACEScg RGB with straight alpha. Time is ctx.time.
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
