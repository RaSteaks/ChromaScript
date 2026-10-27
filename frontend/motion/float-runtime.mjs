import * as THREE from "three";
import {
  sourceSpace,
  workingColorSpace,
  registerSpace,
  assertWorkingSpace,
} from "./source-color.mjs";
import createScene from "chromascript:scene";
import config from "chromascript:config";
import { createHDRCapture } from "./hdr-capture.mjs";
import { frameSamples } from "./time-sampling.mjs";
import { createVideoCapture } from "./video-capture.mjs";

function encodeBytes(bytes) {
  if (typeof bytes.toBase64 === "function") return bytes.toBase64();
  let binary = "";
  for (let i = 0; i < bytes.length; i += 32768)
    binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
  return btoa(binary);
}

// Only this bridge owns the renderer. Source supplies a scene, camera and pure
// frame update, never a display canvas, screenshot, previous-frame accumulator.
const send = (type, value = null) =>
  parent.postMessage({ channel: config.channel, type, value }, "*");
const vertex = `in vec3 position; in vec2 uv; out vec2 texcoord;
void main(){texcoord=uv; gl_Position=vec4(position,1.0);}`;
const quadCamera = new THREE.Camera();
const quadGeometry = new THREE.PlaneGeometry(2, 2);
const floatTarget = (w, h) =>
  new THREE.WebGLRenderTarget(w, h, {
    type: THREE.FloatType,
    format: THREE.RGBAFormat,
    internalFormat: "RGBA32F",
    minFilter: THREE.NearestFilter,
    magFilter: THREE.NearestFilter,
    colorSpace: workingColorSpace,
    depthBuffer: true,
    samples: 0,
  });
function material(fragmentShader, uniforms = {}) {
  return new THREE.RawShaderMaterial({
    glslVersion: THREE.GLSL3,
    vertexShader: vertex,
    fragmentShader: `precision highp float; precision highp sampler2D; in vec2 texcoord;
    out vec4 fragColor; ${fragmentShader}`,
    uniforms,
    depthTest: false,
    depthWrite: false,
    blending: THREE.NoBlending,
    toneMapped: false,
  });
}

const shaderAlphaGuards = new WeakMap();
function opaqueFragment(source, glslVersion) {
  // Wrap main rather than rewriting assignments: early returns still force opaque
  // coverage, while discard still discards the fragment. RGB remains untouched.
  const clean = source.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");
  let output = "gl_FragColor";
  if (glslVersion === THREE.GLSL3 || /^\s*#version\s+300\s+es/m.test(clean)) {
    const outputs = [
      ...clean.matchAll(
        /(?:layout\s*\(([^)]*)\)\s*)?\bout\s+(?:(?:lowp|mediump|highp)\s+)?(\w+)\s+([^;{}]+);/g,
      ),
    ].filter((match) => {
      // Function prototypes may also contain `out vec4`; only global variables
      // name framebuffer outputs. Comments were removed before counting scopes.
      let depth = 0;
      for (const character of clean.slice(0, match.index)) {
        if (character === "(" || character === "{") depth++;
        if (character === ")" || character === "}") depth--;
      }
      return depth === 0;
    });
    const declaration = outputs[0];
    if (
      outputs.length !== 1 ||
      declaration[2] !== "vec4" ||
      !/^[A-Za-z_]\w*$/.test(declaration[3].trim()) ||
      (declaration[1] &&
        !declaration[1]
          .split(",")
          .every((value) => /^\s*(?:location|index)\s*=\s*0\s*$/.test(value)))
    )
      throw new Error(
        "不透明 HDR 自定义着色器须有唯一 vec4 颜色输出；其他输出形式请使用 transparent:true 和 NormalBlending。",
      );
    output = declaration[3].trim();
  }
  // Raw shaders may own their #version directive, which must remain first.
  const version =
    source.match(
      /^(?:(?:\s|\/\*[\s\S]*?\*\/|\/\/[^\n]*\n))*#version[^\n]*(?:\n|$)/,
    )?.[0] || "";
  return `${version}#define main chromascriptOpaqueMain\n${source.slice(version.length)}
#undef main
void main(){chromascriptOpaqueMain(); ${output}.a=1.0;}`;
}

function guardShaderAlpha(m) {
  if (!m.isShaderMaterial) return;
  const previous = shaderAlphaGuards.get(m);
  if (
    previous &&
    m.onBeforeCompile === previous.compile &&
    m.customProgramCacheKey === previous.key
  ) {
    // Three does not automatically invalidate a program when transparent changes.
    if (previous.transparent !== m.transparent) {
      previous.transparent = m.transparent;
      m.needsUpdate = true;
    }
    return;
  }
  const originalCompile =
    m.onBeforeCompile === previous?.compile
      ? previous.originalCompile
      : m.onBeforeCompile;
  const originalKey =
    m.customProgramCacheKey === previous?.key
      ? previous.originalKey
      : m.customProgramCacheKey;
  const compile = function (shader, renderer) {
    originalCompile.call(this, shader, renderer);
    if (!this.transparent)
      shader.fragmentShader = opaqueFragment(
        shader.fragmentShader,
        this.glslVersion,
      );
  };
  const key = function () {
    return `${originalKey.call(this)}:${originalCompile.toString()}:chroma-alpha-${this.transparent}`;
  };
  shaderAlphaGuards.set(m, {
    compile,
    key,
    originalCompile,
    originalKey,
    transparent: m.transparent,
  });
  m.onBeforeCompile = compile;
  m.customProgramCacheKey = key;
  // Only the compiled copy is wrapped; authored source, draw ordering and depth
  // behavior stay intact. Normal transparent blending associates RGB as before.
  m.needsUpdate = true;
}

function main() {
  const renderer = new THREE.WebGLRenderer({
    alpha: true,
    antialias: false,
    premultipliedAlpha: false,
  });
  renderer.debug.onShaderError = (
    context,
    program,
    vertexShader,
    fragmentShader,
  ) => {
    throw new Error(
      "GPU 着色器编译失败：" +
        context.getProgramInfoLog(program) +
        " " +
        context.getShaderInfoLog(fragmentShader) +
        " " +
        context.getShaderInfoLog(vertexShader),
    );
  };
  renderer.toneMapping = THREE.NoToneMapping;
  renderer.outputColorSpace = workingColorSpace;
  renderer.setClearColor(0, 0);
  const gl = renderer.getContext();
  if (!gl.getExtension("EXT_color_buffer_float"))
    throw new Error("GPU 不支持浮点渲染目标；无法生成 HDR 母版。");
  // Exercise storage and readback, including a value distinguishable from half
  // float, negative RGB and >1 radiance. Names/extensions alone prove nothing.
  const debugInfo = gl.getExtension("WEBGL_debug_renderer_info");
  const floatBlending = !!gl.getExtension("EXT_float_blend");
  const probeTarget = floatTarget(1, 1);
  const probeMaterial = material(
    "void main(){fragColor=vec4(-0.25,0.1234567,4.125,0.5);}",
  );
  const quad = new THREE.Mesh(quadGeometry, probeMaterial);
  renderer.setRenderTarget(probeTarget);
  renderer.render(quad, quadCamera);
  const probe = new Float32Array(4);
  renderer.readRenderTargetPixels(probeTarget, 0, 0, 1, 1, probe);
  const expected = [-0.25, 0.1234567, 4.125, 0.5];
  if (
    gl.getError() !== gl.NO_ERROR ||
    expected.some((v, i) => Math.abs(v - probe[i]) > 1e-6)
  )
    throw new Error(
      "GPU 浮点渲染/读取验证失败；不会降级为 8-bit 或 half float。",
    );
  probeTarget.dispose();
  probeMaterial.dispose();
  const limit = config.maxDimension || Math.max(config.width, config.height);
  // Match live WGSL's orientation-aware 720p budget; final rendering is unchanged.
  const useHDR =
    config.preview &&
    config.hdrPreview &&
    sourceSpace !== "Linear Rec.709 (sRGB)" &&
    Math.min(config.width, config.height) *
      Math.min(
        1,
        1280 / Math.max(config.width, config.height),
        720 / Math.min(config.width, config.height),
      ) >=
      16;
  const scale = useHDR
    ? Math.min(
        1,
        1280 / Math.max(config.width, config.height),
        720 / Math.min(config.width, config.height),
      )
    : config.preview
      ? Math.min(
          1,
          1280 / Math.max(config.width, config.height),
          720 / Math.min(config.width, config.height),
        )
      : Math.min(1, limit / Math.max(config.width, config.height));
  // Logical scene dimensions/time never change with preview or final sampling.
  // Export uses an integer grid. Master/reference and GPU video paths both
  // integrate associated light before any display transform.
  const renderScale = config.preview ? 1 : config.renderScale || 1;
  // Video chroma requires even rasters; logical layout and source time are unchanged.
  const raster = (value) =>
    useHDR
      ? Math.max(2, Math.floor(value / 2) * 2)
      : Math.max(1, Math.round(value));
  const width = raster(config.width * scale) * renderScale;
  const height = raster(config.height * scale) * renderScale;
  const viewport = gl.getParameter(gl.MAX_VIEWPORT_DIMS);
  const maxTarget = Math.min(
    gl.getParameter(gl.MAX_TEXTURE_SIZE),
    gl.getParameter(gl.MAX_RENDERBUFFER_SIZE),
  );
  if (
    width > Math.min(maxTarget, viewport[0]) ||
    height > Math.min(maxTarget, viewport[1])
  )
    throw new Error(
      `当前 GPU 不支持 ${width} × ${height} 的采样尺寸；请降低超采样倍率或尺寸。`,
    );
  renderer.setSize(width, height, false);
  renderer.domElement.style.cssText =
    "width:100%;height:100%;object-fit:contain;display:block";
  document.getElementById("root").appendChild(renderer.domElement);
  const target = floatTarget(width, height);
  const context = {
    // Source literals are already in this linear space; no author-side conversion.
    sourceColorSpace: sourceSpace,
    workingColorSpace,
    width: config.width,
    height: config.height,
    frames: config.frames,
    fps: config.fps,
    frameRate: config.frameRate,
    // Pixel-coordinate shaders may normalize gl_FragCoord using the actual
    // raster, while geometry/layout continue to use logical width/height.
    rasterWidth: width,
    rasterHeight: height,
  };
  const checkedScene = (value) => {
    if (
      !value?.scene?.isScene ||
      !value?.camera?.isCamera ||
      typeof value.update !== "function"
    )
      throw new Error(
        "HDR 代码须默认导出工厂函数，返回 {scene, camera, update({frame, time})}。",
      );
    return value;
  };
  const scene = checkedScene(createScene(context));
  const layers = (window.__chromaLayers || []).map((layer, index) => {
    const contract = config.display.layers[index];
    const space = registerSpace(contract);
    const toTarget = new THREE.Matrix3()
      .set(...config.display.working_space.to_linear_rec709.flat())
      .invert()
      .multiply(new THREE.Matrix3().set(...contract.to_linear_rec709.flat()));
    return {
      ...layer,
      space,
      sourceSpace: contract.source,
      toTarget,
      scene: checkedScene(
        layer.factory({
          ...context,
          sourceColorSpace: contract.source,
          workingColorSpace: space,
        }),
      ),
    };
  });
  const scratch = layers.length ? floatTarget(width, height) : null;
  const composite = layers.length
    ? material(
        `uniform sampler2D source; uniform mat3 conversion;
    void main(){vec4 p=texture(source,texcoord);fragColor=vec4(conversion*p.rgb,p.a);}`,
        {
          source: { value: scratch.texture },
          conversion: { value: new THREE.Matrix3() },
        },
      )
    : null;
  if (composite) {
    // Associated light is converted/composited linearly; coverage is untouched.
    composite.transparent = true;
    composite.premultipliedAlpha = true;
    composite.blending = THREE.NormalBlending;
  }
  const uniforms = { source: { value: target.texture } };
  const lookupTextures = [];
  for (const t of config.display.textures) {
    // Expand OCIO RGB tables to RGBA for portable WebGL2 float texture upload.
    const values = new Float32Array(t.width * t.height * 4);
    for (let i = 0; i < t.width * t.height; i++) {
      for (let c = 0; c < t.channels; c++)
        values[i * 4 + c] = t.values[i * t.channels + c];
      values[i * 4 + 3] = 1;
    }
    const texture = new THREE.DataTexture(
      values,
      t.width,
      t.height,
      THREE.RGBAFormat,
      THREE.FloatType,
    );
    texture.minFilter = texture.magFilter = t.linear
      ? THREE.LinearFilter
      : THREE.NearestFilter;
    texture.needsUpdate = true;
    lookupTextures.push(texture);
    uniforms[t.name] = { value: texture };
  }
  const displayMaterial = material(
    `uniform sampler2D source;
    ${config.display.shader}
    void main(){
      vec4 p = texture(source, texcoord);
      vec3 straight = p.a > 0.0 ? p.rgb / p.a : vec3(0.0);
      // OCIO receives RGB only; alpha bypasses the display transform completely.
      fragColor = vec4(ocioDisplay(vec4(straight,1.0)).rgb,p.a);
    }`,
    uniforms,
  );
  const hdrCapture = useHDR
    ? createHDRCapture(renderer, target, config.display.hdr, width, height)
    : null;
  const videoCapture = config.videoCapture
    ? createVideoCapture(
        renderer,
        target,
        config.videoCapture,
        config.width,
        config.height,
        renderScale,
      )
    : null;
  window.addEventListener("pagehide", () => hdrCapture?.dispose(), {
    once: true,
  });
  window.addEventListener("pagehide", () => videoCapture?.dispose(), {
    once: true,
  });
  let data = null;
  function validateMaterials(value, space) {
    // Browser texture unpack cannot convert arbitrary primaries/AP1 white points.
    // Fail explicitly for tagged foreign textures in new spaces. Linear numeric
    // DataTextures may use NoColorSpace with samples already in the selected basis.
    const texture = (t) => {
      if (
        space !== THREE.LinearSRGBColorSpace &&
        t?.isTexture &&
        t.colorSpace !== THREE.NoColorSpace &&
        !(t.isRenderTargetTexture && t.colorSpace === space)
      )
        throw new Error(
          "宽色域颜色纹理须先转换到所选线性空间并使用 NoColorSpace；不能仅重标记 sRGB 纹理。",
        );
    };
    texture(value.scene.background);
    texture(value.scene.environment);
    const seenUniforms = new WeakSet();
    const uniformTextures = (value) => {
      if (!value || typeof value !== "object") return;
      if (value.isTexture) {
        texture(value);
        return; // Texture internals are resources, not nested shader uniforms.
      }
      // Three accepts sampler arrays and nested struct uniforms. Visit aliases
      // once per frame so cycles terminate and later texture edits are rechecked.
      // Numeric buffers contain no textures; do not enumerate large typed arrays.
      if (
        ArrayBuffer.isView(value) ||
        value instanceof ArrayBuffer ||
        seenUniforms.has(value)
      )
        return;
      seenUniforms.add(value);
      for (const child of Object.values(value)) uniformTextures(child);
    };
    const checkedMaterials = new Set();
    value.scene.traverse((node) => {
      for (const original of Array.isArray(node.material)
        ? node.material
        : [node.material]) {
        if (!original) continue;
        // Match Three's actual material selection. An excluded mesh retains its
        // own material, while an unused override must not reject a valid scene.
        const m =
          original.allowOverride === true && value.scene.overrideMaterial
            ? value.scene.overrideMaterial
            : original;
        if (checkedMaterials.has(m)) continue;
        checkedMaterials.add(m);
        for (const value of Object.values(m)) texture(value);
        for (const uniform of Object.values(m.uniforms || {}))
          uniformTextures(uniform.value);
        if (
          m.blending !== THREE.NormalBlending &&
          (m.transparent || m.blending !== THREE.NoBlending)
        )
          throw new Error(
            "HDR 母版要求 NormalBlending 或不透明材质；不支持自定义 Alpha 混合。",
          );
        if (!m.transparent && m.opacity !== undefined && m.opacity !== 1)
          throw new Error(
            "半透明材质必须启用 transparent，以保持线性预乘 Alpha 契约。",
          );
        guardShaderAlpha(m);
      }
    });
  }
  function renderSample(frame, display = true) {
    if (!Number.isFinite(frame) || frame < 0 || frame >= config.frames)
      throw new Error("无效动画帧。");
    const state = {
      ...context,
      frame,
      time: (frame * config.frameRate.denominator) / config.frameRate.numerator,
    };
    assertWorkingSpace();
    renderer.autoClear = false;
    renderer.setRenderTarget(target);
    // Scene backgrounds can change GL clear state. Reset transparent coverage
    // before every image, including a background toggled off on a later frame.
    renderer.setClearColor(0, 0);
    renderer.clear();
    try {
      for (const layer of layers) {
        layer.scene.update({
          ...state,
          sourceColorSpace: layer.sourceSpace,
          workingColorSpace: layer.space,
        });
        layer.assertWorkingSpace();
        THREE.ColorManagement.workingColorSpace = layer.space;
        validateMaterials(layer.scene, layer.space);
        renderer.setRenderTarget(scratch);
        renderer.setClearColor(0, 0);
        renderer.clear();
        renderer.render(layer.scene.scene, layer.scene.camera);
        THREE.ColorManagement.workingColorSpace = workingColorSpace;
        renderer.setRenderTarget(target);
        composite.uniforms.conversion.value = layer.toTarget;
        quad.material = composite;
        renderer.render(quad, quadCamera);
      }
    } finally {
      THREE.ColorManagement.workingColorSpace = workingColorSpace;
    }
    scene.update(state);
    assertWorkingSpace();
    validateMaterials(scene, workingColorSpace);
    renderer.setRenderTarget(target);
    // Retained layers are a background composite, not a shared 3D depth buffer.
    renderer.clearDepth();
    renderer.render(scene.scene, scene.camera);
    if (gl.getError() !== gl.NO_ERROR) throw new Error("HDR GPU 渲染失败。");
    if (display) {
      quad.material = displayMaterial;
      renderer.setRenderTarget(null);
      renderer.render(quad, quadCamera);
      if (gl.getError() !== gl.NO_ERROR)
        throw new Error("OCIO sRGB 预览渲染失败。");
    }
    if (display) document.body.dataset.frame = String(frame);
    return { width, height };
  }
  // Integrate associated linear RGB and coverage before any OCIO/video transform.
  // Ping-pong textures avoid float-blend requirements and texture feedback loops.
  const temporalTargets =
    config.timePlan?.temporal_samples > 1
      ? [floatTarget(width, height), floatTarget(width, height)]
      : [];
  const temporalMaterial = temporalTargets.length
    ? material(
        `uniform sampler2D currentSample; uniform sampler2D previous;
     uniform float weight;
     void main(){fragColor=mix(texture(previous,texcoord),texture(currentSample,texcoord),weight);}`,
        {
          currentSample: { value: target.texture },
          previous: { value: null },
          weight: { value: 1 },
        },
      )
    : null;
  const copyTemporal = temporalTargets.length
    ? material(
        `uniform sampler2D source; void main(){fragColor=texture(source,texcoord);}`,
        { source: { value: null } },
      )
    : null;
  function render(frame, display = true) {
    if (!config.timePlan && !Number.isInteger(frame))
      throw new Error("无效动画帧。");
    const samples = config.timePlan
      ? frameSamples(config.timePlan, frame)
      : [frame];
    if (temporalMaterial) {
      renderer.setRenderTarget(temporalTargets[1]);
      renderer.setClearColor(0, 0);
      renderer.clear();
    }
    for (let index = 0; index < samples.length; index++) {
      renderSample(samples[index], false);
      if (temporalMaterial) {
        temporalMaterial.uniforms.previous.value =
          temporalTargets[(index + 1) % 2].texture;
        temporalMaterial.uniforms.weight.value = 1 / (index + 1);
        renderer.setRenderTarget(temporalTargets[index % 2]);
        quad.material = temporalMaterial;
        renderer.render(quad, quadCamera);
      }
    }
    if (copyTemporal) {
      copyTemporal.uniforms.source.value =
        temporalTargets[(samples.length - 1) % 2].texture;
      renderer.setRenderTarget(target);
      quad.material = copyTemporal;
      renderer.render(quad, quadCamera);
    }
    if (display) {
      quad.material = displayMaterial;
      renderer.setRenderTarget(null);
      renderer.render(quad, quadCamera);
      document.body.dataset.frame = String(frame);
    }
    if (gl.getError() !== gl.NO_ERROR)
      throw new Error("时间采样 GPU 渲染失败。");
    return { width, height };
  }
  // Capture owns one reusable buffer; Node finishes transferring it before the
  // next capture. Float32 values/alpha are copied verbatim, without a display transform.
  let captureValues;
  function capture(frame) {
    render(frame, false);
    captureValues ??= new Float32Array(width * height * 4);
    renderer.readRenderTargetPixels(target, 0, 0, width, height, captureValues);
    if (gl.getError() !== gl.NO_ERROR)
      throw new Error("HDR 浮点像素读取失败。");
    data = new Uint8Array(captureValues.buffer);
    return { width, height, bytes: data.length };
  }
  const bridge = {
    capture,
    async captureVideo(frame, slot, thumbnail) {
      if (!videoCapture) throw new Error("GPU 视频出口未启用。");
      render(frame, false);
      const captured = await videoCapture.capture(slot, thumbnail);
      return {
        pixels: encodeBytes(captured.pixels),
        thumbnail: captured.thumbnail ? encodeBytes(captured.thumbnail) : null,
      };
    },
    // Numeric QA can compare the packed GPU output to the CPU ACES reference.
    captureHDR(frame) {
      if (!hdrCapture) throw new Error("HDR 采集未启用");
      render(frame, false);
      return hdrCapture.capture();
    },
    // Numerical QA reads the actual displayed GPU pixel, not a second CPU recipe.
    sampleDisplay(frame, x, y) {
      render(frame, true);
      const pixel = new Uint8Array(4);
      gl.readPixels(x, y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
      if (gl.getError() !== gl.NO_ERROR) throw new Error("预览像素读取失败。");
      return Array.from(pixel);
    },
    readChunk(offset, length) {
      if (
        !data ||
        !Number.isInteger(offset) ||
        offset < 0 ||
        offset > data.length ||
        !Number.isInteger(length) ||
        length < 1 ||
        length > 4 * 1024 * 1024
      )
        throw new Error("无效读取范围。");
      return encodeBytes(data.subarray(offset, offset + length));
    },
    probe: {
      format: "RGBA32F",
      readback: "Float32Array",
      values: Array.from(probe),
      renderer: gl.getParameter(
        debugInfo ? debugInfo.UNMASKED_RENDERER_WEBGL : gl.RENDERER,
      ),
      vendor: gl.getParameter(
        debugInfo ? debugInfo.UNMASKED_VENDOR_WEBGL : gl.VENDOR,
      ),
      float_blending: floatBlending,
      source_space: sourceSpace,
      preserved_layer_spaces:
        config.display.layers?.map((layer) => layer.source) || [],
      alpha: "premultiplied",
      row_order: "bottom-up",
      sampling: {
        render_scale: renderScale,
        width,
        height,
        spatial_samples: renderScale ** 2,
      },
    },
  };
  window.__chromaFloat = bridge;
  if (!config.preview) return;
  let frame = 0,
    playing = false,
    timer = 0,
    start = 0,
    startFrame = 0;
  const pause = () => {
    playing = false;
    cancelAnimationFrame(timer);
    send("playing", false);
  };
  const draw = () => {
    render(frame);
    send("frame", frame);
  };
  const tick = () => {
    if (!playing) return;
    const next =
      (startFrame +
        Math.floor(((performance.now() - start) * config.fps) / 1000)) %
      config.frames;
    try {
      if (next !== frame) {
        frame = next;
        draw();
      }
    } catch (error) {
      pause();
      send("error", error.message);
      return;
    }
    timer = requestAnimationFrame(tick);
  };
  let captureJob = 0;
  window.addEventListener("message", async (event) => {
    if (event.source !== parent || event.data?.channel !== config.channel)
      return;
    try {
      if (event.data.type === "hdr-cancel") {
        if (captureJob === event.data.value) captureJob = 0;
        return;
      }
      if (event.data.type === "hdr-capture" && useHDR) {
        const { id, frames } = event.data.value || {};
        if (
          !Number.isSafeInteger(id) ||
          !Array.isArray(frames) ||
          !frames.length ||
          frames.length > 64 ||
          frames.some(
            (index) =>
              !Number.isInteger(index) || index < 0 || index >= config.frames,
          )
        )
          return;
        captureJob = id;
        try {
          for (let index = 0; index < frames.length; index++) {
            if (captureJob !== id) return;
            const pixels = bridge.captureHDR(frames[index]);
            // One command per bounded batch avoids a round trip for every frame.
            // Each transferred buffer releases the sandbox copy immediately.
            parent.postMessage(
              {
                channel: config.channel,
                type: "hdr-captured",
                value: { id, index, data: pixels, width, height },
              },
              "*",
              [pixels],
            );
            // Let input/cancellation run between short groups without pacing
            // rendering to the display refresh rate or skipping source frames.
            if (index % 2 === 1)
              await new Promise((resolve) => setTimeout(resolve, 0));
          }
        } catch (error) {
          send("hdr-captured", { id, error: error.message });
        }
        return;
      }
      if (event.data.type === "play" && !playing) {
        playing = true;
        startFrame = frame;
        start = performance.now();
        send("playing", true);
        tick();
      }
      if (event.data.type === "pause") pause();
      if (event.data.type === "seek" && Number.isInteger(event.data.value)) {
        frame = Math.max(0, Math.min(config.frames - 1, event.data.value));
        startFrame = frame;
        start = performance.now();
        draw();
      }
    } catch (error) {
      pause();
      send("error", error.message);
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      pause();
    }
  });
  // Keyboard events inside the sandbox do not bubble to the parent viewer.
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !event.isComposing)
      send("exit-expanded-preview");
  });
  draw();
  send("ready");
}
window.addEventListener("error", (event) => send("error", event.message));
try {
  main();
} catch (error) {
  window.__chromaError = error.message;
  send("error", error.message);
}
