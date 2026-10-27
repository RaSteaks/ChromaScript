"""Temporal integration of bounded SDR screenshot batches, never an HDR master."""

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def combine_sdr_samples(paths: list[Path], destination: Path) -> None:
    total: np.ndarray | None = None
    for path in paths:
        with Image.open(path) as image:
            rgba = np.asarray(image.convert("RGBA"), dtype=np.float32) / np.float32(255)
        rgb = rgba[..., :3]
        linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
        # Screenshot RGB is straight sRGB. Integrate associated light/coverage;
        # alpha is never transfer-encoded and hidden RGB cannot leak into edges.
        rgba[..., :3] = linear * rgba[..., 3:4]
        if total is None:
            total = rgba
        else:
            total += rgba
    if total is None:
        raise ValueError("Temporal sample list is empty")
    total /= len(paths)
    alpha = total[..., 3:4]
    linear = np.divide(total[..., :3], alpha, out=np.zeros_like(total[..., :3]), where=alpha > 0)
    total[..., :3] = np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)
    # The source remains 8-bit screenshot precision. Quantize once after temporal
    # integration; preview/export consume this exact resulting PNG.
    Image.fromarray(np.rint(np.clip(total, 0, 1) * 255).astype(np.uint8)).save(destination)


if __name__ == "__main__":
    for group in json.loads(Path(sys.argv[1]).read_text()):
        combine_sdr_samples([Path(path) for path in group["samples"]], Path(group["output"]))
