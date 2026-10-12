"""ACES output rendering is applied exactly once; alpha is never color transformed."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Any

import numpy as np
import PyOpenColorIO as ocio
from numpy.typing import NDArray

from .errors import ChromaError

CONFIG_ID = "cg-config-v4.0.0_aces-v2.0_ocio-v2.5"
HDR_VIEW = "ACES 2.0 - HDR 1000 nits (Rec.2020)"
SDR_VIEW = "ACES 2.0 - SDR 100 nits (Rec.709)"
DISPLAYS = {
    "sRGB": "sRGB - Display",
    "Rec.709": "Rec.1886 Rec.709 - Display",
    "PQ": "Rec.2100-PQ - Display",
}
HLG_DISPLAY = "Rec.2100-HLG - Display"


@lru_cache(maxsize=1)
def config() -> Any:
    c = ocio.Config.CreateFromBuiltinConfig(CONFIG_ID)
    # The CG config has PQ views but omits an HLG display. OCIO's official display
    # transform supplies the 1000-nit HLG OOTF/OETF without hand-written gamma math.
    hlg = ocio.ColorSpace(ocio.REFERENCE_SPACE_DISPLAY, name=HLG_DISPLAY)
    hlg.setTransform(
        ocio.BuiltinTransform(style="DISPLAY - CIE-XYZ-D65_to_REC.2100-HLG-1000nit"),
        ocio.COLORSPACE_DIR_FROM_REFERENCE,
    )
    c.addColorSpace(hlg)
    c.validate()
    return c


def color_info() -> dict[str, Any]:
    c = config()
    return {
        "id": CONFIG_ID,
        "aces_version": "2.0",
        "ocio_version": ocio.__version__,
        "sha256": hashlib.sha256(c.serialize().encode()).hexdigest(),
        "working_space": "ACEScg",
        "spaces": list(c.getColorSpaceNames()),
        "displays": {d: list(c.getViews(d)) for d in c.getDisplays()},
        "outputs": ["ACEScg", "ACES2065-1", "sRGB", "Rec.709", "PQ", "HLG"],
        "hdr_view": HDR_VIEW,
        "limiting_gamut": "Rec.2020",
        "peak_nits": 1000,
        "hlg_extension": "DISPLAY - CIE-XYZ-D65_to_REC.2100-HLG-1000nit",
    }


@lru_cache(maxsize=32)
def processor(source: str, output: str) -> Any:
    c = config()
    src = "sRGB Encoded Rec.709 (sRGB)" if source == "sRGB" else source
    if output in {"ACEScg", "ACES2065-1"}:
        return c.getProcessor(src, output).getDefaultCPUProcessor()
    if output == "HLG":
        group = ocio.GroupTransform()
        group.appendTransform(
            ocio.DisplayViewTransform(src=src, display=DISPLAYS["PQ"], view=HDR_VIEW)
        )
        group.appendTransform(ocio.ColorSpaceTransform(src=DISPLAYS["PQ"], dst=HLG_DISPLAY))
        return c.getProcessor(group).getDefaultCPUProcessor()
    # Utility/working spaces are pure color-space conversions, distinct from the
    # named display aliases which deliberately apply an ACES output transform.
    target = c.getColorSpace(output)
    if (
        output not in DISPLAYS
        and target
        and target.getReferenceSpaceType() == ocio.REFERENCE_SPACE_SCENE
    ):
        return c.getProcessor(src, output).getDefaultCPUProcessor()
    if output not in DISPLAYS:
        raise ChromaError("unknown_color_output", f"Unknown output: {output}")
    view = HDR_VIEW if output == "PQ" else SDR_VIEW
    return c.getProcessor(
        ocio.DisplayViewTransform(src=src, display=DISPLAYS[output], view=view)
    ).getDefaultCPUProcessor()


def transform(
    image: NDArray[np.float32], output: str, source: str = "ACEScg"
) -> NDArray[np.float32]:
    """Input/output are straight RGB(A); callers unassociate premultiplied files first."""
    result = np.ascontiguousarray(image, dtype=np.float32).copy()
    if not np.isfinite(result).all():
        raise ChromaError("non_finite_color", "Color transform received NaN or infinity")
    p = processor(source, output)
    if result.ndim == 1:
        result[:3] = p.applyRGB(result[:3].tolist())
    else:
        height, width, channels = result.shape
        # Some identity alpha exponents in native OCIO fast paths round alpha.
        # Never send alpha into the color processor: only contiguous RGB is transformed.
        rgb = np.ascontiguousarray(result[..., :3])
        p.apply(ocio.PackedImageDesc(rgb, width, height, 3))
        result[..., :3] = rgb
    if not np.isfinite(result).all():
        raise ChromaError("non_finite_color", "Output transform produced non-finite values")
    return result


def input_rgb(value: list[float], space: str) -> list[float]:
    return transform(np.array(value, dtype=np.float32), "ACEScg", source=space).tolist()


def tags(output: str) -> dict[str, str]:
    if output in {"PQ", "HLG"}:
        return {
            "primaries": "bt2020",
            "transfer": "smpte2084" if output == "PQ" else "arib-std-b67",
            "matrix": "bt2020nc",
            "zscale_matrix": "2020_ncl",
        }
    return {
        "primaries": "bt709",
        "transfer": "iec61966-2-1" if output == "sRGB" else "bt709",
        "matrix": "bt709",
        "zscale_matrix": "709",
    }


def render_output(image: NDArray[np.float32], output: str, keep_alpha: bool) -> NDArray[np.float32]:
    """Flatten on black in scene-linear light before display rendering, when alpha is omitted."""
    pixels = np.array(image, dtype=np.float32, copy=True)
    if not keep_alpha:
        pixels[..., :3] *= pixels[..., 3:4]
        pixels[..., 3] = 1.0
    return transform(pixels, output)
