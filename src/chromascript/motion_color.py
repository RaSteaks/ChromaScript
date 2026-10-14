"""Pinned OCIO display shader and strict float-frame decoding for the Three bridge."""

from functools import lru_cache
from typing import Any

import numpy as np
import PyOpenColorIO as ocio

from .color import DISPLAYS, SDR_VIEW, config, transform
from .errors import ChromaError

SOURCE_SPACE = "Linear Rec.709 (sRGB)"


@lru_cache(maxsize=1)
def display_shader() -> dict[str, Any]:
    # The preview uses the same OCIO rendering transform as CPU-generated PNGs.
    # No fitted Three.js ACES tone mapper, baked SDR screenshot, or alpha transform.
    processor = config().getProcessor(
        ocio.DisplayViewTransform(src=SOURCE_SPACE, display=DISPLAYS["sRGB"], view=SDR_VIEW)
    )
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


def decode_float_frame(data: bytes, width: int, height: int) -> np.ndarray:
    if len(data) != width * height * 16:
        raise ChromaError("motion_frame", "浮点帧字节数与画面规格不符。")
    pixels = np.frombuffer(data, dtype="<f4").reshape(height, width, 4).copy()
    if not np.isfinite(pixels).all() or np.any((pixels[..., 3] < 0) | (pixels[..., 3] > 1)):
        raise ChromaError("motion_frame", "浮点帧包含非有限 RGB 或超出 0..1 的 Alpha。")
    # WebGL rows start at the bottom; normal blending accumulates associated RGB.
    # Unassociate in linear light before OCIO; zero-coverage RGB has no contribution.
    pixels = np.ascontiguousarray(pixels[::-1])
    alpha = pixels[..., 3:4]
    pixels[..., :3] = np.divide(
        pixels[..., :3], alpha, out=np.zeros_like(pixels[..., :3]), where=alpha > 0
    )
    return transform(pixels, "ACEScg", source=SOURCE_SPACE)
