"""Pinned OCIO display shader and strict float-frame decoding for the Three bridge."""

from functools import lru_cache
from typing import Any, Literal

import numpy as np
import PyOpenColorIO as ocio

from .color import DISPLAYS, HDR_VIEW, SDR_VIEW, color_processor, config, transform
from .errors import ChromaError
from .video_transport import VideoPixelFormat

MotionSourceSpace = Literal["Linear Rec.709 (sRGB)", "ACEScg", "Linear Rec.2020", "ACES2065-1"]
VideoOutput = Literal["sRGB", "PQ", "HLG", "ACEScct"]
SOURCE_SPACE: MotionSourceSpace = "Linear Rec.709 (sRGB)"
SOURCE_SPACES = [
    {"value": "Linear Rec.2020", "label": "线性 BT.2020 / D65"},
    {"value": "ACEScg", "label": "ACEScg / AP1 / D60（线性）"},
    {"value": "ACES2065-1", "label": "ACES2065-1 / AP0 / D60（线性）"},
    {"value": SOURCE_SPACE, "label": "线性 Rec.709 / D65（兼容）"},
]


@lru_cache(maxsize=4)
def working_space(source: MotionSourceSpace) -> dict[str, Any]:
    """OCIO owns linear primaries/white adaptation; Three consumes the same matrices."""
    primaries = {
        SOURCE_SPACE: [0.64, 0.33, 0.30, 0.60, 0.15, 0.06],
        "ACEScg": [0.713, 0.293, 0.165, 0.830, 0.128, 0.044],
        "ACES2065-1": [0.7347, 0.2653, 0.0, 1.0, 0.0001, -0.0770],
        "Linear Rec.2020": [0.708, 0.292, 0.170, 0.797, 0.131, 0.046],
    }
    if source not in primaries:
        raise ChromaError("motion_color", "动画源码仅支持线性 Rec.709、BT.2020、ACEScg/AP1 或 ACES/AP0。")
    processor = config().getProcessor(source, SOURCE_SPACE).getDefaultCPUProcessor()
    # All supported spaces are scene-linear. Basis vectors extract a matrix, not
    # a fitted display LUT; AP1's D60/D65 adaptation is included by pinned OCIO.
    matrix = np.array([processor.applyRGB(row.tolist()) for row in np.eye(3)]).T
    return {
        "source": source,
        "primaries": primaries[source],
        "white_point": [0.32168, 0.33767] if source in {"ACEScg", "ACES2065-1"} else [0.3127, 0.3290],
        "to_linear_rec709": matrix.tolist(),
    }


@lru_cache(maxsize=8)
def display_shader(source: MotionSourceSpace = SOURCE_SPACE, hdr: bool = False) -> dict[str, Any]:
    # The preview uses the same OCIO rendering transform as CPU-generated PNGs.
    # No fitted Three.js ACES tone mapper, baked SDR screenshot, or alpha transform.
    processor = config().getProcessor(
        ocio.DisplayViewTransform(src=source, display=DISPLAYS["PQ" if hdr else "sRGB"],
                                  view=HDR_VIEW if hdr else SDR_VIEW)
    )
    return _shader_data(processor, source)


@lru_cache(maxsize=48)
def video_shader(source: MotionSourceSpace, output: VideoOutput,
                 pixel_format: VideoPixelFormat | None = None) -> dict[str, Any]:
    # Resolve samples and coverage in the author's linear basis, then cross the
    # same ACEScg boundary as CPU delivery before applying the pinned output view.
    matrix = config().getProcessor(source, "ACEScg").getDefaultCPUProcessor()
    return {
        "output": output,
        "to_acescg": np.array([matrix.applyRGB(row.tolist()) for row in np.eye(3)]).T.tolist(),
        "display": _shader_data(color_processor("ACEScg", output), "ACEScg"),
        "thumbnail": display_shader("ACEScg"),
        "pixel_format": pixel_format or ("nv12" if output == "sRGB" else "p010le"),
        "chroma_location": "topleft" if output in {"PQ", "HLG"} else "left",
    }


def _shader_data(processor: Any, source: MotionSourceSpace) -> dict[str, Any]:
    desc = ocio.GpuShaderDesc.CreateShaderDesc()
    desc.setLanguage(ocio.GPU_LANGUAGE_GLSL_ES_3_0)
    desc.setAllowTexture1D(False)
    desc.setFunctionName("ocioDisplay")
    processor.getDefaultGPUProcessor().extractGpuShaderInfo(desc)
    if list(desc.get3DTextures()) or list(desc.getUniforms()):
        raise ChromaError("motion_color", "当前 OCIO 预览需要未实现的 GPU 资源。")
    shader = desc.getShaderText()
    # OCIO 2.5's ACES table helpers emit desktop-style implicit int/float
    # arithmetic even in ES mode. Add explicit casts only; the algorithm, tables
    # and constants remain OCIO-owned and are compared against CPU output in tests.
    shader = shader.replace("float i_lo = i_base + 1;", "float i_lo = i_base + 1.0;")
    shader = shader.replace("float i_hi = i_lo + 1;", "float i_hi = i_lo + 1.0;")
    shader = shader.replace("(i_hi - 1 + 0.5)", "(float(i_hi) - 0.5)")
    shader = shader.replace("(i_hi + 0.5)", "(float(i_hi) + 0.5)")
    for texture in desc.getTextures():
        shader = shader.replace(f"/ {texture.width},", f"/ {texture.width}.0,")
    return {
        "working_space": working_space(source),
        "shader": shader,
        "textures": [
            {
                "name": t.samplerName,
                "width": t.width,
                "height": t.height,
                "channels": 1 if t.channel == desc.TEXTURE_RED_CHANNEL else 3,
                "linear": t.interpolation == ocio.INTERP_LINEAR,
                "values": t.getValues().tolist(),
            }
            for t in desc.getTextures()
        ],
    }


def decode_float_frame(
    data: bytes, width: int, height: int, render_scale: int = 1,
    source: MotionSourceSpace = SOURCE_SPACE,
) -> np.ndarray:
    working_space(source)  # Reject encoded/unknown inputs even for the identity path.
    if render_scale not in (1, 2, 4) or len(data) != width * height * render_scale**2 * 16:
        raise ChromaError("motion_frame", "浮点帧字节数与画面规格不符。")
    pixels = np.frombuffer(data, dtype="<f4").reshape(height * render_scale, width * render_scale, 4).copy()
    if not np.isfinite(pixels).all() or np.any((pixels[..., 3] < 0) | (pixels[..., 3] > 1)):
        raise ChromaError("motion_frame", "浮点帧包含非有限 RGB 或超出 0..1 的 Alpha。")
    # WebGL rows start at the bottom; normal blending accumulates associated RGB.
    # Unassociate in linear light before OCIO; zero-coverage RGB has no contribution.
    pixels = np.ascontiguousarray(pixels[::-1])
    if render_scale > 1:
        # Integrate associated radiance and coverage before unassociation or any
        # display transform. Float64 accumulation avoids overflow in a finite HDR average.
        pixels = pixels.reshape(height, render_scale, width, render_scale, 4).mean(
            axis=(1, 3), dtype=np.float64
        ).astype(np.float32)
    alpha = pixels[..., 3:4]
    pixels[..., :3] = np.divide(
        pixels[..., :3], alpha, out=np.zeros_like(pixels[..., :3]), where=alpha > 0
    )
    # Native AP1 needs no round trip through Rec.709. Other linear inputs only
    # change primaries/white point here; display rendering belongs to the exit.
    return pixels if source == "ACEScg" else transform(pixels, "ACEScg", source=source)
