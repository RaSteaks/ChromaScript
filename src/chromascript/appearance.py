"""Display-referred OKLCH picking, kept separate from scene-linear float values."""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Annotated, Any, Literal

import numpy as np
import PyOpenColorIO as ocio
from pydantic import Field, model_validator

from .color import DISPLAYS, SDR_VIEW, color_info, config, transform
from .errors import ChromaError
from .models import Model

# Verify appearance within half an 8-bit display code value, not scene-float
# precision. The actual OCIO round-trip error is returned for every selection.
PICK_PREVIEW_TOLERANCE = 0.5 / 255


class OklchColor(Model):
    # OKLCH is a D65 perceptual space for this SDR picker, not an HDR appearance model.
    lightness: float = Field(ge=0, le=1, strict=True)
    chroma: float = Field(ge=0, le=0.4, strict=True)
    hue: float = Field(ge=0, le=360, strict=True)


class ColorAppearanceRequest(Model):
    color_space: Literal["ACEScg", "sRGB"] = "ACEScg"
    rgb: (
        list[
            Annotated[
                float, Field(ge=-3.4028234663852886e38, le=3.4028234663852886e38, strict=True)
            ]
        ]
        | None
    ) = Field(default=None, min_length=3, max_length=3)
    oklch: OklchColor | None = None

    @model_validator(mode="after")
    def one_representation(self) -> ColorAppearanceRequest:
        if (self.rgb is None) == (self.oklch is None):
            raise ValueError("Provide exactly one of rgb or oklch")
        return self


def oklch_to_linear(lightness: float, chroma: float, hue: float) -> np.ndarray:
    # Ottosson's 2021-01-25 D65 matrices; mirrored in the browser only for palette drawing.
    # https://bottosson.github.io/posts/oklab/#converting-from-linear-srgb-to-oklab
    angle = math.radians(hue)
    a, b = chroma * math.cos(angle), chroma * math.sin(angle)
    cone_l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return np.array(
        [
            4.0767416621 * cone_l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * cone_l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * cone_l - 0.7034186147 * m + 1.7076147010 * s,
        ]
    )


def srgb_to_oklch(rgb: list[float]) -> dict[str, float]:
    encoded = np.array(rgb, dtype=np.float64)
    linear = np.where(encoded <= 0.04045, encoded / 12.92, ((encoded + 0.055) / 1.055) ** 2.4)
    cone_l, m, s = np.cbrt(
        np.array(
            [
                [0.4122214708, 0.5363325363, 0.0514459929],
                [0.2119034982, 0.6806995451, 0.1073969566],
                [0.0883024619, 0.2817188376, 0.6299787005],
            ]
        )
        @ linear
    )
    lightness = 0.2104542553 * cone_l + 0.7936177850 * m - 0.0040720468 * s
    a = 1.9779984951 * cone_l - 2.4285922050 * m + 0.4505937099 * s
    b = 0.0259040371 * cone_l + 0.7827717662 * m - 0.8086757660 * s
    chroma = math.hypot(a, b)
    return {
        "lightness": float(np.clip(lightness, 0, 1)),
        "chroma": 0.0 if chroma < 4e-6 else chroma,
        "hue": 0.0 if chroma < 4e-6 else math.degrees(math.atan2(b, a)) % 360,
    }


def in_gamut(linear: np.ndarray) -> bool:
    return bool(np.all(linear >= -1e-7) and np.all(linear <= 1 + 1e-7))


def gamut_map(color: OklchColor) -> tuple[list[float], dict[str, float], bool]:
    lightness, chroma, hue = color.lightness, color.chroma, color.hue
    linear = oklch_to_linear(lightness, chroma, hue)
    mapped = not in_gamut(linear)
    if mapped:
        # Reduce only chroma at fixed L/h to the sRGB boundary. This is deliberately
        # strict containment, not CSS's perceptual-tolerance gamut mapping algorithm.
        low, high = 0.0, chroma
        for _ in range(28):
            middle = (low + high) / 2
            if in_gamut(oklch_to_linear(lightness, middle, hue)):
                low = middle
            else:
                high = middle
        chroma = low
        linear = oklch_to_linear(lightness, chroma, hue)
    linear = np.clip(linear, 0, 1)
    encoded = np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - 0.055)
    return encoded.tolist(), {"lightness": lightness, "chroma": chroma, "hue": hue % 360}, mapped


@lru_cache(maxsize=1)
def inverse_preview() -> Any:
    # Invert the SAME display view used by frame previews. A utility sRGB input
    # conversion alone would apply a new tone map and change the picked appearance.
    return (
        config()
        .getProcessor(
            ocio.DisplayViewTransform(
                src="ACEScg",
                display=DISPLAYS["sRGB"],
                view=SDR_VIEW,
                direction=ocio.TRANSFORM_DIR_INVERSE,
            )
        )
        .getDefaultCPUProcessor()
    )


def resolve_color(request: ColorAppearanceRequest) -> dict[str, Any]:
    mapped = False
    error = None
    if request.oklch is not None:
        target, appearance, mapped = gamut_map(request.oklch)
        aces = np.array(inverse_preview().applyRGB(target), dtype=np.float32)
        # An sRGB-declared parameter still feeds the scene's utility input transform;
        # store its inverse utility encoding, not the display-rendered swatch.
        rgb = (
            aces.tolist()
            if request.color_space == "ACEScg"
            else transform(aces, "sRGB Encoded Rec.709 (sRGB)").tolist()
        )
    else:
        # Inspection is read-only: preserve negative, >1 and original Python float
        # values exactly. Only the separate display swatch may be clipped.
        assert request.rgb is not None
        rgb = list(request.rgb)

    displayed = transform(np.array(rgb, dtype=np.float32), "sRGB", request.color_space)
    clipped = bool(np.any(displayed < -1e-5) or np.any(displayed > 1 + 1e-5))
    swatch = np.clip(displayed, 0, 1).tolist()
    if request.oklch is not None:
        error = float(np.max(np.abs(np.array(swatch) - target)))
        if error > PICK_PREVIEW_TOLERANCE:
            raise ChromaError(
                "color_pick_roundtrip", "Picked color could not match the ACES preview"
            )
    else:
        appearance = srgb_to_oklch(swatch)
    return {
        "model": "OKLCH",
        "color_space": request.color_space,
        "rgb": rgb,
        "oklch": appearance,
        "display_rgb": swatch,
        "gamut_mapped": mapped,
        "preview_clipped": clipped,
        "extended_range": any(v < 0 or v > 1 for v in rgb),
        "round_trip_error": error,
        "display": "sRGB",
        "view": SDR_VIEW,
        "config": color_info()["sha256"],
    }
