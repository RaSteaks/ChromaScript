import test from "node:test";
import assert from "node:assert/strict";
import {
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  unlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  FLOAT_CHUNK_BYTES,
  transportFloatFrames,
} from "../motion/float-transport.mjs";

test("float transport pipelines one capture while keeping one raw file and exact order", async () => {
  const directory = mkdtempSync(join(tmpdir(), "chroma-transport-"));
  const captured = [],
    accepted = [],
    lengths = [];
  let bytes;
  // Model only the browser RPC boundary. Files, chunks and ACK scheduling are real.
  const page = {
    async evaluate(_fn, args) {
      if (typeof args === "number") {
        captured.push(args);
        bytes = Buffer.alloc(FLOAT_CHUNK_BYTES + 16, args);
        return { width: bytes.length / 16, height: 1, bytes: bytes.length };
      }
      lengths.push(args.length);
      return bytes
        .subarray(args.offset, args.offset + args.length)
        .toString("base64");
    },
  };
  try {
    const timing = await transportFloatFrames(
      page,
      [2, 5, 8],
      directory,
      async (frame) => {
        assert.deepEqual(readdirSync(directory), [`frame-${frame}.f32`]);
        assert.deepEqual(
          readFileSync(join(directory, `frame-${frame}.f32`)),
          Buffer.alloc(FLOAT_CHUNK_BYTES + 16, frame),
        );
        // The next capture has begun, but its bytes cannot overwrite the current file.
        assert.equal(captured.length, Math.min(accepted.length + 2, 3));
        accepted.push(frame);
        unlinkSync(join(directory, `frame-${frame}.f32`));
      },
    );
    assert.deepEqual(accepted, [2, 5, 8]);
    assert.equal(lengths.length, 6);
    assert.equal(timing.bytes, 3 * (FLOAT_CHUNK_BYTES + 16));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("capture failures during consumption are surfaced after the previous ACK", async () => {
  const directory = mkdtempSync(join(tmpdir(), "chroma-transport-"));
  const accepted = [];
  const page = {
    async evaluate(_fn, args) {
      if (args === 1) throw new Error("GPU capture failed");
      if (args === 0) return { width: 1, height: 1, bytes: 16 };
      return Buffer.alloc(16).toString("base64");
    },
  };
  try {
    await assert.rejects(
      transportFloatFrames(page, [0, 1, 2], directory, async (frame) => {
        await new Promise((resolve) => setImmediate(resolve));
        accepted.push(frame);
        unlinkSync(join(directory, `frame-${frame}.f32`));
      }),
      /GPU capture failed/,
    );
    assert.deepEqual(accepted, [0]);
    assert.deepEqual(readdirSync(directory), []);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("truncated chunks cannot be acknowledged as complete float frames", async () => {
  const directory = mkdtempSync(join(tmpdir(), "chroma-transport-"));
  const page = {
    async evaluate(_fn, args) {
      return typeof args === "number"
        ? { width: 1, height: 1, bytes: 16 }
        : "AA==";
    },
  };
  try {
    await assert.rejects(
      transportFloatFrames(page, [0], directory, () =>
        assert.fail("invalid frame must not be acknowledged"),
      ),
      /chunk length/,
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
