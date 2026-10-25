"""Bounded, transient HDR fragments from the sandbox's original float pixels."""

from __future__ import annotations

import base64
import binascii
import json
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import Field, model_validator

from .color import render_output
from .encoding import _boxes, available_encoders, command, quicktime_color_metadata
from .errors import ChromaError
from .models import Model, MotionRate, MotionRenderSettings
from .motion_color import MotionSourceSpace, decode_float_frame
from .presets import builtins

_encoder = threading.BoundedSemaphore(1)


MAX_PREVIEW_BYTES = 192 * 1024 * 1024
_hardware_failed: set[tuple[int, int, int, int]] = set()
_stream_failed: set[tuple[int, int, int, int, int]] = set()


@lru_cache(maxsize=1)
def _hardware_available() -> bool:
    # Discovery is only a hint; the first real encode must still succeed.
    return platform.system() == "Darwin" and "hevc_videotoolbox" in available_encoders()


class HDRPreviewSettings(Model):
    # 720p in either orientation, with the project's exact rational rate.
    width: int = Field(ge=16, le=1280, strict=True)
    height: int = Field(ge=16, le=1280, strict=True)
    fps: MotionRate
    source_color_space: MotionSourceSpace
    input_format: Literal["rgba32f-linear", "p010-pq"] = "rgba32f-linear"

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * (3 if self.input_format == "p010-pq" else 16)

    @model_validator(mode="after")
    def dimensions(self) -> HDRPreviewSettings:
        if self.width % 2 or self.height % 2 or min(self.width, self.height) > 720:
            raise ValueError("HDR 预览要求偶数尺寸，最多 1280×720（或竖屏 720×1280）")
        return self


class HDRPreviewHeader(HDRPreviewSettings):
    frame_count: int = Field(ge=1, le=64, strict=True)

    @model_validator(mode="after")
    def byte_budget(self) -> HDRPreviewHeader:
        if self.frame_bytes * self.frame_count > MAX_PREVIEW_BYTES:
            raise ValueError("HDR 预览片段超过 192 MiB 内存预算")
        return self


class HDRPreviewChunk(HDRPreviewSettings):
    # CLI/MCP use the same format and limits; HTTP also offers a binary adapter.
    frames: list[str] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def bounded_payload(self) -> HDRPreviewChunk:
        if self.frame_bytes * len(self.frames) > MAX_PREVIEW_BYTES:
            raise ValueError("HDR 预览片段超过 192 MiB 内存预算")
        expected = 4 * ((self.frame_bytes + 2) // 3)
        if any(len(frame) != expected for frame in self.frames):
            raise ValueError("HDR 预览帧长度与尺寸不符")
        return self


def hevc_codec(path: Path) -> str:
    """Build RFC 6381's codec string from actual hvcC, never a preset name."""
    with path.open("rb") as stream:
        def visit(start: int, end: int) -> bytes | None:
            for kind, payload, stop in _boxes(stream, start, end):
                if kind in {b"moov", b"trak", b"mdia", b"minf", b"stbl"}:
                    if result := visit(payload, stop):
                        return result
                elif kind == b"stsd":
                    for entry, sample, sample_end in _boxes(stream, payload + 8, stop):
                        if entry != b"hvc1":
                            continue
                        for child, data, child_end in _boxes(stream, sample + 78, sample_end):
                            if child == b"hvcC" and child_end - data >= 13:
                                stream.seek(data)
                                return stream.read(13)
            return None

        record = visit(0, path.stat().st_size)
    if record is None or record[0] != 1:
        raise ChromaError("hdr_preview_codec", "HDR 预览缺少有效的 HEVC 配置。")
    profile = ("", "A", "B", "C")[record[1] >> 6] + str(record[1] & 31)
    compatibility = int(f"{int.from_bytes(record[2:6], 'big'):032b}"[::-1], 2)
    constraints = record[6:12].rstrip(b"\0")
    suffix = "".join(f".{value:02X}" for value in constraints)
    return f"hvc1.{profile}.{compatibility:X}.{'H' if record[1] & 32 else 'L'}{record[12]}{suffix}"


def encode_hdr_preview(request: HDRPreviewChunk) -> dict[str, Any]:
    """Base64 transport for shared operations; processing matches binary HTTP."""
    try:
        payload = b"".join(base64.b64decode(frame, validate=True) for frame in request.frames)
    except (ValueError, binascii.Error) as exc:
        raise ChromaError("hdr_preview_frame", "HDR 预览帧不是有效的 Base64。") from exc
    header = HDRPreviewHeader.model_validate({
        **request.model_dump(exclude={"frames"}), "frame_count": len(request.frames),
    })
    return encode_hdr_preview_binary(header, payload)


def encode_hdr_preview_binary(request: HDRPreviewHeader, payload: bytes | bytearray) -> dict[str, Any]:
    """The HTTP adapter cannot bypass the same precision, dimensions or byte limits."""
    if len(payload) != request.frame_bytes * request.frame_count:
        raise ChromaError("hdr_preview_frame", "HDR 预览二进制长度与帧数不符。")
    queued = time.perf_counter()
    if not _encoder.acquire(timeout=2):
        raise ChromaError("hdr_preview_busy", "HDR 预览编码器正在使用中，请稍后重试预览。")
    acquired = time.perf_counter()
    try:
        result = _encode(request, payload)
        result["timings_ms"]["queue"] = (acquired - queued) * 1000
        result["timings_ms"]["total"] = (time.perf_counter() - queued) * 1000
        return result
    finally:
        _encoder.release()


def _encode(request: HDRPreviewHeader, payload: bytes | bytearray) -> dict[str, Any]:
    probe_executable = shutil.which("ffprobe")
    if not probe_executable:
        raise ChromaError("hdr_preview_unavailable", "HDR 预览需要本机 FFmpeg 和 ffprobe。")
    pixels = payload if request.input_format == "p010-pq" else bytearray()
    hardware_key = (request.width, request.height, request.fps.numerator, request.fps.denominator)
    if request.input_format == "p010-pq":
        # P010 has six zero low bits. Reject a mislabelled 8-bit/float transport;
        # these are display-rendered values, so NEVER apply another OCIO transform.
        if np.any(np.frombuffer(payload, dtype="<u2") & 63):
            raise ChromaError("hdr_preview_frame", "P010 像素低 6 位必须为零。")
    else:
        converted = bytearray()
        for offset in range(0, len(payload), request.frame_bytes):
            linear = decode_float_frame(
                bytes(payload[offset:offset + request.frame_bytes]), request.width, request.height,
                source=request.source_color_space,
            )
            # Legacy float clients retain the same one-transform black composite.
            display = render_output(linear, "PQ", keep_alpha=False)
            if not np.isfinite(linear).all() or not np.isfinite(display).all():
                raise ChromaError("hdr_preview_frame", "HDR 预览颜色转换产生非有限值，请检查源码亮度。")
            converted.extend(np.rint(np.clip(display, 0, 1) * 65535).astype("<u2").tobytes())
        pixels = converted
    preset = builtins()["hdr-pq"]
    settings = MotionRenderSettings(
        width=request.width, height=request.height, frames=request.frame_count, fps=request.fps,
    )
    with tempfile.TemporaryDirectory(prefix="chromascript-hdr-stream-") as temporary:
        path = Path(temporary) / "preview.mp4"
        args = command(preset, settings, path)
        args[args.index("-preset") + 1] = "ultrafast"
        args[args.index("-g") + 1] = str(request.frame_count)
        args[args.index("-x265-params") + 1] = (
            "log-level=error:pools=4:frame-threads=2:bframes=0:scenecut=0"
        )
        args[args.index("-movflags") + 1] = "+empty_moov+default_base_moof+frag_keyframe+write_colr"
        # MSE receives one video track only; delivery timecode tracks do not
        # belong in an ephemeral media-source initialization segment.
        if "-timecode" in args:
            offset = args.index("-timecode")
            del args[offset:offset + 2]
        software_args = args.copy()
        encoder = "libx265"
        if request.input_format == "p010-pq":
            args[args.index("-pixel_format") + 1] = "p010le"
            # Matrix/range/subsampling already happened on the GPU. This format
            # repack changes storage layout only, never pixel values or transfer.
            color_fields = ",setparams=range=limited:color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc"
            args[args.index("-vf") + 1] = "format=yuv420p10le" + color_fields
            args[-1:-1] = [
                "-enc_time_base", f"{request.fps.denominator}:{request.fps.numerator}",
                # VT's isolated first packet can lack duration. Express seconds
                # in the filter's actual TB, without changing that timebase:
                # setts output-timebase rescaling differs across FFmpeg versions.
                # Multiplying by the rational denominator twice stretches 59.94
                # fps frames by 1001 and makes MSE report nonexistent coverage.
                "-bsf:v", "hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9:video_full_range_flag=0:chroma_sample_loc_type=2:"
                f"tick_rate={request.fps.ffmpeg}:num_ticks_poc_diff_one=1,"
                f"setts=pts=N*{request.fps.denominator}/{request.fps.numerator}/TB:"
                f"dts=N*{request.fps.denominator}/{request.fps.numerator}/TB:"
                f"duration={request.fps.denominator}/{request.fps.numerator}/TB",
            ]
            software_args = args.copy()
            if hardware_key not in _hardware_failed and _hardware_available():
                encoder = "hevc_videotoolbox"
                args[args.index("-c:v") + 1] = encoder
                args[args.index("-pix_fmt") + 1] = "p010le"
                args[args.index("-vf") + 1] = "format=p010le" + color_fields
                for option in ("-preset", "-x265-params", "-crf", "-profile:v"):
                    if option in args:
                        index = args.index(option)
                        del args[index:index + 2]
                args[-1:-1] = ["-profile:v", "main10", "-b:v", "16000k", "-allow_sw", "0",
                                "-realtime", "1", "-prio_speed", "1", "-bf", "0"]
        persistent = False
        encode_began = time.perf_counter()
        try:
            # Single-frame seeks reuse the measured low-delay native stream too;
            # each independent fragment still receives full decode verification.
            stream_key = (*hardware_key, request.frame_count)
            if encoder == "hevc_videotoolbox" and stream_key not in _stream_failed:
                from .hdr_encoder import encode_fragment

                try:
                    path.write_bytes(encode_fragment(args, request.frame_count, request.frame_bytes, pixels))
                    result = subprocess.CompletedProcess(args, 0, b"", b"")
                    persistent = True
                except ChromaError:
                    _stream_failed.add(stream_key)
                    result = subprocess.run(args, input=pixels, capture_output=True, timeout=20)
            else:
                result = subprocess.run(args, input=pixels, capture_output=True, timeout=20)
            if result.returncode and encoder == "hevc_videotoolbox":
                # Probe by actual encoding. An advertised but unavailable hardware
                # encoder falls back without changing resolution or project fps.
                _hardware_failed.add(hardware_key)
                encoder = "libx265"
                result = subprocess.run(software_args, input=pixels, capture_output=True, timeout=20)
            if result.returncode:
                raise ChromaError("hdr_preview_encode", result.stderr[-2000:].decode(errors="replace"))
            if request.input_format == "p010-pq" and not any(
                entry.get("primaries") == 9 and entry.get("transfer") == 16
                and entry.get("matrix") == 9 for entry in quicktime_color_metadata(path)
            ):
                # VT overwrites codec-context color fields after encoding. Read
                # the corrected native SPS in a copy-only remux so MP4 colr agrees;
                # pixels and timing are unchanged, and both layers are reverified.
                remuxed = path.with_name("tagged.mp4")
                remux = subprocess.run([
                    args[0], "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy",
                    "-movflags", "+empty_moov+default_base_moof+frag_keyframe+write_colr",
                    str(remuxed),
                ], capture_output=True, timeout=10)
                if remux.returncode:
                    raise ChromaError("hdr_preview_encode", "HDR 预览容器写入失败")
                path = remuxed
            verify_began = time.perf_counter()
            # count_frames performs a full decode in this same probe process.
            # Explode-on-error, stderr, actual frame count, packet timing and
            # pixel/tag checks retain strict verification without a second decode.
            decoded = subprocess.run(
                [probe_executable, "-v", "error", "-err_detect", "explode",
                 "-threads", "4", "-count_frames", "-show_streams", "-show_packets",
                 "-show_entries", "stream:packet=pts,dts,duration",
                 "-of", "json", str(path)], capture_output=True, text=True, timeout=10,
            )
            if decoded.returncode or decoded.stderr:
                raise ChromaError("hdr_preview_decode", decoded.stderr[-2000:])
            probed = json.loads(decoded.stdout)
            streams = probed["streams"]
            video: dict[str, Any] = next((item for item in streams if item.get("codec_type") == "video"), {})
            # A codec's nominal r_frame_rate is not proof of presentation time.
            # Check every bounded packet on the actual MP4 clock, including a
            # one-frame poster and the nonzero origin of a reused encoder.
            packets = probed.get("packets", [])
            step = Fraction(request.fps.denominator, request.fps.numerator)
            time_base = Fraction(video.get("time_base", "1/1"))
            first_pts = packets[0].get("pts") if packets else None
            if len(packets) != request.frame_count or first_pts is None or any(
                packet.get("pts") is None or packet.get("dts") != packet.get("pts")
                or (packet["pts"] - first_pts) * time_base != index * step
                or packet.get("duration", 0) * time_base != step
                for index, packet in enumerate(packets)
            ):
                raise ChromaError("hdr_preview_verification", "HDR 预览实际帧时间戳或时长不符。")
            if video.get("nb_read_frames") != str(request.frame_count):
                raise ChromaError("hdr_preview_decode", "HDR 预览严格解码帧数不符。")
            expected = {
                "codec_name": "hevc", "codec_tag_string": "hvc1", "pix_fmt": "yuv420p10le",
                "color_primaries": "bt2020", "color_transfer": "smpte2084",
                "color_space": "bt2020nc", "color_range": "tv", "chroma_location": "topleft",
                "width": request.width, "height": request.height,
                "nb_read_frames": str(request.frame_count),
                "r_frame_rate": request.fps.ffmpeg,
            }
            native = quicktime_color_metadata(path)
            if any(video.get(key) != value for key, value in expected.items()) or not any(
                item.get("primaries") == 9 and item.get("transfer") == 16
                and item.get("matrix") == 9 and item.get("full_range") is False for item in native
            ):
                raise ChromaError("hdr_preview_verification", "HDR 预览实际解码属性或容器色彩标签不符。",
                                  decoded={key: video.get(key) for key in expected}, container=native)
        except subprocess.TimeoutExpired as exc:
            raise ChromaError("hdr_preview_timeout", "HDR 预览编码或验证超时，已回退 SDR。") from exc
        return {
            "data": base64.b64encode(path.read_bytes()).decode(),
            "mime_type": f'video/mp4; codecs="{hevc_codec(path)}"',
            "width": request.width, "height": request.height,
            "frames": request.frame_count, "fps": request.fps.model_dump(),
            "transient": True, "output": "PQ", "target_peak_nits": 1000, "encoder": encoder,
            "input_format": request.input_format,
            # Native reusable streams have an absolute decode clock. MSE offsets
            # each independently decoded chunk using its actual first timestamp.
            "timestamp_base": float(int(video.get("start_pts", 0)) * Fraction(video["time_base"])),
            "persistent_encoder": persistent,
            "timings_ms": {"encode_mux": (verify_began - encode_began) * 1000,
                           "verify": (time.perf_counter() - verify_began) * 1000},
            "verification": {"decoded": expected, "container_color": native, "strict_decode": True,
                             "packet_timing": True},
        }
