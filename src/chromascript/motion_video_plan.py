"""Freeze processing, encoder and rate control once at submission, never mid-video."""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any, Literal

from .encoding import available_encoders
from .errors import ChromaError
from .models import DeliveryPreset, Model, MotionRenderSettings
from .motion import MotionEngine, MotionOutput, motion_video_preset
from .video_hardware import hardware_probe
from .video_transport import VideoPixelFormat


class VideoExecution(Model):
    processing: Literal["reference", "gpu"]
    pixel_format: VideoPixelFormat | None = None
    preset: DeliveryPreset
    speed: Literal["slow", "medium", "fast"] = "slow"
    hardware: bool = False
    capability: dict[str, Any] | None = None
    fallback_reason: str | None = None


def video_bitrate(settings: MotionRenderSettings, quality: str) -> int:
    # A content-dependent target, not a file-size cap. Default quality retains
    # the existing nominal 0.2 bit/pixel/frame planning rate and 8 Mb/s floor.
    density = {"quality": Fraction(1, 5), "balanced": Fraction(7, 50), "speed": Fraction(1, 10)}[quality]
    rate = Fraction(settings.fps.numerator, settings.fps.denominator)
    return max(8000, math.ceil(settings.width * settings.height * rate * density / 1000))


def resolve_video_execution(engine: MotionEngine, output: MotionOutput,
                            settings: MotionRenderSettings) -> VideoExecution | None:
    if output.video == "none":
        return None
    preset = motion_video_preset(output.video)
    gpu = engine == "three-float-v1" and output.master_bit_depth is None and output.video_processing == "auto"
    pixel_format: VideoPixelFormat | None = None
    if gpu:
        pixel_format = ("p416le" if output.video == "prores-4444-acescct" else
                        "p210le" if output.video.startswith("prores-hq-") else
                        "nv12" if output.video == "srgb" else "p010le")
    plan = VideoExecution(processing="gpu" if gpu else "reference", pixel_format=pixel_format, preset=preset)
    if preset.codec not in {"libx264", "libx265"}:
        if output.video_encoder == "hardware":
            raise ChromaError("motion_output", "此格式使用专用编码路径，请选择自动编码。")
        return plan
    speeds: dict[str, Literal["slow", "medium", "fast"]] = {"quality": "slow", "balanced": "medium", "speed": "fast"}
    plan.speed = speeds[output.video_quality]
    plan.preset = preset.model_copy(update={"crf": {"quality": 18, "balanced": 20, "speed": 23}[output.video_quality]})
    if output.video_encoder == "software":
        return plan
    codec = "h264_videotoolbox" if preset.codec == "libx264" else "hevc_videotoolbox"
    hardware = preset.model_copy(update={"codec": codec, "crf": None,
                                         "bitrate_kbps": video_bitrate(settings, output.video_quality)})
    evidence: dict[str, Any] = (hardware_probe(hardware, settings, plan.speed) if codec in available_encoders() else
                {"available": False, "reason": f"本机 FFmpeg 缺少 {codec}。"})
    if evidence["available"]:
        plan.preset = hardware
        plan.hardware = True
        plan.capability = evidence
    elif output.video_encoder == "hardware":
        raise ChromaError("video_hardware", evidence["reason"])
    else:
        plan.fallback_reason = evidence["reason"]
    return plan
