"""Identical float decoding and verified master storage for serial/parallel exports."""

from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

import numpy as np

from .encoding import check_image_metadata
from .errors import ChromaError
from .images import read_image, write_image
from .motion import MotionOutput, MotionSpec
from .motion_color import decode_float_frame
from .presets import builtins


@dataclass
class PreparedFrame:
    index: int
    pixels: np.ndarray
    record: dict[str, Any] | None
    seconds: dict[str, float]


def prepare_float_frame(
    index: int, raw: Path, spec: MotionSpec, output: MotionOutput,
    config_hash: str, master: Path | None = None,
) -> PreparedFrame:
    start = monotonic()
    pixels = decode_float_frame(raw.read_bytes(), spec.width, spec.height,
                                output.render_scale, spec.source_color_space)
    seconds = {"float_decode": monotonic() - start}
    if master is None:
        return PreparedFrame(index, pixels, None, seconds)
    depth = output.master_bit_depth
    assert depth is not None
    start = monotonic()
    saved = write_image(master, pixels, depth=depth, config_hash=config_hash,
                        fps=(spec.fps.numerator, spec.fps.denominator), frame_index=index,
                        disk_reserve_bytes=0)
    seconds["exr_write_and_hash"] = monotonic() - start
    start = monotonic()
    decoded, metadata = read_image(master)
    expected = pixels if depth == 32 else pixels.astype(np.float16).astype(np.float32)
    issues = check_image_metadata(metadata, builtins()[f"exr-{depth}-zip"])
    if issues or not np.array_equal(decoded, expected):
        raise ChromaError("verification_failed", "EXR 实际存储或像素回读不符。", issues=issues)
    record = {
        **saved, "metadata": metadata,
        "minimum": decoded.min(axis=(0, 1)).tolist(),
        "maximum": decoded.max(axis=(0, 1)).tolist(),
        "high_rgb_values": int((decoded[..., :3] > 1).sum()),
        "negative_rgb_values": int((decoded[..., :3] < 0).sum()),
        "round_trip_max_error": float(np.max(np.abs(decoded - pixels))), "passed": True,
    }
    seconds["exr_verify"] = monotonic() - start
    # The video consumer receives actual stored pixels, including Half rounding.
    return PreparedFrame(index, decoded, record, seconds)
