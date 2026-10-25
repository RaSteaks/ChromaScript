import assert from "node:assert/strict";
import test from "node:test";
import {
  createHDRPreview,
  hdrVideoCapabilities,
} from "../motion/hdr-preview.mjs";

// Use Safari's measured query combination, not a browser-name allowlist.
const scope = (videoHigh, videoStandard, displayHigh, hevc = true) => ({
  matchMedia(query) {
    return {
      matches:
        {
          "(video-dynamic-range: high)": videoHigh,
          "(video-dynamic-range: standard)": videoStandard,
          "(dynamic-range: high)": displayHigh,
        }[query] ?? false,
    };
  },
  MediaSource: hevc === null ? undefined : { isTypeSupported: () => hevc },
});

test("Safari's unsupported video query falls back to its positive HDR display signal", () => {
  const result = hdrVideoCapabilities(scope(false, false, true));
  assert.equal(result.supported, true);
  assert.equal(result.videoQuerySupported, false);
});

test("a known SDR video plane is not overridden by HDR graphics support", () => {
  assert.equal(hdrVideoCapabilities(scope(false, true, true)).supported, false);
});

test("HDR video plane works even when the graphics plane is SDR", () => {
  assert.equal(hdrVideoCapabilities(scope(true, true, false)).supported, true);
});

test("unknown video query and SDR display stay SDR", () => {
  assert.equal(
    hdrVideoCapabilities(scope(false, false, false)).supported,
    false,
  );
});

for (const hevc of [false, null]) {
  test(`HDR display still requires native HEVC media-source support (${hevc})`, () => {
    const result = hdrVideoCapabilities(scope(false, false, true, hevc));
    assert.equal(result.supported, false);
    assert.match(result.reason, /HEVC/);
  });
}

const until = async (condition) => {
  for (let i = 0; i < 300; i++) {
    if (condition()) return;
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  assert.ok(condition(), "controller did not settle");
};
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

// Simulate native async append/seek events, not HDR pixels. Real HEVC, PQ values
// and compositor continuity are exercised separately by the WebKit regressions.
function harness(t, encodeHook) {
  const nodes = [],
    sources = new Map(),
    revoked = [],
    encoded = [],
    frames = [];
  let serial = 0,
    status,
    playing = false,
    fallbacks = 0;
  class Video extends EventTarget {
    style = {};
    dataset = {};
    readyState = 0;
    paused = true;
    seeking = false;
    time = 0;
    ranges = [];
    removed = false;
    setAttribute() {}
    removeAttribute() {}
    remove() {
      this.removed = true;
    }
    load() {
      this.readyState = 0;
      this.ranges = [];
    }
    requestVideoFrameCallback() {
      return 1;
    }
    cancelVideoFrameCallback() {}
    pause() {
      this.paused = true;
    }
    async play() {
      this.paused = false;
      this.dispatchEvent(new Event("playing"));
    }
    set src(value) {
      this.source = value;
      const source = sources.get(value);
      source.video = this;
      queueMicrotask(() => source.dispatchEvent(new Event("sourceopen")));
    }
    get src() {
      return this.source;
    }
    set currentTime(value) {
      this.time = value;
      this.seeking = true;
      // Expose the native asynchronous boundary for visibility-race coverage.
      this.dispatchEvent(new Event("seeking"));
      queueMicrotask(() => {
        this.seeking = false;
        this.dispatchEvent(new Event("seeked"));
      });
    }
    get currentTime() {
      return this.time;
    }
    get buffered() {
      return {
        length: this.ranges.length,
        start: (i) => this.ranges[i][0],
        end: (i) => this.ranges[i][1],
      };
    }
  }
  class Source extends EventTarget {
    static isTypeSupported() {
      return true;
    }
    addSourceBuffer() {
      const source = this;
      const buffer = new EventTarget();
      buffer.updating = false;
      buffer.appendBuffer = (bytes) => {
        buffer.updating = true;
        queueMicrotask(() => {
          source.video.ranges.push([
            buffer.timestampOffset,
            buffer.timestampOffset + bytes[0] / 60,
          ]);
          source.video.readyState = 2;
          buffer.updating = false;
          buffer.dispatchEvent(new Event("updateend"));
          source.video.dispatchEvent(new Event("loadeddata"));
        });
      };
      buffer.remove = (_, cutoff) => {
        buffer.updating = true;
        queueMicrotask(() => {
          source.video.ranges = source.video.ranges.filter(
            (range) => range[1] > cutoff,
          );
          buffer.updating = false;
          buffer.dispatchEvent(new Event("updateend"));
        });
      };
      return buffer;
    }
  }
  const originals = new Map();
  const replace = (key, value) => {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, value });
  };
  replace("document", {
    createElement: () => {
      const node = new Video();
      nodes.push(node);
      return node;
    },
  });
  replace("MediaSource", Source);
  replace("navigator", {
    mediaCapabilities: { decodingInfo: async () => ({ supported: true }) },
  });
  replace("matchMedia", () =>
    Object.assign(new EventTarget(), { matches: true }),
  );
  replace("cancelAnimationFrame", () => {});
  t.mock.method(URL, "createObjectURL", (source) => {
    const url = `blob:${serial++}`;
    sources.set(url, source);
    return url;
  });
  t.mock.method(URL, "revokeObjectURL", (url) => revoked.push(url));
  const mount = Object.assign(new EventTarget(), { append() {}, dataset: {} });
  const controller = createHDRPreview({
    mount,
    width: 32,
    height: 32,
    config: {
      frames: 600,
      fps: 60,
      frameRate: { numerator: 60, denominator: 1 },
      display: { working_space: { source: "Linear Rec.2020" } },
    },
    capture: async (indices) =>
      indices.map((frame) => new Uint16Array([frame]).buffer),
    encodeChunk: async (header, pixels) => {
      const frame = new Uint16Array(pixels[0])[0];
      encoded.push(frame);
      await encodeHook?.(frame);
      return {
        verification: { strict_decode: true, packet_timing: true },
        output: "PQ",
        mime_type: "video/mp4",
        data: btoa(String.fromCharCode(header.frame_count)),
        timestamp_base: 0,
      };
    },
    onFrame: (frame) => frames.push(frame),
    onStatus: (value) => {
      status = value;
    },
    onPlaying: (value) => {
      playing = value;
    },
    onFallback: () => {
      fallbacks++;
    },
  });
  t.after(() => {
    controller.dispose();
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  return {
    controller,
    encoded,
    frames,
    sources,
    revoked,
    mount,
    nodes,
    get video() {
      return nodes.findLast((node) => node.src && !node.removed);
    },
    get status() {
      return status;
    },
    get playing() {
      return playing;
    },
    get fallbacks() {
      return fallbacks;
    },
  };
}

test("exact seeks coalesce to one current job and the latest target, reusing native MSE", async (t) => {
  const gate = deferred();
  const h = harness(t, (frame) => (frame === 30 ? gate.promise : undefined));
  h.controller.seek(0);
  await until(() => h.frames.at(-1) === 0);
  const node = h.video,
    url = node.src;
  h.controller.seek(30);
  await until(() => h.encoded.includes(30));
  h.controller.seek(60);
  h.controller.seek(90);
  assert.deepEqual(h.encoded, [0, 30]);
  assert.equal(node.style.visibility, "visible");
  gate.resolve();
  await until(() => h.frames.at(-1) === 90);
  h.controller.pause();
  assert.deepEqual(h.encoded, [0, 30, 90]);
  assert.deepEqual(
    h.frames,
    [0, 90],
    "obsolete exact targets are cached, not presented",
  );
  assert.equal(h.video, node);
  assert.equal(node.src, url);
  h.controller.seek(30);
  await until(() => h.frames.at(-1) === 30);
  h.controller.seek(90);
  await until(() => h.frames.at(-1) === 90);
  h.controller.pause();
  assert.deepEqual(
    h.encoded,
    [0, 30, 90],
    "verified fragments and native ranges both reuse work",
  );
  assert.equal(JSON.parse(h.mount.dataset.hdrMetrics).at(-1).cache, "native");
});

test("drag presents completed HDR work before release, then settles only on the final target", async (t) => {
  const first = deferred(),
    second = deferred();
  const h = harness(t, (frame) =>
    frame === 30 ? first.promise : frame === 90 ? second.promise : undefined,
  );
  h.controller.seek(0);
  await until(() => h.frames.at(-1) === 0);
  h.controller.setScrubbing(true);
  h.controller.seek(30);
  await until(() => h.encoded.includes(30));
  h.controller.seek(60);
  h.controller.seek(90);
  first.resolve();
  await until(() => h.frames.includes(30) && h.encoded.includes(90));
  h.controller.seek(180);
  h.controller.setScrubbing(false);
  second.resolve();
  await until(() => h.frames.at(-1) === 180);
  h.controller.pause();
  assert.deepEqual(h.frames, [0, 30, 180]);
  assert.deepEqual(h.encoded, [0, 30, 90, 180]);
  assert.equal(h.playing, false);
  assert.equal(h.status.mode, "hdr");
});

test("pause suppresses neighbour work and dispose releases URLs even with encoding in flight", async (t) => {
  const gate = deferred();
  const h = harness(t, (frame) => (frame === 20 ? gate.promise : undefined));
  h.controller.seek(0);
  await until(() => h.frames.at(-1) === 0);
  h.controller.pause();
  await new Promise((resolve) => setTimeout(resolve, 150));
  assert.deepEqual(h.encoded, [0]);
  h.controller.seek(20);
  await until(() => h.encoded.includes(20));
  h.controller.dispose();
  gate.resolve();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.deepEqual(h.frames, [0]);
  assert.equal(h.controller.enabled, false);
  assert.deepEqual(h.revoked, [...h.sources.keys()]);
  assert.equal(h.fallbacks, 0);
});

test("encoder errors retain the explicit SDR recovery path", async (t) => {
  const h = harness(t, (frame) => {
    if (frame === 20) throw new Error("test encode failure");
  });
  h.controller.seek(0);
  await until(() => h.frames.at(-1) === 0);
  h.controller.seek(20);
  await until(() => h.fallbacks === 1);
  assert.equal(h.status.mode, "sdr");
  assert.equal(h.controller.enabled, false);
  assert.deepEqual(h.revoked, [...h.sources.keys()]);
});

test("play after a paused seek appends a contiguous run and starts only after prebuffering", async (t) => {
  const h = harness(t);
  h.controller.seek(0);
  await until(() => h.frames.at(-1) === 0);
  h.controller.play();
  await until(() => h.video && !h.video.paused);
  assert.equal(h.playing, true);
  assert.equal(h.status.mode, "hdr");
  assert.ok(h.encoded.includes(1));
  h.controller.pause();
});

test("an obsolete frame failure cannot force SDR over a newer target", async (t) => {
  const gate = deferred();
  const h = harness(t, async (frame) => {
    if (frame === 30) {
      await gate.promise;
      throw new Error("old frame failed");
    }
  });
  h.controller.seek(0);
  await until(() => h.frames.at(-1) === 0);
  h.controller.seek(30);
  await until(() => h.encoded.includes(30));
  h.controller.seek(90);
  gate.resolve();
  await until(() => h.frames.at(-1) === 90);
  h.controller.pause();
  assert.equal(h.fallbacks, 0);
  assert.equal(h.status.mode, "hdr");
});

test("hidden work retains intent but starts no new capture until resumed", async (t) => {
  const gate = deferred();
  const h = harness(t, (frame) => (frame === 30 ? gate.promise : undefined));
  h.controller.seek(0);
  await until(() => h.frames.at(-1) === 0);
  h.controller.seek(30);
  await until(() => h.encoded.includes(30));
  h.controller.seek(90);
  h.controller.setActive(false);
  gate.resolve();
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(h.frames, [0]);
  assert.deepEqual(h.encoded, [0, 30]);
  h.controller.setActive(true);
  await until(() => h.frames.at(-1) === 90);
  h.controller.pause();
  assert.deepEqual(h.encoded, [0, 30, 90]);
});

test("a seek completed while hidden is acknowledged on resume before playback", async (t) => {
  const h = harness(t);
  h.controller.seek(0);
  await until(() => h.frames.at(-1) === 0);
  h.controller.pause();
  // Hide after currentTime is assigned but before the native seeked event.
  h.video.addEventListener(
    "seeking",
    () => {
      h.controller.setActive(false);
      h.controller.pause();
    },
    { once: true },
  );
  h.controller.seek(30);
  await until(() => h.encoded.includes(30) && !h.video.seeking);
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(h.frames, [0]);
  h.controller.setActive(true);
  await until(() => h.frames.at(-1) === 30);
  assert.deepEqual(h.encoded, [0, 30], "resume reuses the decoded target");
  h.controller.play();
  await until(() => !h.video.paused);
  assert.equal(h.frames.at(-1), 30, "play starts at the recovered position");
  assert.ok(h.encoded.includes(31));
  h.controller.pause();
});
