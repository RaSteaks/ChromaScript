"""Atomic image I/O with explicit chromaticities, storage depth, and alpha association."""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import OpenImageIO as oiio
from numpy.typing import NDArray
from PIL import Image, ImageCms

from .color import output_metadata
from .errors import ChromaError

CHROMATICITIES = {
    "ACEScg": (0.713, 0.293, 0.165, 0.830, 0.128, 0.044, 0.32168, 0.33767),
    "ACES2065-1": (0.7347, 0.2653, 0.0, 1.0, 0.0001, -0.077, 0.32168, 0.33767),
}
# ACEScct is AP1 with a log curve, not an AP0 ACES image container.
CHROMATICITIES["ACEScct"] = CHROMATICITIES["ACEScg"]
DPX_GAMMA_OFFSET = 1948  # SMPTE ST 268 television header, field 68 (R32).


@lru_cache(maxsize=1)
def srgb_profile() -> bytes:
    """Embed a real RGB/TRC profile without applying a second pixel transform."""
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def resize_srgb(image: NDArray[np.float32], max_dimension: int) -> NDArray[np.float32]:
    """Downsample an already-rendered, straight sRGB display copy in linear light."""
    height, width, _ = image.shape
    scale = min(1.0, max_dimension / max(width, height))
    if scale == 1:
        return image
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    result = np.empty((size[1], size[0], 4), dtype=np.float32)
    alpha = np.clip(image[..., 3], 0, 1)
    # Float planes defer quantization until PNG writing. BOX uses nonnegative box
    # weights, keeping filtered alpha in [0,1] without ringing at transparent edges.
    result[..., 3] = np.asarray(Image.fromarray(alpha).resize(size, Image.Resampling.BOX))
    for channel in range(3):
        encoded = np.clip(image[..., channel], 0, 1)
        linear = np.where(encoded <= 0.04045, encoded / 12.92, ((encoded + 0.055) / 1.055) ** 2.4)
        associated = np.asarray(Image.fromarray(linear * alpha).resize(size, Image.Resampling.BOX))
        # Only undo/reapply the sRGB transfer: ACES rendering has already happened.
        # Alpha weights light but never receives a transfer function itself.
        straight = np.divide(
            associated, result[..., 3], out=np.zeros_like(associated), where=result[..., 3] > 0
        )
        result[..., channel] = np.where(
            straight <= 0.0031308, straight * 12.92, 1.055 * straight ** (1 / 2.4) - 0.055
        )
    return result


def _dpx_byte_order(header: bytes) -> str:
    if header[:4] == b"SDPX":
        return ">"
    if header[:4] == b"XPDS":
        return "<"
    raise ChromaError("image_metadata", "Invalid DPX header")


def write_image(
    path: Path,
    image: NDArray[np.float32],
    *,
    space: str = "ACEScg",
    depth: int = 32,
    compression: str = "zip",
    alpha_mode: str = "straight",
    config_hash: str = "",
    fps: tuple[int, int] | None = None,
    frame_index: int | None = None,
    timecode_start: str | None = None,
) -> dict[str, Any]:
    if not np.isfinite(image).all():
        raise ChromaError("non_finite_pixels", "Cannot store NaN or infinity")
    pixels = np.array(image, dtype=np.float32, copy=True, order="C")
    if pixels.shape[2] == 4 and alpha_mode == "premultiplied":
        pixels[..., :3] *= pixels[..., 3:4]
    is_exr = path.suffix.lower() == ".exr"
    if is_exr and depth == 16 and np.max(np.abs(pixels)) > 65504:
        raise ChromaError("half_overflow", "Values exceed half precision range; choose float EXR")
    storage = (
        oiio.FLOAT
        if depth == 32
        else oiio.HALF
        if is_exr
        else oiio.UINT16
        if depth > 8
        else oiio.UINT8
    )
    height, width, channels = pixels.shape
    spec = oiio.ImageSpec(width, height, channels, storage)
    if fps:
        spec.attribute("FramesPerSecond", oiio.TypeRational, fps)
    if frame_index is not None:
        spec.attribute("chromascript:frame_index", frame_index)
    if timecode_start is not None:
        spec.attribute("chromascript:timecode_start", timecode_start)
    # OIIO's strict ACES-container writer requires the canonical interoperability ID,
    # rather than an equivalent human-readable OCIO alias.
    storage_space = {"ACEScg": "lin_ap1_scene", "ACES2065-1": "lin_ap0_scene"}.get(space, space)
    spec.attribute("oiio:ColorSpace", storage_space)
    encoding = output_metadata(space)
    spec.attribute("chromascript:color_space", space)
    spec.attribute("chromascript:output_color_space", encoding["color_space"])
    spec.attribute("chromascript:gamma", encoding["gamma"])
    spec.attribute("chromascript:alpha_mode", alpha_mode)
    spec.attribute("chromascript:config_sha256", config_hash)
    spec.attribute("oiio:UnassociatedAlpha", int(alpha_mode == "straight"))
    if is_exr:
        spec.attribute("compression", compression)
        if space in CHROMATICITIES:
            spec.attribute("chromaticities", oiio.TypeDesc("float[8]"), CHROMATICITIES[space])
        # AP1 data must not receive the AP0 ACES image-container flag.
        # SMPTE ACES containers require uncompressed half storage. Other AP0 EXRs
        # remain correctly tagged AP0 intermediates, without claiming container compliance.
        if space == "ACES2065-1" and depth == 16 and compression == "none":
            spec.attribute("acesImageContainerFlag", 1)
    elif path.suffix.lower() == ".dpx":
        spec.attribute("oiio:BitsPerSample", depth)
        # Rec.1886 is a display gamma, not the BT.709 camera OETF. DPX has
        # no enum for gamma 2.4; use user-defined transfer plus its numeric gamma.
        spec.attribute("dpx:Colorimetric", "ITU-R 709-4")
        spec.attribute("dpx:Transfer", "User defined")
        spec.attribute("ImageDescription", "Rec.709 / Gamma 2.4 (Rec.1886)")
    elif path.suffix.lower() in {".tif", ".tiff"} and space == "sRGB":
        # TIFF drops arbitrary attributes. ICC tag 34675 preserves its actual
        # primaries and piecewise transfer in a standard, portable file field.
        profile = srgb_profile()
        spec.attribute("ICCProfile", oiio.TypeDesc(f"uint8[{len(profile)}]"), profile)
    if not is_exr:
        # Integer display formats are quantized only after their output transform.
        scale = 65535 if depth > 8 else 255
        pixels = np.rint(np.clip(pixels, 0, 1) * scale).astype(np.uint16 if depth > 8 else np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(path.parent).free < pixels.nbytes + 128 * 1024**2:
        raise ChromaError("disk_space", "Not enough free space to safely write this frame")
    partial = path.with_name(path.stem + ".partial" + path.suffix)
    output = oiio.ImageOutput.create(str(partial))
    if output is None:
        raise ChromaError("image_format", oiio.geterror())
    try:
        if not output.open(str(partial), spec):
            raise ChromaError("image_write", output.geterror())
        if not output.write_image(pixels):
            raise ChromaError("image_write", output.geterror())
        if not output.close():
            raise ChromaError("image_write", output.geterror())
        if path.suffix.lower() == ".dpx":
            # OIIO 3.1 does not expose the television gamma field. Patch only
            # that standard float in the private partial file, before publication.
            with partial.open("r+b") as stream:
                header = stream.read(DPX_GAMMA_OFFSET + 4)
                if len(header) != DPX_GAMMA_OFFSET + 4:
                    raise ChromaError("image_metadata", "DPX television header is missing")
                order = _dpx_byte_order(header)
                stream.seek(DPX_GAMMA_OFFSET)
                stream.write(struct.pack(order + "f", 2.4))
        os.replace(partial, path)
    except Exception:
        output.close()
        partial.unlink(missing_ok=True)
        raise
    return {"sha256": file_hash(path), "size_bytes": path.stat().st_size}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_image(path: Path, *, straight: bool = True) -> tuple[NDArray[np.float32], dict[str, Any]]:
    # PNG/TIFF readers otherwise implicitly associate straight alpha. Keep stored
    # samples untouched; explicit file association is handled below exactly once.
    options = oiio.ImageSpec()
    options.attribute("oiio:UnassociatedAlpha", 1)
    source = oiio.ImageInput.open(str(path), options)
    if source is None:
        raise ChromaError("image_read", oiio.geterror())
    try:
        spec = source.spec()
        pixels = np.asarray(source.read_image(format=oiio.FLOAT), dtype=np.float32)
        metadata: dict[str, Any] = {a.name: a.value for a in spec.extra_attribs}
        if "ICCProfile" in metadata:
            profile = bytes(metadata["ICCProfile"])
            expected_profile = srgb_profile()
            # Creation time may vary between workers; compare every colorimetric
            # byte, rather than trusting the profile's human-readable name.
            metadata["ICCProfile"] = {
                "size_bytes": len(profile),
                "sha256": hashlib.sha256(profile).hexdigest(),
                "matches_srgb": profile[:24] + profile[36:]
                == expected_profile[:24] + expected_profile[36:],
            }
        if source.format_name() == "png":
            # OIIO assumes sRGB for untagged PNGs. Read native chunks as evidence
            # so that an absent tag cannot pass a delivery check by that default.
            with Image.open(path) as png:
                for key, name in [
                    ("srgb", "png:sRGB"),
                    ("gamma", "png:Gamma"),
                    ("chromaticity", "png:Chromaticities"),
                ]:
                    if key in png.info:
                        metadata[name] = png.info[key]
        elif source.format_name() == "dpx":
            with path.open("rb") as stream:
                header = stream.read(DPX_GAMMA_OFFSET + 4)
            if len(header) == DPX_GAMMA_OFFSET + 4:
                gamma = struct.unpack_from(_dpx_byte_order(header) + "f", header, DPX_GAMMA_OFFSET)[
                    0
                ]
                if np.isfinite(gamma):
                    metadata["dpx:Gamma"] = gamma
        # TIFF stores association in its standard ExtraSamples flag, and does not
        # retain our custom EXR attribute. Infer it before returning straight RGB.
        if source.format_name() == "tiff" and spec.alpha_channel >= 0:
            metadata["chromascript:alpha_mode"] = (
                "straight" if metadata.get("tiff:UnassociatedAlpha", 0) else "premultiplied"
            )
        metadata.update(
            width=spec.width,
            height=spec.height,
            channels=spec.nchannels,
            storage=str(spec.format),
            channel_names=list(spec.channelnames),
        )
    finally:
        source.close()
    if (
        straight
        and pixels.shape[2] == 4
        and metadata.get("chromascript:alpha_mode") == "premultiplied"
    ):
        alpha = pixels[..., 3:4]
        pixels[..., :3] = np.divide(
            pixels[..., :3], alpha, out=np.zeros_like(pixels[..., :3]), where=alpha > 0
        )
    return pixels, metadata
