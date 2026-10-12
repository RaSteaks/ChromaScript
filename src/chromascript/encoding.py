"""Encode already display-rendered RGB; zscale only changes matrix/range and sampling."""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
from numpy.typing import NDArray

from .color import tags
from .errors import ChromaError
from .models import DeliveryPreset, RenderSettings


def available_encoders() -> set[str]:
    executable = shutil.which("ffmpeg")
    if not executable:
        return set()
    result = subprocess.run(
        [executable, "-hide_banner", "-encoders"], capture_output=True, text=True, check=True
    )
    return {
        line.split()[1]
        for line in result.stdout.splitlines()
        if len(line.split()) > 1 and line.lstrip().startswith("V")
    }


def command(preset: DeliveryPreset, settings: RenderSettings, destination: Path) -> list[str]:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ChromaError("ffmpeg_unavailable", "Install FFmpeg and add it to PATH")
    color_tags = tags(preset.color.output)
    pixel = preset.pixel_format
    rgb = pixel.startswith(("gbr", "rgb", "bgr"))
    alpha = preset.alpha
    if rgb:
        video_filter = f"format={pixel}"
    else:
        source_format = "gbrap16le" if alpha else "gbrp16le"
        video_filter = (
            f"format={source_format},zscale=matrixin=gbr:matrix={color_tags['zscale_matrix']}:"
            f"rangein=full:range={preset.data_range}:dither=error_diffusion,format={pixel}"
        )
    # Frame metadata from filters takes precedence over encoder defaults. Stamp the
    # already-rendered transfer/primaries on frames without applying another curve.
    video_filter += (
        f",setparams=range={preset.data_range}:"
        f"color_primaries={color_tags['primaries']}:color_trc={color_tags['transfer']}:"
        f"colorspace={'gbr' if rgb else color_tags['matrix']}"
    )
    args = [
        executable,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "warning",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgba64le",
        "-video_size",
        f"{settings.width}x{settings.height}",
        "-framerate",
        settings.fps.ffmpeg,
        "-i",
        "pipe:0",
        "-an",
        "-vf",
        video_filter,
        "-c:v",
        preset.codec,
    ]
    if preset.profile is not None:
        args += ["-profile:v", preset.profile]
    if preset.codec in {"libx264", "libx265"}:
        args += [
            "-preset",
            "slow",
            "-g",
            str(max(1, round(settings.fps.value * preset.gop_seconds))),
        ]
        if preset.crf is not None:
            args += ["-crf", str(preset.crf)]
        if preset.bitrate_kbps is not None:
            args += ["-b:v", f"{preset.bitrate_kbps}k"]
    if preset.codec == "libx265":
        args += ["-tag:v", "hvc1", "-x265-params", "log-level=error:pools=4"]
    if preset.codec == "ffv1":
        args += ["-level", "3", "-coder", "1", "-context", "1"]
    args += [
        "-pix_fmt",
        pixel,
        "-r",
        settings.fps.ffmpeg,
        "-fps_mode",
        "cfr",
        "-color_primaries",
        color_tags["primaries"],
        "-color_trc",
        color_tags["transfer"],
        "-colorspace",
        "rgb" if rgb else color_tags["matrix"],
        "-color_range",
        "pc" if preset.data_range == "full" else "tv",
    ]
    if preset.container in {"mov", "mp4"}:
        args += ["-movflags", "+faststart+write_colr", "-timecode", settings.timecode]
    if preset.container == "mxf":
        args += ["-timecode", settings.timecode, "-f", "mxf"]
    args += ["-threads", "4", str(destination)]
    return args


def encode(
    preset: DeliveryPreset,
    settings: RenderSettings,
    path: Path,
    frames: Iterable[NDArray[np.float32]],
    progress: Callable[[int], None],
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.stem + ".partial" + path.suffix)
    log_path = path.with_suffix(path.suffix + ".ffmpeg.log")
    args = command(preset, settings, partial)
    count = 0
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log
        )
        try:
            assert process.stdin is not None
            for frame in frames:
                if (
                    frame.shape != (settings.height, settings.width, 4)
                    or not np.isfinite(frame).all()
                ):
                    raise ChromaError("encoder_input", "Invalid frame delivered to encoder")
                # Quantization occurs after the ACES output transform, preserving 16-bit
                # encoder input. Lossy output precision is reported separately.
                pixels = np.rint(np.clip(frame, 0, 1) * 65535).astype("<u2")
                process.stdin.write(pixels.tobytes())
                count += 1
                progress(count)
            process.stdin.close()
            return_code = process.wait()
            if return_code:
                raise ChromaError(
                    "encode_failed",
                    log_path.read_text(errors="replace")[-8000:],
                    command=args,
                    return_code=return_code,
                )
            partial.replace(path)
        except Exception as exc:
            if process.stdin and not process.stdin.closed:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass  # Preserve the native codec error which closed the input pipe.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            partial.unlink(missing_ok=True)
            if isinstance(exc, BrokenPipeError):
                raise ChromaError(
                    "encode_failed",
                    log_path.read_text(errors="replace")[-8000:],
                    command=args,
                    return_code=process.returncode,
                ) from exc
            raise
    return probe(path)


def probe(path: Path, count_frames: bool = True) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise ChromaError("ffprobe_unavailable", "ffprobe is required for delivery verification")
    args = [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json"]
    if count_frames:
        args += ["-count_frames"]
    result = subprocess.run([*args, str(path)], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ChromaError("probe_failed", result.stderr[-4000:])
    return json.loads(result.stdout)


def check_probe(
    info: dict[str, Any], preset: DeliveryPreset, settings: RenderSettings
) -> dict[str, Any]:
    from fractions import Fraction

    video = next((stream for stream in info["streams"] if stream["codec_type"] == "video"), None)
    if not video:
        raise ChromaError("delivery_verification", "Output has no video stream")
    issues = []
    expected_codec = {"libx264": "h264", "libx265": "hevc", "prores_ks": "prores"}.get(
        preset.codec, preset.codec
    )
    if video.get("codec_name") != expected_codec:
        issues.append("codec mismatch")
    # QuickTime/MP4 timecode may live on a separate tmcd stream, MXF on the container.
    timecodes = [stream.get("tags", {}).get("timecode") for stream in info["streams"]]
    timecodes.append(info.get("format", {}).get("tags", {}).get("timecode"))
    if preset.container in {"mov", "mp4", "mxf"} and settings.timecode not in timecodes:
        issues.append("timecode mismatch")
    if (video["width"], video["height"]) != (settings.width, settings.height):
        issues.append("dimension mismatch")
    # MXF often reports avg_frame_rate=0/0. Its edit rate and exact stream
    # duration still provide independent evidence, rather than inventing a rate.
    actual_rate = None
    rate_source = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            candidate = Fraction(str(video.get(key, "0/0")))
        except (ValueError, ZeroDivisionError):
            continue
        if candidate > 0:
            actual_rate, rate_source = candidate, key
            break
    expected_rate = Fraction(settings.fps.numerator, settings.fps.denominator)
    if actual_rate != expected_rate:
        issues.append("frame-rate mismatch")
    if rate_source == "r_frame_rate" and "duration_ts" in video:
        try:
            duration = Fraction(video["duration_ts"]) * Fraction(video["time_base"])
            if duration != Fraction(settings.frames, 1) / expected_rate:
                issues.append("stream duration does not match frame count and rate")
        except (KeyError, ValueError, ZeroDivisionError):
            issues.append("stream duration could not be verified")
    count = video.get("nb_read_frames") or video.get("nb_frames")
    if count is None or int(count) != settings.frames:
        issues.append("frame-count mismatch")
    expected = tags(preset.color.output)
    for key, value in [
        ("color_primaries", expected["primaries"]),
        ("color_transfer", expected["transfer"]),
    ]:
        # Some legacy uncompressed MOV encoders cannot carry all color tags. The file
        # remains experimental instead of being falsely certified by encoder presence.
        if video.get(key) != value:
            issues.append(f"{key}: expected {value}, got {video.get(key)}")
    expected_matrix = (
        "gbr" if preset.pixel_format.startswith(("gbr", "rgb", "bgr")) else expected["matrix"]
    )
    if video.get("color_space") != expected_matrix:
        issues.append(f"matrix mismatch: {video.get('color_space')}")
    expected_range = "pc" if preset.data_range == "full" else "tv"
    actual_range = video.get("color_range")
    implicit_range = actual_range is None and video.get("codec_name") in {
        "prores",
        "dnxhd",
        "v210",
        "r210",
    }
    # QuickTime nclc and these intra codecs do not expose a separate range flag.
    # Their RGB/full or YCbCr/legal convention is also tested with encoded ramps.
    if actual_range != expected_range and not implicit_range:
        issues.append(f"range mismatch: {video.get('color_range')}")
    # Decoder storage may be wider than the encoder input (notably ProRes 4444).
    # Check minimum precision while reporting both quantities separately.
    import re

    storage = video.get("pix_fmt", "")
    depth_match = re.search(r"(?:p|rgb|rgba|gbrp|gray)(10|12|16|48|64)(?:le|be)?$", storage)
    actual_depth = int(video.get("bits_per_raw_sample", "0") or 0)
    if depth_match:
        actual_depth = max(
            actual_depth, {48: 16, 64: 16}.get(int(depth_match[1]), int(depth_match[1]))
        )
    if not actual_depth:
        actual_depth = 8
    if actual_depth < preset.bit_depth:
        issues.append(
            f"precision mismatch: decoded {actual_depth}-bit < requested {preset.bit_depth}-bit"
        )
    if preset.alpha and "a" not in video.get("pix_fmt", ""):
        issues.append("missing alpha channel")
    return {
        "passed": not issues,
        "issues": issues,
        "video": video,
        "frame_rate_source": rate_source,
        "verified_frame_rate": str(actual_rate) if actual_rate is not None else None,
        "render_precision": "float32",
        "encoder_input_precision": "16-bit RGB",
        "codec_input_precision": f"{preset.bit_depth}-bit",
        "decoded_storage_precision": f"{actual_depth}-bit",
        "range_signaling": "codec convention" if implicit_range else "explicit",
        "effective_range": expected_range,
        "container": preset.container,
    }


@lru_cache(maxsize=64)
def pixel_format_info(name: str) -> dict[str, Any]:
    """Use the installed encoder's descriptor instead of inferring precision from a label."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_pixel_formats", "-of", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    for item in json.loads(result.stdout)["pixel_formats"]:
        if item["name"] == name:
            return item
    raise ChromaError("pixel_format", f"Unknown pixel format: {name}")


def check_image_metadata(metadata: dict[str, Any], preset: DeliveryPreset) -> list[str]:
    """Verify native storage, rather than equating an extension with a delivery depth."""
    from .images import CHROMATICITIES

    issues = []
    depths = {"half": 16, "float": 32, "uint8": 8, "uint16": 16}
    actual_depth = int(
        metadata.get("oiio:BitsPerSample", depths.get(str(metadata.get("storage", "")), 0))
    )
    if actual_depth != preset.bit_depth:
        issues.append(f"image precision mismatch: expected {preset.bit_depth}, got {actual_depth}")
    if metadata.get("channels") != (4 if preset.alpha else 3):
        issues.append("image channel count mismatch")
    if preset.container == "exr":
        if metadata.get("compression") != preset.compression:
            issues.append("EXR compression mismatch")
        expected = CHROMATICITIES.get(preset.color.output)
        actual_chroma = metadata.get("chromaticities")
        if actual_chroma is None:
            actual_chroma = []
        if expected and (
            np.shape(actual_chroma) != (8,) or not np.allclose(actual_chroma, expected, atol=1e-7)
        ):
            issues.append("EXR chromaticities mismatch")
        if preset.id == "aces2065-archive" and metadata.get("acesImageContainerFlag") != 1:
            issues.append("missing ACES image container flag")
    if preset.alpha and preset.container in {"png", "tiff"}:
        associated = not metadata.get("oiio:UnassociatedAlpha", 0)
        if associated != (preset.alpha_mode == "premultiplied"):
            issues.append("image alpha association mismatch")
    return issues
