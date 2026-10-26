import { once } from "node:events";
import { VIDEO_IN_FLIGHT } from "./video-protocol.mjs";

/** Bounded GPU/PBO captures in flight, one ordered binary delivery stream. */
export async function transportVideoFrames(page, indices, thumbnail, accept) {
  const started = performance.now();
  const timing = { capture_ms: 0, consumer_ms: 0, bytes: 0 };
  const capture = (position) => {
    if (position >= indices.length) return null;
    const start = performance.now();
    return page
      .evaluate(
        async ({ frame, slot, thumbnail }) =>
          window.__chromaFloat.captureVideo(frame, slot, thumbnail),
        {
          frame: indices[position],
          slot: position % VIDEO_IN_FLIGHT,
          thumbnail: thumbnail && indices[position] === 0,
        },
      )
      .then(
        (result) => {
          timing.capture_ms += performance.now() - start;
          return { result };
        },
        (error) => ({ error }),
      );
  };
  const pending = Array.from({ length: VIDEO_IN_FLIGHT }, (_, index) =>
    capture(index),
  );
  for (let position = 0; position < indices.length; position++) {
    const slot = position % VIDEO_IN_FLIGHT;
    const captured = await pending[slot];
    if (captured.error) throw captured.error;
    const pixels = Buffer.from(captured.result.pixels, "base64");
    const thumb = captured.result.thumbnail
      ? Buffer.from(captured.result.thumbnail, "base64")
      : Buffer.alloc(0);
    timing.bytes += pixels.length;
    const start = performance.now();
    await accept(indices[position], pixels, thumb);
    timing.consumer_ms += performance.now() - start;
    pending[slot] = capture(position + VIDEO_IN_FLIGHT);
  }
  return {
    ...timing,
    total_ms: performance.now() - started,
    frames: indices.length,
    in_flight: VIDEO_IN_FLIGHT,
    binary_pipe: true,
    temporary_frame_files: 0,
  };
}

export async function writeVideoPacket(frame, pixels, thumbnail) {
  const header = Buffer.from(
    JSON.stringify({
      frame,
      bytes: pixels.length,
      thumbnail_bytes: thumbnail.length,
    }),
  );
  const length = Buffer.alloc(4);
  length.writeUInt32LE(header.length);
  // Respect pipe pressure rather than retaining a whole video in Node memory.
  for (const part of [length, header, pixels, thumbnail]) {
    if (part.length && !process.stdout.write(part))
      await once(process.stdout, "drain");
  }
}
