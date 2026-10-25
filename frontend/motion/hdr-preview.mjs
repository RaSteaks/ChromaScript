// HDR is presented by the native video compositor. The canvas remains a separate
// SDR fallback; relabelling an 8-bit canvas/captureStream would not preserve HDR.
export function hdrVideoCapabilities(scope = window) {
  const Media = scope.MediaSource;
  const videoHigh = scope.matchMedia("(video-dynamic-range: high)").matches;
  const videoStandard = scope.matchMedia(
    "(video-dynamic-range: standard)",
  ).matches;
  const displayHigh = scope.matchMedia("(dynamic-range: high)").matches;
  const videoQuerySupported = videoHigh || videoStandard;
  // Safari 26.2 reports BOTH video queries false because the feature is absent.
  // Use the general HDR signal only in that case; a known SDR video plane must
  // still win over an HDR graphics plane. Actual HEVC/PQ decoding is checked later.
  const displayHDR = videoQuerySupported ? videoHigh : displayHigh;
  const hevc = !!Media?.isTypeSupported('video/mp4; codecs="hvc1"');
  return {
    supported: displayHDR && hevc,
    displayHDR,
    videoQuerySupported,
    hevc,
    reason: !displayHDR
      ? "浏览器未报告当前屏幕的 HDR 显示能力"
      : !hevc
        ? "浏览器不支持 HEVC 分段视频播放"
        : "",
  };
}

export function hdrVideoSupport(scope = window) {
  return hdrVideoCapabilities(scope).supported;
}

export function createHDRPreview({
  config,
  width,
  height,
  capture,
  mount,
  encodeChunk,
  onStatus,
  onFrame,
  onPlaying,
  onFallback,
}) {
  const rate = config.fps;
  const chunkFrames = Math.max(
    1,
    Math.min(
      64,
      Math.ceil(rate * 1.25),
      Math.floor((192 * 1024 * 1024) / (width * height * 3)),
    ),
  );
  let video, retained, media, buffer, url, mime, opening;
  let wanted = false,
    disposed = false,
    failed = false,
    dragging = false,
    suspended = false;
  let busy = false,
    positioning = false,
    desired = null,
    sequence = 0,
    allowPrefetch = true;
  let timer,
    animation,
    abort = new AbortController();
  let nextSample = 0,
    lastFrame = 0,
    nextFrame = 0,
    prefetch = [];
  const runs = [];
  const callbacks = new WeakMap();
  // This cache belongs to one immutable accepted recipe/raster/color contract.
  // Store verified compressed fragments, not display-converted RGB screenshots.
  const cache = new Map();
  let cacheBytes = 0;
  const cacheLimit = 16 * 1024 * 1024;
  const metrics = [];
  const capabilities = new Set();
  const status = (mode, message) => onStatus({ mode, message });
  const readyStatus = () =>
    status("hdr", `HDR · PQ · ${width} × ${height} · ${rate.toFixed(2)} fps`);
  const pendingStatus = () =>
    status("buffering", "正在定位 HDR 视频，保留上一帧 HDR 画面…");
  const release = (node, objectURL) => {
    node.cancelVideoFrameCallback?.(callbacks.get(node));
    callbacks.delete(node);
    node.pause();
    node.removeAttribute("src");
    node.load();
    node.remove();
    if (objectURL) URL.revokeObjectURL(objectURL);
  };
  const releaseRetained = () => {
    if (retained) release(retained.video, retained.url);
    retained = null;
  };
  const projectFrame = (time) => {
    const sample = Math.floor((time + 1e-6) * rate);
    const run = runs.find(
      (item) => sample >= item.start && sample < item.start + item.count,
    );
    return run ? (run.frame + sample - run.start) % config.frames : null;
  };
  const report = (time) => {
    if (
      positioning ||
      disposed ||
      failed ||
      suspended ||
      video.readyState < 2 ||
      video.style.visibility !== "visible"
    )
      return;
    const frame = projectFrame(time);
    if (frame !== null) {
      lastFrame = frame;
      video.dataset.frame = String(frame);
      onFrame(frame);
    }
  };
  const makeVideo = () => {
    const node = document.createElement("video");
    node.muted = true;
    node.playsInline = true;
    node.preload = "auto";
    node.disableRemotePlayback = true;
    node.setAttribute("aria-label", "720p HDR 视频预览");
    node.style.cssText =
      "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:black;visibility:hidden;pointer-events:none;dynamic-range-limit:no-limit";
    const current = () => node === video && !disposed && !failed;
    const presented = (_, metadata) => {
      if (!current()) return;
      if (wanted) report(metadata.mediaTime);
      callbacks.set(node, node.requestVideoFrameCallback(presented));
    };
    if (node.requestVideoFrameCallback)
      callbacks.set(node, node.requestVideoFrameCallback(presented));
    node.addEventListener("playing", () => {
      if (!current()) return;
      if (!wanted || desired || positioning) {
        node.pause();
        return;
      }
      onPlaying(true);
      readyStatus();
    });
    node.addEventListener("waiting", () => {
      if (current() && wanted && !positioning) {
        node.pause();
        status("buffering", "正在缓冲 720p HDR 视频，保持项目帧率…");
        kick();
      }
    });
    node.addEventListener("error", () => {
      if (current() && node.error) fallback("HDR 视频播放失败");
    });
    mount.append(node);
    return node;
  };
  // A single MSE clock holds multiple source-frame runs. Only an incompatible
  // codec needs a second player; ordinary scrub/step operations keep this DOM.
  const resetMedia = () => {
    if (video?.style.visibility === "visible") {
      releaseRetained();
      retained = { video, url };
    } else if (video) release(video, url);
    video = makeVideo();
    buffer = mime = null;
    nextSample = 0;
    runs.length = 0;
    media = new MediaSource();
    const source = media;
    opening = new Promise((resolve, reject) => {
      const timeout = setTimeout(
        () => finish(new Error("HDR 视频启动超时")),
        10000,
      );
      const cancel = () => finish(new DOMException("cancelled", "AbortError"));
      const opened = () => finish();
      const finish = (error) => {
        clearTimeout(timeout);
        source.removeEventListener("sourceopen", opened);
        abort.signal.removeEventListener("abort", cancel);
        error ? reject(error) : resolve();
      };
      source.addEventListener("sourceopen", opened, { once: true });
      abort.signal.addEventListener("abort", cancel, { once: true });
    });
    // The request can disappear before first append; avoid an unhandled reject.
    opening.catch(() => {});
    url = URL.createObjectURL(media);
    video.src = url;
  };
  const fallback = (message) => {
    if (failed || disposed) return;
    failed = true;
    wanted = false;
    desired = null;
    prefetch = [];
    clearTimeout(timer);
    cancelAnimationFrame(animation);
    abort.abort();
    if (video) release(video, url);
    url = null;
    releaseRetained();
    cache.clear();
    cacheBytes = 0;
    onPlaying(false);
    status("sdr", `${message} · 已回退 sRGB；可重新编译重试。`);
    onFallback();
  };
  const update = (action) =>
    new Promise((resolve, reject) => {
      const source = buffer;
      const timeout = setTimeout(
        () => finish(new Error("HDR 视频分段无法解码")),
        10000,
      );
      const done = () => finish();
      const error = () => finish(new Error("HDR 视频分段无法解码"));
      const cancel = () => finish(new DOMException("cancelled", "AbortError"));
      const finish = (cause) => {
        clearTimeout(timeout);
        source.removeEventListener("updateend", done);
        source.removeEventListener("error", error);
        source.removeEventListener("abort", error);
        abort.signal.removeEventListener("abort", cancel);
        cause ? reject(cause) : resolve();
      };
      source.addEventListener("updateend", done, { once: true });
      source.addEventListener("error", error, { once: true });
      source.addEventListener("abort", error, { once: true });
      abort.signal.addEventListener("abort", cancel, { once: true });
      try {
        action();
      } catch (cause) {
        finish(cause);
      }
    });
  const findCached = (frame) => {
    for (const [key, entry] of [...cache].reverse()) {
      const index = (frame - entry.frame + config.frames) % config.frames;
      if (index < entry.count) {
        cache.delete(key);
        cache.set(key, entry);
        return { entry, index };
      }
    }
    return null;
  };
  const remember = (entry) => {
    const key = `${entry.frame}:${entry.count}`;
    if (cache.has(key)) cacheBytes -= cache.get(key).bytes.byteLength;
    cache.delete(key);
    cache.set(key, entry);
    cacheBytes += entry.bytes.byteLength;
    while (cacheBytes > cacheLimit || cache.size > 64) {
      const oldest = cache.keys().next().value;
      cacheBytes -= cache.get(oldest).bytes.byteLength;
      cache.delete(oldest);
    }
  };
  const encode = async (frame, count, timing) => {
    let began = performance.now();
    const pixels = await capture(
      Array.from({ length: count }, (_, i) => (frame + i) % config.frames),
      abort.signal,
    );
    timing.capture = performance.now() - began;
    if (disposed || failed) throw new DOMException("cancelled", "AbortError");
    began = performance.now();
    const result = await encodeChunk(
      {
        width,
        height,
        frame_count: count,
        input_format: "p010-pq",
        fps: config.frameRate,
        source_color_space: config.display.working_space.source,
      },
      pixels,
    );
    timing.encode = performance.now() - began;
    timing.server = result.timings_ms;
    if (disposed || failed) throw new DOMException("cancelled", "AbortError");
    if (
      !result?.verification?.strict_decode ||
      !result.verification.packet_timing ||
      result.output !== "PQ" ||
      !MediaSource.isTypeSupported(result.mime_type)
    )
      throw new Error("当前浏览器不支持已验证的 HDR 编码");
    const entry = {
      frame,
      count,
      mime: result.mime_type,
      base: result.timestamp_base || 0,
      bytes: Uint8Array.from(atob(result.data), (character) =>
        character.charCodeAt(0),
      ),
    };
    remember(entry);
    return { entry, index: 0 };
  };
  const append = async (entry) => {
    if (!capabilities.has(entry.mime)) {
      if (!navigator.mediaCapabilities?.decodingInfo)
        throw new Error("浏览器无法确认 HDR 解码能力");
      const capability = await navigator.mediaCapabilities.decodingInfo({
        type: "media-source",
        video: {
          contentType: entry.mime,
          width,
          height,
          bitrate: 16000000,
          framerate: rate,
          colorGamut: "rec2020",
          transferFunction: "pq",
        },
      });
      if (!capability.supported) throw new Error("当前设备不支持此 HDR 视频");
      capabilities.add(entry.mime);
    }
    if (disposed || failed) throw new DOMException("cancelled", "AbortError");
    if (!media || (mime && mime !== entry.mime && !buffer.changeType))
      resetMedia();
    await opening;
    if (disposed || failed) throw new DOMException("cancelled", "AbortError");
    if (!buffer) {
      buffer = media.addSourceBuffer(entry.mime);
      buffer.mode = "segments";
    } else if (mime !== entry.mime) buffer.changeType(entry.mime);
    mime = entry.mime;
    const start = nextSample;
    buffer.timestampOffset = start / rate - entry.base;
    await update(() => buffer.appendBuffer(entry.bytes));
    runs.push({ start, frame: entry.frame, count: entry.count });
    nextSample += entry.count;
    return start;
  };
  const bufferedTarget = (frame) => {
    if (!video || video.readyState < 2) return null;
    let target = null;
    for (const run of runs)
      for (let i = 0; i < run.count; i++) {
        if ((run.frame + i) % config.frames !== frame) continue;
        const time = (run.start + i) / rate;
        for (let j = 0; j < video.buffered.length; j++)
          if (
            video.buffered.start(j) <= time + 1e-6 &&
            video.buffered.end(j) >= time + 1 / rate - 1e-6
          ) {
            if (
              target === null ||
              Math.abs(time - video.currentTime) <
                Math.abs(target / rate - video.currentTime)
            )
              target = run.start + i;
          }
      }
    return target;
  };
  const show = async (sample, request) => {
    positioning = true;
    const target = sample / rate,
      node = video;
    try {
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(
          () => finish(new Error("HDR 视频定位超时")),
          10000,
        );
        const cancel = () =>
          finish(new DOMException("cancelled", "AbortError"));
        const finish = (error) => {
          clearTimeout(timeout);
          node.removeEventListener("loadeddata", check);
          node.removeEventListener("seeked", check);
          abort.signal.removeEventListener("abort", cancel);
          error ? reject(error) : resolve();
        };
        const check = () => {
          if (
            !node.seeking &&
            node.readyState >= 2 &&
            Math.abs(node.currentTime - target) < 1 / rate
          )
            finish();
        };
        node.addEventListener("loadeddata", check);
        node.addEventListener("seeked", check);
        abort.signal.addEventListener("abort", cancel, { once: true });
        node.currentTime = target;
        check();
      });
    } finally {
      positioning = false;
    }
    // Visibility may change while native seeked is pending. Keep the target
    // queued until report can acknowledge it and advance the visible/audio clock.
    if (
      disposed ||
      failed ||
      suspended ||
      (!dragging && desired?.frame !== request.frame)
    )
      return false;
    node.style.visibility = "visible";
    releaseRetained();
    report(node.currentTime);
    return true;
  };
  const trim = async () => {
    // Keep six seconds of native history and a bounded compressed LRU outside
    // it. Never evict the surface currently retained for presentation.
    const cutoff = Math.max(
      0,
      Math.min(video.currentTime - 2, nextSample / rate - 6),
    );
    if (
      cutoff > 0 &&
      video.buffered.length &&
      video.buffered.start(0) < cutoff
    ) {
      await update(() => buffer.remove(0, cutoff));
      while (runs.length && (runs[0].start + runs[0].count) / rate <= cutoff)
        runs.shift();
    }
  };
  const record = (request, timing) => {
    const item = {
      frame: request.frame,
      dragging,
      ...timing,
      total: performance.now() - request.at,
    };
    metrics.push(item);
    if (metrics.length > 64) metrics.shift();
    mount.dataset.hdrMetrics = JSON.stringify(metrics);
    mount.dataset.hdrCacheBytes = String(cacheBytes);
    mount.dispatchEvent(
      new CustomEvent("hdr-preview-sample", { bubbles: true, detail: item }),
    );
  };
  const startPlayback = async () => {
    const node = video;
    try {
      await node.play();
      if (node !== video || !wanted || disposed || desired) node.pause();
    } catch {
      // A seek/codec handoff can reject an older play promise. It must not
      // overwrite the newer target's buffering state or stop its player.
      if (node === video && wanted && !desired && !disposed && !failed) {
        wanted = false;
        onPlaying(false);
        readyStatus();
      }
    }
  };
  const run = async () => {
    if (busy || disposed || failed || suspended) return;
    busy = true;
    const activeFrame = desired?.frame ?? nextFrame;
    try {
      if (desired) {
        const request = desired;
        const timing = {
          queue: performance.now() - request.at,
          capture: 0,
          encode: 0,
          decode: 0,
          cache: "native",
        };
        let sample = request.playStart ? null : bufferedTarget(request.frame);
        let next = (request.frame + 1) % config.frames;
        if (sample === null) {
          let hit = findCached(request.frame);
          timing.cache = hit ? "encoded" : "miss";
          if (!hit) hit = await encode(request.frame, 1, timing);
          // During drag, present completed work so updates cannot starve. After
          // release/keyboard input, only the latest exact target may replace it.
          if (suspended || (!dragging && desired?.frame !== request.frame))
            return;
          const began = performance.now();
          sample = (await append(hit.entry)) + hit.index;
          timing.decode = performance.now() - began;
          next = (hit.entry.frame + hit.entry.count) % config.frames;
        }
        if (
          disposed ||
          failed ||
          suspended ||
          (!dragging && desired?.frame !== request.frame)
        )
          return;
        const began = performance.now();
        if (!(await show(sample, request))) return;
        timing.decode += performance.now() - began;
        // Input may arrive during native seek. Keep its target queued; never
        // overwrite intent with this older acknowledgement.
        record(request, timing);
        if (
          desired?.id === request.id ||
          (desired?.frame === request.frame && !desired.playStart)
        ) {
          desired = null;
          nextFrame = next;
          if (!wanted) {
            readyStatus();
            if (!dragging && allowPrefetch)
              prefetch = [1, 2].map((i) => (request.frame + i) % config.frames);
          }
        } else pendingStatus();
        await trim();
      } else if (wanted) {
        const ahead = nextSample / rate - video.currentTime;
        if (ahead >= 2 && video.paused) void startPlayback();
        if (ahead < 4) {
          const timing = {};
          const hit = await encode(nextFrame, chunkFrames, timing);
          if (desired || !wanted || disposed || failed || suspended) return;
          await append(hit.entry);
          nextFrame = (nextFrame + chunkFrames) % config.frames;
          await trim();
          if (nextSample / rate - video.currentTime >= 2 && video.paused)
            void startPlayback();
        }
      } else if (!dragging && prefetch.length) {
        // At most two adjacent paused frames; lower priority than any user seek.
        // pause/hidden/dispose stops new background work, with no polling loop.
        const frame = prefetch.shift();
        if (!findCached(frame)) {
          try {
            await encode(frame, 1, {});
          } catch {
            prefetch = [];
          } // Optional neighbours must not fail the displayed frame.
        }
      }
    } catch (cause) {
      // A frame-specific failure from a superseded target must not force SDR
      // over a newer valid request. Retry that latest target through the normal
      // single-flight loop; a failure of the latest target still falls back.
      if (!disposed && !failed && (!desired || desired.frame === activeFrame))
        fallback(cause.message || "HDR 预览失败");
    } finally {
      busy = false;
      if (
        !disposed &&
        !failed &&
        !suspended &&
        (desired || wanted || prefetch.length)
      )
        kick(desired ? 0 : wanted ? 30 : 120);
    }
  };
  function kick(delay = 0) {
    clearTimeout(timer);
    if (suspended) return;
    timer = setTimeout(() => void run(), delay);
  }
  const seek = (frame) => {
    if (disposed || failed) return;
    wanted = false;
    allowPrefetch = true;
    video?.pause();
    onPlaying(false);
    prefetch = [];
    // One executing job plus one replaceable target. Pointer events never abort
    // the encoder or allocate a queue of captured pixel batches.
    desired = { frame, at: performance.now(), id: ++sequence };
    pendingStatus();
    kick();
  };
  const displays = [
    "(video-dynamic-range: high)",
    "(video-dynamic-range: standard)",
    "(dynamic-range: high)",
  ].map((query) => matchMedia(query));
  const displayChanged = () => {
    if (!hdrVideoCapabilities().displayHDR)
      fallback("浏览器已不再报告当前屏幕的 HDR 显示能力");
  };
  for (const display of displays)
    display.addEventListener("change", displayChanged);
  // Keep rAF only as a compatibility clock; paused seeks use decoded events.
  const tick = () => {
    if (video && wanted) report(video.currentTime);
    if (!disposed && !failed) animation = requestAnimationFrame(tick);
  };
  if (!document.createElement("video").requestVideoFrameCallback)
    animation = requestAnimationFrame(tick);
  return {
    get enabled() {
      return !failed && !disposed;
    },
    seek,
    setActive(value) {
      suspended = !value;
      if (suspended) {
        // Keep only intent/cache when hidden. Resume may reuse the completed
        // in-flight encode, but no new captures or playback start offscreen.
        wanted = false;
        dragging = false;
        prefetch = [];
        clearTimeout(timer);
        video?.pause();
        onPlaying(false);
      } else if (desired) kick();
    },
    setScrubbing(value) {
      dragging = value;
      if (!value && desired) kick();
    },
    play() {
      if (disposed || failed) return;
      wanted = true;
      onPlaying(true);
      prefetch = [];
      // Append a contiguous playback run after any random-access samples. It
      // prevents playback from crossing unrelated scrub frames in the MSE clock.
      desired = {
        frame: desired?.frame ?? lastFrame,
        at: performance.now(),
        id: ++sequence,
        playStart: true,
      };
      status("buffering", "正在缓冲 720p HDR 视频，保持项目帧率…");
      kick();
    },
    pause() {
      wanted = false;
      allowPrefetch = false;
      prefetch = [];
      video?.pause();
      onPlaying(false);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      wanted = false;
      desired = null;
      prefetch = [];
      clearTimeout(timer);
      cancelAnimationFrame(animation);
      abort.abort();
      if (video) release(video, url);
      releaseRetained();
      cache.clear();
      runs.length = 0;
      for (const display of displays)
        display.removeEventListener("change", displayChanged);
    },
  };
}
