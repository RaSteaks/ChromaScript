import * as THREE from "three";

/** GPU OCIO -> top-down P010. No screenshot, 8-bit RGB or CPU tone-map boundary. */
export function createHDRCapture(renderer, source, contract, width, height) {
  const camera = new THREE.Camera();
  const geometry = new THREE.PlaneGeometry(2, 2);
  const textures = [];
  const uniforms = { source: { value: source.texture } };
  for (const table of contract.textures) {
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
    textures.push(texture);
    uniforms[table.name] = { value: texture };
  }
  const material = (shader, bindings) =>
    new THREE.RawShaderMaterial({
      glslVersion: THREE.GLSL3,
      vertexShader:
        "in vec3 position; in vec2 uv; out vec2 texcoord; void main(){texcoord=uv;gl_Position=vec4(position,1.0);}",
      fragmentShader: `precision highp float; precision highp int; precision highp sampler2D;
      in vec2 texcoord; out vec4 fragColor; ${shader}`,
      uniforms: bindings,
      depthTest: false,
      depthWrite: false,
      blending: THREE.NoBlending,
      toneMapped: false,
    });
  const pq = new THREE.WebGLRenderTarget(width, height, {
    type: THREE.FloatType,
    format: THREE.RGBAFormat,
    internalFormat: "RGBA32F",
    minFilter: THREE.NearestFilter,
    magFilter: THREE.NearestFilter,
    depthBuffer: false,
  });
  const packed = new THREE.WebGLRenderTarget(width / 2, (height * 3) / 2, {
    type: THREE.UnsignedByteType,
    format: THREE.RGBAFormat,
    internalFormat: "RGBA8",
    minFilter: THREE.NearestFilter,
    magFilter: THREE.NearestFilter,
    depthBuffer: false,
  });
  const output = material(
    `uniform sampler2D source;
    ${contract.shader}
    void main(){
      // Source is associated linear light. Its RGB already is the black composite;
      // alpha bypasses OCIO, and PQ rendering happens exactly once here.
      vec4 p=texture(source,texcoord);
      vec3 rgb=ocioDisplay(vec4(p.a>0.0?p.rgb:vec3(0.0),1.0)).rgb;
      bool valid=!any(isnan(p))&&!any(isinf(p))&&p.a>=0.0&&p.a<=1.0
        &&!any(isnan(rgb))&&!any(isinf(rgb));
      fragColor=vec4(valid?rgb:vec3(0.0),valid?1.0:0.0);
    }`,
    uniforms,
  );
  const pack = material(
    `uniform sampler2D pq;
    bool invalidSample=false;
    vec3 sampleRGB(ivec2 p){
      p=clamp(p,ivec2(0),ivec2(${width - 1},${height - 1}));
      vec4 value=texelFetch(pq,ivec2(p.x,${height - 1}-p.y),0);
      invalidSample=invalidSample||value.a<0.5;
      return value.rgb;
    }
    float luma(vec3 rgb){return dot(rgb,vec3(0.2627,0.6780,0.0593));}
    void main(){
      ivec2 p=ivec2(gl_FragCoord.xy); vec2 codes;
      if(p.y<${height}){
        codes=64.0+876.0*vec2(luma(sampleRGB(ivec2(p.x*2,p.y))),luma(sampleRGB(ivec2(p.x*2+1,p.y))));
      }else{
        ivec2 c=ivec2(p.x*2,(p.y-${height})*2); vec3 rgb=vec3(0.0);
        // Symmetric 3x3 low-pass is centered at the top-left luma sample.
        // The encoder's VUI declares that same chroma location (type 2).
        for(int y=-1;y<=1;y++) for(int x=-1;x<=1;x++)
          rgb+=sampleRGB(c+ivec2(x,y))*float((x==0?2:1)*(y==0?2:1))/16.0;
        float Y=luma(rgb);
        codes=512.0+896.0*vec2((rgb.b-Y)/1.8814,(rgb.r-Y)/1.4746);
      }
      // P010 stores ten-bit codes in the HIGH bits of little-endian uint16.
      vec2 words=floor(clamp(codes,0.0,1023.0)+0.5)*64.0;
      fragColor=vec4(mod(words.x,256.0),floor(words.x/256.0),mod(words.y,256.0),floor(words.y/256.0))/255.0;
      // Invalid float/alpha input sets a forbidden low bit; the shared encoder
      // rejects the frame instead of silently converting NaN to black.
      if(invalidSample) fragColor.r=1.0/255.0;
    }`,
    { pq: { value: pq.texture } },
  );
  const mesh = new THREE.Mesh(geometry, output);
  return {
    capture() {
      mesh.material = output;
      renderer.setRenderTarget(pq);
      renderer.render(mesh, camera);
      mesh.material = pack;
      renderer.setRenderTarget(packed);
      renderer.render(mesh, camera);
      const data = new Uint8Array(width * height * 3);
      renderer.readRenderTargetPixels(
        packed,
        0,
        0,
        width / 2,
        (height * 3) / 2,
        data,
      );
      if (renderer.getContext().getError() !== 0)
        throw new Error("HDR 10-bit 像素采集失败");
      return data.buffer;
    },
    dispose() {
      output.dispose();
      pack.dispose();
      geometry.dispose();
      pq.dispose();
      packed.dispose();
      for (const texture of textures) texture.dispose();
    },
  };
}
