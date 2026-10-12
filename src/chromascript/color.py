"""ACES output rendering is applied exactly once; alpha is never color transformed."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Any

import numpy as np
import PyOpenColorIO as ocio
from numpy.typing import NDArray

from .errors import ChromaError
from .models import OUTPUT_ENCODINGS

CONFIG_ID = "cg-config-v4.0.0_aces-v2.0_ocio-v2.5"
HDR_VIEW = "ACES 2.0 - HDR 1000 nits (Rec.2020)"
SDR_VIEW = "ACES 2.0 - SDR 100 nits (Rec.709)"
DISPLAYS = {
    "sRGB": "sRGB - Display",
    "Rec.709": "Rec.1886 Rec.709 - Display",
    "PQ": "Rec.2100-PQ - Display",
    "Display-P3": "Display P3 - Display",
    "P3-PQ": "ST2084-P3-D65 - Display",
}
HLG_DISPLAY = "Rec.2100-HLG - Display"

# One terminology owner serves the UI, model tools and native verification reports.
# Peak luminance belongs to the output view, never to the transfer function's name.
SPACE_LABELS = {
    "P3-D65": "P3-D65",
    "Rec.709": "Rec.709（BT.709）",
    "Rec.2020": "BT.2020（Rec.2020）",
    "ACEScct": "ACEScct（AP1，对数）",
    "ACEScg": "ACEScg（AP1，线性）",
    "ACES2065-1": "ACES2065-1（AP0，线性）",
}
GAMMA_LABELS = {
    "linear": "线性（Linear）",
    "acescct": "ACEScct（对数）",
    "gamma2.4": "Gamma 2.4（BT.1886）",
    "srgb": "sRGB（IEC 61966-2-1）",
    "st2084": "PQ（SMPTE ST 2084）",
    "hlg": "HLG（ARIB STD-B67）",
}


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
        "outputs": list(OUTPUT_ENCODINGS),
        "output_encodings": {output: output_metadata(output) for output in OUTPUT_ENCODINGS},
        "hdr_view": HDR_VIEW,
        "limiting_gamut": "Rec.2020",
        "peak_nits": 1000,
        "hlg_extension": "DISPLAY - CIE-XYZ-D65_to_REC.2100-HLG-1000nit",
    }


@lru_cache(maxsize=32)
def processor(source: str, output: str) -> Any:
    c = config()
    src = "sRGB Encoded Rec.709 (sRGB)" if source == "sRGB" else source
    if output in {"ACEScg", "ACES2065-1", "ACEScct"}:
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
    # P3 uses the official P3 limiting gamut as well as P3 display encoding;
    # tagging a Rec.709 picture as P3 would not create wider-gamut pixels.
    view = (
        "ACES 2.0 - HDR 1000 nits (P3 D65)"
        if output == "P3-PQ"
        else "ACES 2.0 - SDR 100 nits (P3 D65)"
        if output == "Display-P3"
        else HDR_VIEW
        if output == "PQ"
        else SDR_VIEW
    )
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
    space, gamma = OUTPUT_ENCODINGS[output]
    if output == "ACEScct":
        # H.273 has no AP1 or ACEScct identifiers. Unspecified flags plus explicit
        # encoding metadata are honest; neither BT.709 nor generic logarithm fits.
        return {
            "primaries": "unknown",
            "transfer": "unknown",
            "matrix": "bt709",
            "zscale_matrix": "709",
        }
    if space == "P3-D65":
        # SMPTE 432 identifies P3-D65. The signalled 709 YCbCr matrix is only a
        # reversible packing of encoded RGB; it does not change the RGB primaries.
        return {
            "primaries": "smpte432",
            "transfer": "smpte2084" if gamma == "st2084" else "iec61966-2-1",
            "matrix": "bt709",
            "zscale_matrix": "709",
        }
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


def output_metadata(output: str) -> dict[str, Any]:
    """Describe the encoded samples, independently of a container's limited tag vocabulary."""
    space, gamma = OUTPUT_ENCODINGS[output]
    scene_linear = gamma == "linear"
    scene_log = gamma == "acescct"
    scene_referred = scene_linear or scene_log
    hdr = gamma in {"st2084", "hlg"}
    # These are the actual fixed ACES views used by processor(), not an editable
    # mastering-metadata value or an intrinsic maximum of PQ/HLG encoding.
    target_peak = None if scene_referred else 1000 if hdr else 100
    signal_standard = (
        "ACEScct"
        if scene_log
        else "ACES"
        if scene_linear
        else "SMPTE ST 2084 / P3-D65"
        if hdr and space == "P3-D65"
        else "ITU-R BT.2100"
        if hdr
        else "IEC 61966-2-1"
        if gamma == "srgb"
        else "ITU-R BT.709 / BT.1886"
    )
    return {
        "color_space_label": SPACE_LABELS[space],
        "transfer_function_label": GAMMA_LABELS[gamma],
        "profile_label": f"{SPACE_LABELS[space]} / {GAMMA_LABELS[gamma]}",
        "white_point": "D60" if scene_referred else "D65",
        "dynamic_range": "scene-log"
        if scene_log
        else "scene-linear"
        if scene_linear
        else "HDR"
        if hdr
        else "SDR",
        "signal_standard": signal_standard,
        "target_display_peak_nits": target_peak,
        "target_display_peak_editable": False,
        "limiting_gamut": None if scene_referred else space,
        "output_view": None
        if scene_referred
        else ("ACES 2.0 - HDR 1000 nits (P3 D65)" if hdr else "ACES 2.0 - SDR 100 nits (P3 D65)")
        if space == "P3-D65"
        else HDR_VIEW
        if hdr
        else SDR_VIEW,
        "color_space": space,
        "gamma": gamma,
        "output_transform": output,
        "reference": "scene" if scene_referred else "display",
        "resolve_input_color_space": "ACES (AP1)" if scene_log else space,
        "requires_manual_video_interpretation": scene_log,
        "resolve_input_gamma": {
            "linear": "Linear",
            "acescct": "ACEScct",
            "gamma2.4": "Gamma 2.4",
            "srgb": "sRGB",
            "st2084": "ST2084",
            "hlg": "HLG",
        }[gamma],
        # Native header checks cannot certify an import in a particular Resolve version.
        "resolve_import_verified": False,
    }


def render_output(image: NDArray[np.float32], output: str, keep_alpha: bool) -> NDArray[np.float32]:
    """Flatten on black in scene-linear light before display rendering, when alpha is omitted."""
    pixels = np.array(image, dtype=np.float32, copy=True)
    if not keep_alpha:
        pixels[..., :3] *= pixels[..., 3:4]
        pixels[..., 3] = 1.0
    return transform(pixels, output)
