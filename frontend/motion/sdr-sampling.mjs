import {
  mkdirSync,
  writeFileSync,
  renameSync,
  rmSync,
  readdirSync,
} from "node:fs";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { frameSamples } from "./time-sampling.mjs";

// The envelope is only a transport index. Freeze evaluates source frames while
// useVideoConfig still sees the original composition fps, duration and dimensions.
export const sampledScene = `function SampledScene({sampleFrames}) {
  const frame = useCurrentFrame();
  return <Freeze frame={sampleFrames ? sampleFrames[frame] : frame}><Scene /></Freeze>;
}`;

export async function renderSampledFrames(renderFrames, options, request) {
  const plan = request.time_plan;
  const indices =
    request.frame_indices ??
    (request.frame === undefined
      ? Array.from({ length: plan.frames }, (_, index) => index)
      : [request.frame]);
  const batchSize = Math.max(
    1,
    Math.floor(Math.min(64, plan.source_frames) / plan.temporal_samples),
  );
  const temporary = join(request.directory, "temporal-samples");
  // Retry restarts SDR rendering. Leftover partial sample batches are disposable.
  rmSync(temporary, { recursive: true, force: true });
  mkdirSync(request.frames, { recursive: true });
  let completed = 0;
  for (let start = 0; start < indices.length; start += batchSize) {
    mkdirSync(temporary, { recursive: true });
    const batch = indices.slice(start, start + batchSize);
    const samples = batch.flatMap((frame) => frameSamples(plan, frame));
    // Very short sources still permit multiple exposure samples; each envelope
    // stays within the unchanged source duration accepted by Remotion.
    for (
      let offset = 0;
      offset < samples.length;
      offset += plan.source_frames
    ) {
      const values = samples.slice(offset, offset + plan.source_frames);
      await renderFrames({
        ...options,
        outputDir: temporary,
        inputProps: { sampleFrames: values },
        composition: {
          ...options.composition,
          props: { sampleFrames: values },
        },
        frames: values.map((_, index) => index),
        frameRange: undefined,
        imageSequencePattern: `sample-${offset}-[frame].png`,
        onStart: undefined,
        onFrameUpdate: undefined,
      });
    }
    const files = new Map(
      readdirSync(temporary).flatMap((name) => {
        const match = /^sample-(\d+)-(\d+)\.png$/.exec(name);
        return match
          ? [[Number(match[1]) + Number(match[2]), join(temporary, name)]]
          : [];
      }),
    );
    if (files.size !== samples.length) throw new Error("时间采样截图不完整。");
    const groups = batch.map((frame, index) => ({
      output: join(request.frames, `frame-${frame}.png`),
      samples: Array.from({ length: plan.temporal_samples }, (_, sample) => {
        const ordinal = index * plan.temporal_samples + sample;
        const path = files.get(ordinal);
        if (!path) throw new Error("时间采样截图索引不符。");
        return path;
      }),
    }));
    if (plan.temporal_samples === 1) {
      for (const group of groups) renameSync(group.samples[0], group.output);
    } else {
      const manifest = join(temporary, "samples.json");
      writeFileSync(manifest, JSON.stringify(groups));
      // A bounded batch is merged by the same local numerical implementation
      // used by output previews; intermediate screenshots are then discarded.
      execFileSync(
        request.python,
        ["-m", "chromascript.motion_sdr_sampling", manifest],
        { stdio: "pipe" },
      );
    }
    rmSync(temporary, { recursive: true });
    completed += batch.length;
    options.onFrameUpdate?.(completed);
  }
}
