import * as THREE from "three";
import { VIDEO_IN_FLIGHT } from "./video-protocol.mjs";

/** Float scene -> linear sampling/coverage -> pinned OCIO -> packed video bytes.
 * Only video copies are quantized. The source RGBA32F target is never modified.
 */
export function createVideoCapture(
  renderer,
  source,
  contract,
  width,
  height,
  scale,
) {
  const geometry = new THREE.PlaneGeometry(2, 2);
  const camera = new THREE.Camera();
  const resources = [geometry];
  const gl = renderer.getContext();
  const viewport = gl.getParameter(gl.MAX_VIEWPORT_DIMS);
  const limitWidth = Math.min(
    renderer.capabilities.maxTextureSize,
    viewport[0],
  );
  const limitHeight = Math.min(
    renderer.capabilities.maxTextureSize,
    viewport[1],
  );
  const target = (w, h, floating) => {
    if (w > limitWidth || h > limitHeight)
      throw new Error(`GPU 视频纹理 ${w} × ${h} 超出设备范围。`);
    const result = new THREE.WebGLRenderTarget(w, h, {
      type: floating ? THREE.FloatType : THREE.UnsignedByteType,
      format: THREE.RGBAFormat,
      internalFormat: floating ? "RGBA32F" : "RGBA8",
      minFilter: THREE.NearestFilter,
      magFilter: THREE.NearestFilter,
      depthBuffer: false,
      stencilBuffer: false,
    });
    resources.push(result);
    // Validate allocation once; per-frame validation stays in the GPU payload.
    renderer.setRenderTarget(result);
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE)
      throw new Error("GPU 视频目标纹理不可渲染。");
    return result;
  };
  const material = (shader, uniforms) => {
    const result = new THREE.RawShaderMaterial({
      glslVersion: THREE.GLSL3,
      vertexShader:
        "in vec3 position; void main(){gl_Position=vec4(position,1.0);}",
      fragmentShader: `precision highp float; precision highp int; precision highp sampler2D;
        out vec4 fragColor; ${shader}`,
      uniforms,
      depthTest: false,
      depthWrite: false,
      blending: THREE.NoBlending,
      toneMapped: false,
    });
    resources.push(result);
    return result;
  };
  const linear = target(width, height, true);
  const displayed = target(width, height, true);
  const depth =
    contract.pixel_format === "nv12"
      ? 8
      : contract.pixel_format === "p416le"
        ? 16
        : 10;
  const sixteen = depth > 8;
  const wide = contract.output === "PQ" || contract.output === "HLG";
  const sx = contract.pixel_format === "p416le" ? 1 : 2;
  const sy = ["nv12", "p010le"].includes(contract.pixel_format) ? 2 : 1;
  const frameBytes = width * height * (1 + 2 / (sx * sy)) * (sixteen ? 2 : 1);
  // A final row carries per-column validity, outside the codec payload. This
  // detects every bad source sample without a synchronous one-pixel GPU read.
  const packedWidth = Math.max(
    width,
    Math.ceil(frameBytes / (4 * (limitHeight - 1))),
  );
  const rows = Math.ceil(frameBytes / (packedWidth * 4));
  const packed = target(packedWidth, rows + 1, false);
  const resolve = material(
    `
    uniform sampler2D source; uniform mat3 toACEScg;
    bool finite4(vec4 v){return !any(isnan(v))&&!any(isinf(v));}
    vec4 halfValue(vec4 v){
      uvec4 bits=floatBitsToUint(v), exponent=bits & uvec4(0x7f800000u);
      return mix(v*.5,uintBitsToFloat(bits-uvec4(0x00800000u)),greaterThan(exponent,uvec4(0x00800000u)));
    }
    void main(){
      ivec2 origin=ivec2(gl_FragCoord.xy)*${scale};
      vec4 values[16]; bool valid=true;
      for(int y=0;y<${scale};y++)for(int x=0;x<${scale};x++){
        vec4 p=texelFetch(source,origin+ivec2(x,y),0);
        valid=valid&&finite4(p)&&p.a>=0.&&p.a<=1.;
        values[y*${scale}+x]=p;
      }
      // Average associated light/coverage before unassociation. Balanced halves
      // avoid overflow without reducing the source or working float32 precision.
      for(int stride=${Math.floor((scale * scale) / 2)};stride>0;stride/=2)
        for(int i=0;i<stride;i++)values[i]=halfValue(values[i])+halfValue(values[i+stride]);
      vec4 p=values[0];
      vec3 straight=p.a>0.?p.rgb/p.a:vec3(0.);
      vec3 master=toACEScg*straight;
      valid=valid&&finite4(vec4(master,p.a));
      // Opaque video composites on linear black. Alpha never enters OCIO.
      fragColor=vec4(valid?master*p.a:vec3(0.),valid?1.:0.);
    }`,
    {
      source: { value: source.texture },
      toACEScg: {
        value: new THREE.Matrix3().set(...contract.to_acescg.flat()),
      },
    },
  );
  const makeOutput = (description, quantize = false) => {
    const uniforms = { linear: { value: linear.texture } };
    for (const table of description.textures) {
      const values = new Float32Array(table.width * table.height * 4);
      for (let i = 0; i < table.width * table.height; i++) {
        for (let c = 0; c < table.channels; c++)
          values[i * 4 + c] = table.values[i * table.channels + c];
        values[i * 4 + 3] = 1;
      }
      const texture = new THREE.DataTexture(
        values,
        table.width,
        table.height,
        THREE.RGBAFormat,
        THREE.FloatType,
      );
      texture.minFilter = texture.magFilter = table.linear
        ? THREE.LinearFilter
        : THREE.NearestFilter;
      texture.needsUpdate = true;
      resources.push(texture);
      uniforms[table.name] = { value: texture };
    }
    return material(
      `
      uniform sampler2D linear; ${description.shader}
      void main(){
        vec4 p=texelFetch(linear,ivec2(gl_FragCoord.xy),0);
        vec3 rgb=ocioDisplay(vec4(p.rgb,1.)).rgb;
        bool valid=p.a>.5&&!any(isnan(rgb))&&!any(isinf(rgb));
        ${
          quantize
            ? "rgb=clamp(rgb,0.,1.);"
            : contract.output === "HLG"
              ? `
          // HLG super-whites remain float through the matrix. Check physical
          // 10-bit narrow codes rather than clipping RGB to nominal white.
          float Y=dot(rgb,vec3(.2627,.6780,.0593));
          vec3 codes=roundEven(vec3(64.+876.*Y,512.+896.*(rgb.b-Y)/1.8814,512.+896.*(rgb.r-Y)/1.4746));
          valid=valid&&all(greaterThanEqual(codes,vec3(4.)))&&all(lessThanEqual(codes,vec3(1019.)));
        `
              : `${contract.output === "ACEScct" ? "valid=valid&&all(greaterThanEqual(rgb,vec3(0.)))&&all(lessThanEqual(rgb,vec3(1.)));" : ""}
          rgb=roundEven(clamp(rgb,0.,1.)*65535.)/65535.;`
        }
        fragColor=vec4(valid?rgb:vec3(0.),valid?1.:0.);
      }`,
      uniforms,
    );
  };
  const output = makeOutput(contract.display);
  const coefficients = wide ? ".2627,.6780,.0593" : ".2126,.7152,.0722";
  const blue = wide ? "1.8814" : "1.8556";
  const red = wide ? "1.4746" : "1.5748";
  const chroma =
    sx === 1
      ? "rgb=sampleRGB(p);"
      : sy === 1
        ? `
    for(int x=-1;x<=1;x++)rgb+=sampleRGB(p+ivec2(x,0))*float(x==0?2:1)/4.;
  `
        : contract.chroma_location === "topleft"
          ? `
    for(int y=-1;y<=1;y++)for(int x=-1;x<=1;x++)
      rgb+=sampleRGB(p+ivec2(x,y))*float((x==0?2:1)*(y==0?2:1))/16.;
  `
          : `
    // A 2:1 bilinear low-pass has radius two. At the half-pixel vertical
    // position its four taps are 1:3:3:1, not a two-row box average.
    for(int y=-1;y<=2;y++)for(int x=-1;x<=1;x++)
      rgb+=sampleRGB(p+ivec2(x,y))*float((x==0?2:1)*((y==0||y==1)?3:1))/32.;
  `;
  const pack = material(
    `
    uniform sampler2D displayed;
    vec3 sampleRGB(ivec2 p){
      p=clamp(p,ivec2(0),ivec2(${width - 1},${height - 1}));
      return texelFetch(displayed,ivec2(p.x,${height - 1}-p.y),0).rgb;
    }
    float codeAt(int index){
      if(index<${width * height}){
        float Y=dot(sampleRGB(ivec2(index%${width},index/${width})),vec3(${coefficients}));
        return roundEven(${16 * 2 ** (depth - 8)}.+${219 * 2 ** (depth - 8)}.*Y);
      }
      int uv=index-${width * height}; int site=uv/2;
      ivec2 p=ivec2((site%${width / sx})*${sx},(site/${width / sx})*${sy});
      vec3 rgb=vec3(0.); ${chroma}
      float Y=dot(rgb,vec3(${coefficients}));
      float C=uv%2==0?(rgb.b-Y)/${blue}:(rgb.r-Y)/${red};
      return roundEven(${128 * 2 ** (depth - 8)}.+${224 * 2 ** (depth - 8)}.*C);
    }
    float byteAt(int index){
      if(index>=${frameBytes})return 0.;
      ${sixteen ? `float word=codeAt(index/2)*${2 ** (16 - depth)}.; return index%2==0?mod(word,256.):floor(word/256.);` : "return codeAt(index);"}
    }
    void main(){
      ivec2 p=ivec2(gl_FragCoord.xy);
      if(p.y==${rows}){
        bool valid=true;
        if(p.x<${width})for(int y=0;y<${height};y++)valid=valid&&texelFetch(displayed,ivec2(p.x,y),0).a>.5;
        fragColor=vec4(valid?0.:1.);return;
      }
      int offset=(p.y*${packedWidth}+p.x)*4;
      fragColor=vec4(byteAt(offset),byteAt(offset+1),byteAt(offset+2),byteAt(offset+3))/255.;
    }`,
    { displayed: { value: displayed.texture } },
  );
  const mesh = new THREE.Mesh(geometry, resolve);
  const draw = (shader, destination) => {
    mesh.material = shader;
    renderer.setRenderTarget(destination);
    renderer.render(mesh, camera);
  };
  const buffers = Array.from(
    { length: VIDEO_IN_FLIGHT },
    () => new Uint8Array(packedWidth * (rows + 1) * 4),
  );
  const busy = Array(VIDEO_IN_FLIGHT).fill(false);
  let thumbnailTarget, thumbnailMaterial;
  return {
    frameBytes,
    packetBytes: buffers[0].length,
    statusOffset: packedWidth * rows * 4,
    async capture(slot, thumbnail = false) {
      if (busy[slot]) throw new Error("GPU 视频读取槽仍在使用。");
      busy[slot] = true;
      try {
        draw(resolve, linear);
        draw(output, displayed);
        draw(pack, packed);
        // WebGL2 PBO/fence readback lets subsequent frame commands run while
        // transfer completes. Independent byte buffers bound in-flight work.
        const pixels = renderer.readRenderTargetPixelsAsync(
          packed,
          0,
          0,
          packedWidth,
          rows + 1,
          buffers[slot],
        );
        let thumbnailPixels;
        if (thumbnail) {
          thumbnailTarget ??= target(width, height, false);
          thumbnailMaterial ??= makeOutput(contract.thumbnail, true);
          draw(thumbnailMaterial, thumbnailTarget);
          thumbnailPixels = renderer.readRenderTargetPixelsAsync(
            thumbnailTarget,
            0,
            0,
            width,
            height,
            new Uint8Array(width * height * 4),
          );
        }
        await pixels;
        if (gl.isContextLost()) throw new Error("GPU 视频上下文已丢失。");
        if (
          buffers[slot]
            .subarray(packedWidth * rows * 4)
            .some((value) => value !== 0)
        )
          throw new Error(
            "GPU 视频包含非有限值、无效 Alpha 或超出可存储范围的 HLG 信号。",
          );
        const thumb = thumbnailPixels ? await thumbnailPixels : null;
        return {
          pixels: buffers[slot].subarray(0, frameBytes),
          thumbnail: thumb,
        };
      } finally {
        busy[slot] = false;
      }
    },
    dispose() {
      for (const resource of resources) resource.dispose();
    },
  };
}
