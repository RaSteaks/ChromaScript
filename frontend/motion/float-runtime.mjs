import * as THREE from "three";
import createScene from "chromascript:scene";
import config from "chromascript:config";

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
    colorSpace: THREE.LinearSRGBColorSpace,
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
  renderer.outputColorSpace = THREE.LinearSRGBColorSpace;
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
  const limit = config.preview
    ? 960
    : config.maxDimension || Math.max(config.width, config.height);
  const scale = Math.min(1, limit / Math.max(config.width, config.height));
  const width = Math.max(1, Math.round(config.width * scale));
  const height = Math.max(1, Math.round(config.height * scale));
  renderer.setSize(width, height, false);
  renderer.domElement.style.cssText =
    "width:100%;height:100%;object-fit:contain;display:block";
  document.getElementById("root").appendChild(renderer.domElement);
  const target = floatTarget(width, height);
  const scene = createScene({
    width: config.width,
    height: config.height,
    frames: config.frames,
    fps: config.fps,
    frameRate: config.frameRate,
  });
  if (
    !scene?.scene?.isScene ||
    !scene?.camera?.isCamera ||
    typeof scene.update !== "function"
  )
    throw new Error(
      "HDR 代码须默认导出工厂函数，返回 {scene, camera, update({frame, time})}；不支持 React DOM/SVG 截图。",
    );
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
  let data = null;
  function render(frame, display = true) {
    if (!Number.isInteger(frame) || frame < 0 || frame >= config.frames)
      throw new Error("无效动画帧。");
    // Exact rational source time, independent of preview wall-clock/skipped frames.
    scene.update({
      frame,
      time: (frame * config.frameRate.denominator) / config.frameRate.numerator,
      fps: config.fps,
      width: config.width,
      height: config.height,
    });
    scene.scene.traverse((node) => {
      for (const m of Array.isArray(node.material)
        ? node.material
        : [node.material]) {
        if (!m) continue;
        if (
          m.blending !== THREE.NormalBlending &&
          m.blending !== THREE.NoBlending
        )
          throw new Error(
            "HDR 母版要求 NormalBlending 或不透明材质；不支持自定义 Alpha 混合。",
          );
        if (!m.transparent && m.opacity !== undefined && m.opacity !== 1)
          throw new Error(
            "半透明材质必须启用 transparent，以保持线性预乘 Alpha 契约。",
          );
      }
    });
    renderer.setRenderTarget(target);
    renderer.clear();
    renderer.render(scene.scene, scene.camera);
    if (gl.getError() !== gl.NO_ERROR) throw new Error("HDR GPU 渲染失败。");
    if (display) {
      quad.material = displayMaterial;
      renderer.setRenderTarget(null);
      renderer.render(quad, quadCamera);
      if (gl.getError() !== gl.NO_ERROR)
        throw new Error("OCIO sRGB 预览渲染失败。");
    }
    document.body.dataset.frame = String(frame);
    return { width, height };
  }
  function capture(frame) {
    render(frame, false);
    const values = new Float32Array(width * height * 4);
    renderer.readRenderTargetPixels(target, 0, 0, width, height, values);
    if (gl.getError() !== gl.NO_ERROR)
      throw new Error("HDR 浮点像素读取失败。");
    data = new Uint8Array(values.buffer);
    return { width, height, bytes: data.length };
  }
  const bridge = {
    capture,
    // Numerical QA reads the actual displayed GPU pixel, not a second CPU recipe.
    sampleDisplay(frame, x, y) {
      render(frame, true);
      const pixel = new Uint8Array(4);
      gl.readPixels(x, y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
      if (gl.getError() !== gl.NO_ERROR) throw new Error("预览像素读取失败。");
      return Array.from(pixel);
    },
    readChunk(offset, length) {
      if (!data || !Number.isInteger(offset) || offset < 0 || length > 1048576)
        throw new Error("无效读取范围。");
      const chunk = data.subarray(offset, offset + length);
      let binary = "";
      for (let i = 0; i < chunk.length; i += 32768)
        binary += String.fromCharCode(...chunk.subarray(i, i + 32768));
      return btoa(binary);
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
      source_space: "Linear Rec.709 (sRGB)",
      alpha: "premultiplied",
      row_order: "bottom-up",
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
  window.addEventListener("message", (event) => {
    if (event.source !== parent || event.data?.channel !== config.channel)
      return;
    try {
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
    if (document.hidden) pause();
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
