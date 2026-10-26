import { openSync, writeSync, closeSync } from "node:fs";
import { join } from "node:path";

// Bound individual protocol messages, while amortizing browser round trips.
export const FLOAT_CHUNK_BYTES = 4 * 1024 * 1024;

/** One raw file on disk; the next capture overlaps consumption of the current one. */
export async function transportFloatFrames(page, indices, directory, accept) {
  const started = performance.now();
  const timing = { capture_ms: 0, transfer_ms: 0, consumer_ms: 0, bytes: 0 };
  const capture = (frame) => {
    const start = performance.now();
    // Settle errors immediately: a capture can fail while Python is still consuming
    // the previous frame. Propagate that failure after its ACK, without an unhandled rejection.
    return page
      .evaluate((f) => window.__chromaFloat.capture(f), frame)
      .then(
        (value) => {
          timing.capture_ms += performance.now() - start;
          return { value };
        },
        (error) => ({ error }),
      );
  };
  let pending = indices.length ? capture(indices[0]) : null;
  for (let index = 0; index < indices.length; index++) {
    const result = await pending;
    if (result.error) throw result.error;
    const captured = result.value;
    if (captured.bytes !== captured.width * captured.height * 16)
      throw new Error("Float frame dimensions mismatch");
    const start = performance.now();
    const fd = openSync(join(directory, `frame-${indices[index]}.f32`), "w");
    try {
      for (
        let offset = 0;
        offset < captured.bytes;
        offset += FLOAT_CHUNK_BYTES
      ) {
        const data = await page.evaluate(
          ({ offset, length }) =>
            window.__chromaFloat.readChunk(offset, length),
          { offset, length: FLOAT_CHUNK_BYTES },
        );
        const bytes = Buffer.from(data, "base64");
        if (
          bytes.length !== Math.min(FLOAT_CHUNK_BYTES, captured.bytes - offset)
        )
          throw new Error("Float frame chunk length mismatch");
        let written = 0;
        while (written < bytes.length)
          written += writeSync(fd, bytes, written, bytes.length - written);
      }
    } finally {
      closeSync(fd);
    }
    timing.transfer_ms += performance.now() - start;
    timing.bytes += captured.bytes;
    // Transfer finished before reusing the browser's float buffer. The next frame
    // stays in that buffer until ACK removes the previous raw file; disk usage and
    // exact frame order remain unchanged. At most one speculative frame is rendered.
    pending = index + 1 < indices.length ? capture(indices[index + 1]) : null;
    const consumerStart = performance.now();
    await accept(indices[index]);
    timing.consumer_ms += performance.now() - consumerStart;
  }
  return {
    ...timing,
    total_ms: performance.now() - started,
    frames: indices.length,
    chunk_bytes: FLOAT_CHUNK_BYTES,
    lookahead_frames: 1,
  };
}
