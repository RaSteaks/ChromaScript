"""Transient, bounded draft previews; no revisions, frame caches or jobs are written."""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image, PngImagePlugin
from pydantic import Field

from .color import SDR_VIEW, transform
from .models import Model, Rational, RenderSettings, SceneSpec

PREVIEW_MAX_DIMENSION = 1280
PREVIEW_SHORT_DIMENSION = 720


class LivePreviewRequest(Model):
    spec: SceneSpec
    width: int = Field(default=3840, ge=16, le=8192)
    height: int = Field(default=2160, ge=16, le=8192)
    fps: Rational = Field(default_factory=Rational)
    frame: int = Field(default=0, ge=0, le=10_000_000)

    def settings(self) -> RenderSettings:
        # Fit a landscape/portrait 720p raster without upscaling small sources.
        # Bounding both edges also prevents square drafts exceeding the pixel budget.
        scale = min(
            1.0, PREVIEW_MAX_DIMENSION / max(self.width, self.height),
            PREVIEW_SHORT_DIMENSION / min(self.width, self.height),
        )
        return RenderSettings(
            width=max(16, round(self.width * scale)),
            height=max(16, round(self.height * scale)),
            fps=self.fps,
            frames=1,
            samples=1,
        )


def preview_image(image: NDArray[np.float32]) -> dict[str, Any]:
    """Encode only a display copy; alpha bypasses OCIO and stays unassociated."""
    display = transform(image, "sRGB")
    pixels = np.rint(np.clip(display, 0, 1) * 255).astype(np.uint8)
    metadata = PngImagePlugin.PngInfo()
    metadata.add(b"sRGB", b"\x00")
    stream = BytesIO()
    # Fast in-memory PNG avoids EXR I/O, durable artifact records and video encoding.
    Image.fromarray(pixels).save(stream, format="PNG", pnginfo=metadata, compress_level=1)
    return {
        "data": base64.b64encode(stream.getvalue()).decode("ascii"),
        "mime_type": "image/png",
        "width": image.shape[1],
        "height": image.shape[0],
        "display": "sRGB",
        "view": SDR_VIEW,
        "alpha_mode": "straight",
        "bit_depth": 8,
    }
