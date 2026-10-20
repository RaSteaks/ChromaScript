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
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  video.disableRemotePlayback = true;
  video.setAttribute("aria-label", "720p HDR 视频预览");
  video.style.cssText =
    "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:black;visibility:hidden;pointer-events:none;dynamic-range-limit:no-limit";
  mount.append(video);
  // Every original frame is rendered; the source rational timebase is unchanged.
  const rate = config.fps;
  const chunkFrames = Math.max(
    1,
    Math.min(
      64,
      Math.ceil(rate * 1.25),
      Math.floor((192 * 1024 * 1024) / (width * height * 3)),
    ),
  );
  let generation = 0,
    origin = 0,
    rendered = 0;
  let wanted = false,
    failed = false,
    disposed = false;
  let media, buffer, url, pumpTimer, watchdog, animation;
  let encoding = Promise.resolve();
  let captureAbort = new AbortController();
  let prefetched = null;
  let rebuffering = true;
  const startBuffer = 2;
  const maxAhead = 4;
  const status = (mode, message) => onStatus({ mode, message });
  const clear = () => {
    generation++;
    captureAbort.abort();
    captureAbort = new AbortController();
    prefetched = null;
    rebuffering = true;
    clearTimeout(pumpTimer);
    clearTimeout(watchdog);
    video.pause();
    video.removeAttribute("src");
    video.load();
    if (url) URL.revokeObjectURL(url);
    url = null;
    media = buffer = null;
    video.style.visibility = "hidden";
  };
  const fallback = (message) => {
    if (failed || disposed) return;
    failed = true;
    cancelAnimationFrame(animation);
    wanted = false;
    clear();
    onPlaying(false);
    status("sdr", `${message} · 已回退 sRGB；可重新编译重试。`);
    onFallback();
  };
  const encode = (header, frames, token) => {
    // A seek can invalidate an in-flight fragment, but never starts a second
    // local encoder while that request still owns CPU/memory.
    const work = encoding
      .catch(() => {})
      .then(() => {
        if (token !== generation || disposed) throw new Error("superseded");
        const began = performance.now();
        return encodeChunk(header, frames).then((result) => {
          video.dataset.encodeMs = String(performance.now() - began);
          return result;
        });
      });
    encoding = work;
    return work;
  };
  const append = (source, bytes) =>
    new Promise((resolve, reject) => {
      const timeout = setTimeout(() => error(), 10000);
      const done = () => {
        clearTimeout(timeout);
        source.removeEventListener("updateend", complete);
        source.removeEventListener("error", error);
        source.removeEventListener("abort", error);
      };
      const complete = () => {
        done();
        resolve();
      };
      const error = () => {
        done();
        reject(new Error("HDR 视频分段无法解码"));
      };
      source.addEventListener("updateend", complete, { once: true });
      source.addEventListener("error", error, { once: true });
      source.addEventListener("abort", error, { once: true });
      try {
        source.appendBuffer(bytes);
      } catch (cause) {
        done();
        reject(cause);
      }
    });
  const startPlayback = async () => {
    const token = generation;
    try {
      await video.play();
      if (token !== generation || disposed) return;
      if (!wanted) video.pause();
    } catch {
      if (token !== generation || disposed) return;
      wanted = false;
      onPlaying(false);
      status("hdr", "HDR 视频已就绪，请点击播放");
    }
  };
  const captureBatch = async (start, count, token) => {
    if (token !== generation || disposed || (start > 0 && !wanted)) return null;
    const signal = captureAbort.signal;
    const began = performance.now();
    try {
      const frames = await capture(
        Array.from(
          { length: count },
          (_, i) => (origin + start + i) % config.frames,
        ),
        signal,
      );
      video.dataset.captureMs = String(performance.now() - began);
      return frames;
    } catch (cause) {
      if (signal.aborted) return null;
      throw cause;
    }
  };
  const pump = async (token) => {
    if (token !== generation || failed || disposed) return;
    try {
      const ahead = rendered / rate - video.currentTime;
      if ((rendered && !wanted) || ahead > maxAhead || buffer?.updating) {
        pumpTimer = setTimeout(() => void pump(token), 50);
        return;
      }
      const start = rendered;
      // A paused seek only needs one sharp HDR frame. Playback uses larger
      // fragments to amortize hardware startup and strict decode verification.
      const count = !rendered && !wanted ? 1 : chunkFrames;
      let frames;
      if (prefetched?.start === start) {
        const batch = prefetched;
        prefetched = null;
        const ready = await batch.promise;
        if (ready?.error) throw ready.error;
        frames = ready?.frames;
      }
      if (!frames) frames = await captureBatch(start, count, token);
      if (token !== generation || disposed) return;
      if (!frames) {
        pumpTimer = setTimeout(() => void pump(token), 50);
        return;
      }
      const encodingResult = encode(
        {
          width,
          height,
          frame_count: frames.length,
          input_format: "p010-pq",
          fps: config.frameRate,
          source_color_space: config.display.working_space.source,
        },
        frames,
        token,
      );
      if (wanted && ahead < maxAhead) {
        const next = start + frames.length;
        // At most one binary batch ahead. GPU capture overlaps native encoding;
        // cancellation is generation-checked before each frame and before append.
        prefetched = {
          start: next,
          promise: captureBatch(next, chunkFrames, token).then(
            (frames) => ({ frames }),
            (error) => ({ error }),
          ),
        };
      }
      const result = await encodingResult;
      if (token !== generation || disposed) return;
      if (
        !result?.verification?.strict_decode ||
        result.output !== "PQ" ||
        !MediaSource.isTypeSupported(result.mime_type)
      )
        throw new Error("当前浏览器不支持已验证的 HDR 编码");
      if (!buffer) {
        if (!navigator.mediaCapabilities?.decodingInfo)
          throw new Error("浏览器无法确认 HDR 解码能力");
        const capability = await navigator.mediaCapabilities.decodingInfo({
          type: "media-source",
          video: {
            contentType: result.mime_type,
            width,
            height,
            bitrate: 16000000,
            framerate: rate,
            colorGamut: "rec2020",
            transferFunction: "pq",
          },
        });
        if (token !== generation || disposed) return;
        if (!capability.supported) throw new Error("当前设备不支持此 HDR 视频");
        buffer = media.addSourceBuffer(result.mime_type);
        buffer.mode = "segments";
      }
      // Each fragment starts at zero and carries its own initialization segment.
      // No B frames: its decode/presentation times share the exact sample clock.
      buffer.timestampOffset = start / rate - (result.timestamp_base || 0);
      const bytes = Uint8Array.from(atob(result.data), (character) =>
        character.charCodeAt(0),
      );
      await append(buffer, bytes);
      if (token !== generation || disposed) return;
      rendered += frames.length;
      if (video.currentTime > 3 && !buffer.updating)
        buffer.remove(0, video.currentTime - 2);
      if (
        wanted &&
        video.paused &&
        rendered / rate - video.currentTime >= startBuffer
      ) {
        rebuffering = false;
        void startPlayback();
      }
      pumpTimer = setTimeout(() => void pump(token), 0);
    } catch (cause) {
      if (token === generation) fallback(cause.message || "HDR 预览失败");
    }
  };
  const reportFrame = (time) => {
    if (video.readyState < 2 || video.style.visibility !== "visible") return;
    onFrame((origin + Math.floor((time + 1e-6) * rate)) % config.frames);
  };
  const presented = (_, metadata) => {
    if (disposed || failed) return;
    reportFrame(metadata.mediaTime);
    video.requestVideoFrameCallback(presented);
  };
  if (video.requestVideoFrameCallback)
    video.requestVideoFrameCallback(presented);
  else {
    const tick = () => {
      if (!video.paused) reportFrame(video.currentTime);
      if (!disposed && !failed) animation = requestAnimationFrame(tick);
    };
    animation = requestAnimationFrame(tick);
  }
  video.addEventListener("loadeddata", () => {
    if (failed || disposed) return;
    clearTimeout(watchdog);
    video.style.visibility = "visible";
    // A decoded poster is ready while paused, but requested playback may still
    // be prebuffering. Only native "playing" may enable the host's audio clock.
    if (!wanted)
      status("hdr", `HDR · PQ · ${width} × ${height} · ${rate.toFixed(2)} fps`);
  });
  video.addEventListener("playing", () => {
    if (!wanted) {
      video.pause();
      return;
    }
    onPlaying(true);
    status("hdr", `HDR · PQ · ${width} × ${height} · ${rate.toFixed(2)} fps`);
  });
  video.addEventListener("waiting", () => {
    if (wanted) {
      // Keep Pause available while buffering; the host separately pauses audio
      // from this status so its clock cannot run ahead of stalled video.
      rebuffering = true;
      video.pause();
      status("buffering", "正在缓冲 720p HDR 视频，保持项目帧率…");
    }
  });
  video.addEventListener("error", () => {
    if (video.error && !disposed) fallback("HDR 视频播放失败");
  });
  const seek = (frame) => {
    clear();
    origin = frame;
    rendered = 0;
    onPlaying(wanted);
    const token = generation;
    status("buffering", "正在准备 720p HDR 视频…");
    media = new MediaSource();
    media.addEventListener("sourceopen", () => void pump(token), {
      once: true,
    });
    url = URL.createObjectURL(media);
    video.src = url;
    watchdog = setTimeout(() => {
      if (token === generation) fallback("HDR 视频启动超时");
    }, 50000);
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
  return {
    get enabled() {
      return !failed;
    },
    seek,
    play() {
      wanted = true;
      onPlaying(true);
      if (
        video.readyState >= 2 &&
        (!rebuffering || rendered / rate - video.currentTime >= startBuffer)
      )
        void startPlayback();
      else status("buffering", "正在缓冲 720p HDR 视频，保持项目帧率…");
    },
    pause() {
      wanted = false;
      captureAbort.abort();
      captureAbort = new AbortController();
      video.pause();
      onPlaying(false);
    },
    dispose() {
      disposed = true;
      cancelAnimationFrame(animation);
      clear();
      for (const display of displays)
        display.removeEventListener("change", displayChanged);
      video.remove();
    },
  };
}
