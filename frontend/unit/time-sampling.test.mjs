import test from "node:test";
import assert from "node:assert/strict";
import { frameSamples } from "../motion/time-sampling.mjs";

const plan = {
  source_frames: 600,
  source_fps: { numerator: 60, denominator: 1 },
  frames: 300,
  fps: { numerator: 30, denominator: 1 },
  temporal_samples: 1,
  shutter_angle: 180,
};

test("fast sampling keeps original frame/time; noninteger ratios hold the preceding frame", () => {
  assert.deepEqual(
    [0, 1, 60, 299].map((index) => frameSamples(plan, index)),
    [[0], [2], [120], [598]],
  );
  assert.deepEqual(
    frameSamples(
      { ...plan, fps: { numerator: 24, denominator: 1 }, frames: 240 },
      1,
    ),
    [2],
  );
  assert.deepEqual(
    frameSamples(
      {
        ...plan,
        source_fps: { numerator: 60000, denominator: 1001 },
        fps: { numerator: 30000, denominator: 1001 },
      },
      60,
    ),
    [120],
  );
});
test("centered exposure uses fractional original frames, clamps boundaries, and is seek-order independent", () => {
  const temporal = { ...plan, temporal_samples: 4 };
  assert.deepEqual(frameSamples(temporal, 1), [1.625, 1.875, 2.125, 2.375]);
  assert.deepEqual(frameSamples(temporal, 0), [0, 0, 0.125, 0.375]);
  assert.deepEqual(
    frameSamples({ ...temporal, source_frames: 1, frames: 1 }, 0),
    [0, 0, 0, 0],
  );
  frameSamples(temporal, 299);
  assert.deepEqual(frameSamples(temporal, 1), [1.625, 1.875, 2.125, 2.375]);
  assert.throws(() => frameSamples(plan, 300));
});
