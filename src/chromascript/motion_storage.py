"""Content measurements and nominal storage estimates, never safety multipliers."""

from __future__ import annotations

import math
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any

from .audio import audio_sample_window, read_timeline
from .models import MotionRenderSettings
from .store import Store, canonical, now


def estimate_storage(
    store: Store, settings: MotionRenderSettings, engine: str, output: Any,
    destination: Path, project_id: str | None,
    measurements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .motion import prores_hq_frame_bits

    volume = destination
    while not volume.exists():
        volume = volume.parent
    floating = engine == "three-float-v1"
    pixels, count = settings.width * settings.height, settings.frames
    duration = Fraction(count * settings.fps.denominator, settings.fps.numerator)
    master_frame = pixels * 4 * (output.master_bit_depth or 0) // 8 if floating else 0
    uncompressed = master_frame * count
    master = uncompressed
    measured = measurements or {}
    sample_count = measured.get("sample_count", 0)
    if sample_count and "master_bytes" in measured:
        master = math.ceil(Fraction(sum(measured["master_bytes"]) * count, sample_count))
    prores = output.video.startswith("prores-")
    video = 0
    video_planning_bps: int | None = None
    unknown = ["交付元数据、日志及预览图的最终大小尚未测量"]
    if output.video != "none":
        if output.video == "prores-4444-acescct":
            # Apple's nominal 330 Mb/s at 1080p29.97. No quality/rate control is
            # changed by this estimate; integer frame counts avoid duration drift.
            bits = Fraction(330_000_000 * 1001 * pixels, 30000 * 1920 * 1080)
            video = math.ceil(bits * count / 8)
            video_planning_bps = math.ceil(bits * Fraction(settings.fps.numerator, settings.fps.denominator))
        elif prores:
            bits = Fraction(prores_hq_frame_bits(settings.width, settings.height))
            video = math.ceil(bits * count / 8)
            video_planning_bps = math.ceil(bits * Fraction(settings.fps.numerator, settings.fps.denominator))
        else:
            video_planning_bps = max(8_000_000, math.ceil(Fraction(pixels * settings.fps.numerator, 5 * settings.fps.denominator)))
            video = math.ceil(video_planning_bps * duration / 8)
        if sample_count and "video_packet_bytes" in measured:
            video = math.ceil(Fraction(sum(measured["video_packet_bytes"]) * count, sample_count))
            # Fixed headers are counted once; only measured sample/chunk table
            # entries scale with frame count, not the whole short movie header.
            video += measured["video_fixed_bytes"] + math.ceil(
                Fraction(measured["video_table_bytes"] * count, sample_count)
            )
        else:
            unknown.append("视频封装头及索引尚未采样，当前视频值为规格码率估算")
    audio_bytes = embedded_audio = 0
    if project_id and read_timeline(store, project_id)["timeline"]["clips"]:
        samples = audio_sample_window(settings)[0]
        audio_bytes = samples * 8 + 44  # Actual stereo float32 WAV mix.
        if output.video != "none":
            if prores:
                embedded_audio = samples * 6  # Stereo PCM24 inside MOV.
                unknown.append("音频封装索引尚未测量")
            else:
                # FFmpeg's native stereo AAC default target, distinct from the
                # retained float WAV. This remains nominal until actual muxing.
                embedded_audio = math.ceil(128_000 * duration / 8)
                unknown.append("视频内 AAC 音轨按 128 kb/s 默认目标估算，尚未实测")
    video += embedded_audio
    # Float transport is one supersampled frame per producer. SDR retains its
    # screenshot sequence, whose compressed size is unknown without sampling.
    intermediate = pixels * 16 * output.render_scale**2 * min(output.render_workers, count) if floating else pixels * 4 * output.render_scale**2 * count
    auxiliary = measured.get("source_metadata_bytes", 0)
    final = master + video + audio_bytes + auxiliary
    # Float EXR and video now stream together: account for their simultaneous
    # files plus bounded raw transports. Shared CPU/GPU buffers are not disk files.
    render_peak = master + audio_bytes + auxiliary + intermediate
    encode_peak = final + (intermediate if not master_frame else 0)
    peak = final + intermediate if floating else max(render_peak, encode_peak)
    same_volume = volume.stat().st_dev == store.root.stat().st_dev
    workspace_required = len(canonical({
        "settings": settings.model_dump(), "output": output.model_dump(), "measurements": measured,
    }).encode())
    if same_volume:
        peak += workspace_required
    destination_free = shutil.disk_usage(volume).free
    workspace_free = destination_free if same_volume else shutil.disk_usage(store.root).free
    return {
        "basis": "sample-mean-v1" if measurements else "nominal-no-margin-v2",
        "engine": engine, "settings": settings.model_dump(), "output": output.model_dump(),
        "export_directory": str(destination), "checked_at": now(),
        "master_frame_bytes": master_frame, "master_uncompressed_bytes": uncompressed,
        "compressed_master_bytes": master if sample_count and "master_bytes" in measured else None,
        "master_overhead_budget_bytes": 0, "intermediate_budget_bytes": intermediate,
        "video_budget_bytes": video, "video_planning_bps": video_planning_bps,
        "audio_bytes": audio_bytes, "auxiliary_budget_bytes": auxiliary,
        "workspace_required_bytes": workspace_required, "destination_required_bytes": peak,
        "estimated_output_bytes": final, "estimated_peak_bytes": peak,
        "destination_free_bytes": destination_free, "workspace_free_bytes": workspace_free,
        "same_volume": same_volume, "destination_sufficient": destination_free >= peak,
        "workspace_sufficient": workspace_free >= workspace_required,
        "sufficient": destination_free >= peak and workspace_free >= workspace_required,
        "sample_count": sample_count, "unmeasured_components": unknown,
        "notes": [
            "按规格或采样平均值估算，不增加百分比或最大帧余量；不是体积上限。",
            "磁盘峰值只计算实际共存文件；临时帧不代表整片浮点缓存。",
            "未采样 EXR 显示未压缩参考值，采样后使用实际 ZIP 文件平均大小。",
            *unknown,
        ],
    }
