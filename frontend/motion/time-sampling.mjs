// Both engines use this sampler. BigInt keeps rational frame boundaries exact;
// conversion to floating point happens only at the source evaluation boundary.
export function frameSamples(plan, frame) {
  if (!Number.isInteger(frame) || frame < 0 || frame >= plan.frames)
    throw new Error("输出帧索引超出范围。");
  const p = BigInt(plan.source_fps.numerator) * BigInt(plan.fps.denominator);
  const q = BigInt(plan.source_fps.denominator) * BigInt(plan.fps.numerator);
  const last = BigInt(plan.source_frames - 1);
  if (plan.temporal_samples === 1)
    return [
      Number((BigInt(frame) * p) / q > last ? last : (BigInt(frame) * p) / q),
    ];
  const count = BigInt(plan.temporal_samples);
  const scale = 720n * count;
  const denominator = q * scale;
  return Array.from({ length: plan.temporal_samples }, (_, sample) => {
    // Stratified midpoint exposure, centered on the output timestamp. Endpoint
    // hold avoids evaluating user code outside its original composition range.
    const numerator =
      p *
      (BigInt(frame) * scale +
        (2n * BigInt(sample) + 1n - count) * BigInt(plan.shutter_angle));
    if (numerator <= 0n) return 0;
    if (numerator >= last * denominator) return Number(last);
    return Number(numerator) / Number(denominator);
  });
}
