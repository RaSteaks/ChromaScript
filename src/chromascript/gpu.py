"""Bounded offscreen Metal/WebGPU compute renderer with global sample coordinates."""

from __future__ import annotations

import hashlib
import math
import struct
import threading
from collections import OrderedDict
from typing import Any, Callable

import numpy as np
import wgpu
from numpy.typing import NDArray

from .color import input_rgb
from .errors import ChromaError
from .models import RenderSettings, SceneSpec

HEADER = """
struct Uniforms {
    resolution: vec2<u32>,
    tile_origin: vec2<u32>,
    tile_size: vec2<u32>,
    seed: u32,
    samples: u32,
    time_data: vec4<f32>,
    parameters: array<vec4<f32>, 24>,
};
@group(0) @binding(0) var<uniform> settings: Uniforms;
@group(0) @binding(1) var<storage, read_write> pixels: array<vec4<f32>>;
"""
KERNEL = """
@compute @workgroup_size(8, 8)
fn main(@builtin(global_invocation_id) gid: vec3<u32>) {
    if gid.x >= settings.tile_size.x || gid.y >= settings.tile_size.y { return; }
    let side = u32(sqrt(f32(settings.samples)));
    var sum = vec4<f32>(0.0);
    for (var sy = 0u; sy < side; sy++) {
        for (var sx = 0u; sx < side; sx++) {
            let offset = (vec2<f32>(f32(sx), f32(sy)) + 0.5) / f32(side);
            let dims = vec2<f32>(settings.resolution);
            // Preview pixels sample the original logical resolution. Pixel-sized
            // effects therefore keep their scale while the raster does less work.
            let pixel = (vec2<f32>(settings.tile_origin + gid.xy) + offset)
                        * (dims / settings.time_data.zw);
            let uv = vec2<f32>(pixel.x / dims.x, 1.0 - pixel.y / dims.y);
            let ctx = SceneContext(pixel, uv, dims, settings.time_data.x,
                                   u32(settings.time_data.y), settings.seed, make_parameters());
            let c = shade(ctx);
            // Validate every sample, including hidden RGB, before averaging can hide
            // invalid values. IEEE exponent bits identify both infinity and NaN.
            if any((bitcast<vec4<u32>>(c) & vec4<u32>(0x7f800000u)) == vec4<u32>(0x7f800000u)) {
                pixels[gid.y * settings.tile_size.x + gid.x] = vec4<f32>(bitcast<f32>(0x7fc00000u), 0.0, 0.0, 1.0);
                return;
            }
            if c.a < 0.0 || c.a > 1.0 {
                pixels[gid.y * settings.tile_size.x + gid.x] = vec4<f32>(0.0, 0.0, 0.0, -1.0);
                return;
            }
            // Accumulate coverage-weighted radiance, then unassociate. This avoids
            // dark fringes and RGB leakage when averaging transparent samples.
            sum += vec4<f32>(c.rgb * c.a, c.a);
        }
    }
    sum /= f32(settings.samples);
    var rgb = vec3<f32>(0.0);
    if sum.a > 0.0 { rgb = sum.rgb / sum.a; }
    pixels[gid.y * settings.tile_size.x + gid.x] = vec4<f32>(rgb, sum.a);
}
"""


def shader_source(scene: SceneSpec) -> str:
    fields, values = [], []
    for index, parameter in enumerate(scene.parameters):
        kind = {"number": "f32", "color": "vec3<f32>", "boolean": "bool"}[parameter.kind]
        fields.append(f"    {parameter.name}: {kind},")
        expression = f"settings.parameters[{index}]." + (
            "xyz" if parameter.kind == "color" else "x"
        )
        values.append(f"({expression} > 0.5)" if parameter.kind == "boolean" else expression)
    if not fields:
        fields, values = ["    unused: f32,"], ["0.0"]
    definitions = "\n".join(fields)
    return (
        HEADER
        + f"""
struct SceneParameters {{
{definitions}
}};
struct SceneContext {{
    pixel: vec2<f32>,
    uv: vec2<f32>,
    resolution: vec2<f32>,
    time: f32,
    frame: u32,
    seed: u32,
    params: SceneParameters,
}};
fn make_parameters() -> SceneParameters {{
    return SceneParameters({", ".join(values)});
}}
"""
        + scene.code
        + KERNEL
    )


class Renderer:
    """A worker owns one GPU device and reuses bounded buffers across frames."""

    def __init__(self) -> None:
        try:
            adapter = wgpu.gpu.request_adapter_sync(power_preference="high-performance")
            if adapter is None:
                raise RuntimeError("No GPU adapter")
            self.device = adapter.request_device_sync()
            self.info = dict(adapter.info)
            self.cache: OrderedDict[str, Any] = OrderedDict()
            self.lock = threading.Lock()
            self.uniform = self.device.create_buffer(
                size=432, usage=wgpu.BufferUsage.UNIFORM | wgpu.BufferUsage.COPY_DST
            )
            self.output = self.device.create_buffer(
                size=512 * 512 * 16, usage=wgpu.BufferUsage.STORAGE | wgpu.BufferUsage.COPY_SRC
            )
        except Exception as exc:
            raise ChromaError("gpu_unavailable", f"GPU initialization failed: {exc}") from exc

    def pipeline(self, scene: SceneSpec) -> Any:
        source = shader_source(scene)
        key = hashlib.sha256(source.encode()).hexdigest()
        if key not in self.cache:
            try:
                module = self.device.create_shader_module(code=source)
                self.cache[key] = self.device.create_compute_pipeline(
                    layout="auto", compute={"module": module, "entry_point": "main"}
                )
            except Exception as exc:
                raise ChromaError("shader_compile", str(exc), generated_source=source) from exc
            # Interactive code edits must not retain an unbounded number of GPU pipelines.
            if len(self.cache) > 16:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        return self.cache[key]

    def validate(self, scene: SceneSpec) -> dict[str, Any]:
        with self.lock:
            self.pipeline(scene)
        return {
            "valid": True,
            "gpu": self.info,
            "parameter_count": len(scene.parameters),
            "source_sha256": hashlib.sha256(shader_source(scene).encode()).hexdigest(),
        }

    def render(
        self,
        scene: SceneSpec,
        settings: RenderSettings,
        frame: int,
        on_tile: Callable[[], None] | None = None,
        *,
        resolution: tuple[int, int] | None = None,
    ) -> NDArray[np.float32]:
        with self.lock:
            return self._render(scene, settings, frame, on_tile, resolution)

    def _render(
        self,
        scene: SceneSpec,
        settings: RenderSettings,
        frame: int,
        on_tile: Callable[[], None] | None,
        resolution: tuple[int, int] | None,
    ) -> NDArray[np.float32]:
        pipeline = self.pipeline(scene)
        params = np.zeros((24, 4), dtype=np.float32)
        for index, parameter in enumerate(scene.parameters):
            value = scene.values.get(parameter.name, parameter.default)
            if parameter.kind == "color":
                params[index, :3] = input_rgb(value, parameter.color_space)
            else:
                params[index, 0] = float(value)
        bind = self.device.create_bind_group(
            layout=pipeline.get_bind_group_layout(0),
            entries=[
                {"binding": 0, "resource": {"buffer": self.uniform}},
                {"binding": 1, "resource": {"buffer": self.output}},
            ],
        )
        image = np.empty((settings.height, settings.width, 4), dtype=np.float32)
        for y in range(0, settings.height, settings.tile_size):
            for x in range(0, settings.width, settings.tile_size):
                width = min(settings.tile_size, settings.width - x)
                height = min(settings.tile_size, settings.height - y)
                # Frame time comes from the exact rational timebase, not elapsed wall time.
                header = struct.pack(
                    "<8I4f",
                    *(resolution or (settings.width, settings.height)),
                    x,
                    y,
                    width,
                    height,
                    scene.seed,
                    settings.samples,
                    frame * settings.fps.denominator / settings.fps.numerator,
                    float(frame),
                    float(settings.width),
                    float(settings.height),
                )
                self.device.queue.write_buffer(self.uniform, 0, header + params.tobytes())
                encoder = self.device.create_command_encoder()
                compute = encoder.begin_compute_pass()
                compute.set_pipeline(pipeline)
                compute.set_bind_group(0, bind)
                compute.dispatch_workgroups(math.ceil(width / 8), math.ceil(height / 8))
                compute.end()
                self.device.queue.submit([encoder.finish()])
                memory = self.device.queue.read_buffer(self.output, 0, width * height * 16)
                image[y : y + height, x : x + width] = np.frombuffer(
                    memory, dtype=np.float32
                ).reshape(height, width, 4)
                if on_tile:
                    on_tile()
        if not np.isfinite(image).all():
            raise ChromaError("non_finite_pixels", "Shader produced NaN or infinity")
        if np.any((image[..., 3] < 0) | (image[..., 3] > 1)):
            raise ChromaError("invalid_alpha", "Shader alpha must be in [0, 1]")
        return image
