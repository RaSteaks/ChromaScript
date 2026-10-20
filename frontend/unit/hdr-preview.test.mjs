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

test("first-frame readiness never enables audio before native HDR playback", (t) => {
  class Video extends EventTarget {
    style = {};
    dataset = {};
    currentTime = 0;
    readyState = 0;
    paused = true;
    setAttribute() {}
    removeAttribute() {}
    remove() {}
    requestVideoFrameCallback() {}
    pause() {
      this.paused = true;
    }
    load() {
      this.readyState = 0;
    }
    async play() {
      this.paused = false;
    }
  }
  const video = new Video();
  const originals = new Map();
  const replace = (key, value) => {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, value });
  };
  replace("document", { createElement: () => video });
  replace("MediaSource", class extends EventTarget {});
  replace("matchMedia", () =>
    Object.assign(new EventTarget(), { matches: true }),
  );
  replace("cancelAnimationFrame", () => {});
  t.mock.method(URL, "createObjectURL", () => "blob:hdr-test");
  t.mock.method(URL, "revokeObjectURL", () => {});
  let playing = false;
  let status;
  const controller = createHDRPreview({
    mount: { append() {} },
    width: 1280,
    height: 720,
    config: {
      frames: 600,
      fps: 60,
      frameRate: { numerator: 60, denominator: 1 },
      display: { working_space: { source: "Linear Rec.2020" } },
    },
    capture: async () => [],
    encodeChunk: async () => ({}),
    onFrame() {},
    onFallback() {},
    onPlaying: (value) => {
      playing = value;
    },
    onStatus: (value) => {
      status = value;
    },
  });
  // This is the same gate MotionPreview supplies to PreviewAudio. Media events
  // are controlled here to hold the gap between first-frame decode and playback.
  const audioEnabled = () => playing && status.mode !== "buffering";
  const firstFrame = () => {
    video.readyState = 2;
    video.dispatchEvent(new Event("loadeddata"));
  };
  const nativePlaying = () => {
    video.paused = false;
    video.dispatchEvent(new Event("playing"));
  };
  try {
    controller.seek(0);
    controller.play();
    firstFrame();
    assert.equal(video.paused, true);
    assert.equal(audioEnabled(), false, "early Play must keep audio buffered");
    nativePlaying();
    assert.equal(audioEnabled(), true);

    controller.seek(120);
    firstFrame();
    assert.equal(video.paused, true);
    assert.equal(
      audioEnabled(),
      false,
      "a seek while playing must rebuffer audio",
    );
    nativePlaying();
    assert.equal(audioEnabled(), true);

    controller.pause();
    controller.seek(180);
    firstFrame();
    assert.equal(
      status.mode,
      "hdr",
      "a paused poster should still be shown as ready",
    );
    assert.equal(audioEnabled(), false);
  } finally {
    controller.dispose();
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});
