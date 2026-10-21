"""Encode display-rendered or explicitly declared scene-log RGB without another view."""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import tempfile
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator

import numpy as np
from numpy.typing import NDArray

from .color import output_metadata, tags, video_tag_issue
from .errors import ChromaError
from .models import OUTPUT_ENCODINGS, DeliveryPreset, RenderSettings


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


def _video_tags(preset: DeliveryPreset) -> dict[str, str]:
    result = tags(preset.color.output)
    if preset.container == "mov" and OUTPUT_ENCODINGS[preset.color.output][1] in {
        "gamma2.4",
        "gamma2.6",
    }:
        # NCLC has no pure gamma-2.4/2.6 code. colr supersedes gama, so keep
        # unspecified TRC and an explicit declaration requiring manual import.
        result["transfer"] = "unknown"
    return result


def _encoding_declaration(preset: DeliveryPreset) -> dict[str, Any] | None:
    """Native comments record intent, not a standardized automatic color transform."""
    space, gamma = OUTPUT_ENCODINGS[preset.color.output]
    if preset.color.output == "ACEScct" or (
        preset.container == "mov" and gamma in {"gamma2.4", "gamma2.6"}
    ):
        declaration: dict[str, Any] = {
            "chromascript_color_encoding": preset.color.output,
            "primaries": "AP1" if gamma == "acescct" else space,
            "transfer": "ACEScct" if gamma == "acescct" else gamma,
            "data_range": preset.data_range,
        }
        if preset.color_metadata_policy == "declared-scene":
            # Native comments carry a complete explicit interpretation, but do
            # not pretend that a receiving application automatically applies it.
            declaration.update(
                white_point="D60", reference="scene", display_transform_applied=False,
                working_space="ACEScg", matrix="BT.709", transport="p416le",
                image_precision_bits=12, requires_manual_interpretation=True,
                resolve_input="ACEScct", resolve_data_levels="Video",
            )
        return declaration
    return None


def _hdr_chroma_location(preset: DeliveryPreset) -> str | None:
    # BT.2100's 4:2:0 lattice is co-sited at the top-left luma sample.
    # The resampler and the encoder must describe the same physical positions.
    return "topleft" if (
        OUTPUT_ENCODINGS[preset.color.output][1] in {"st2084", "hlg"}
        and "420" in preset.pixel_format
    ) else None


def _hlg_float_input(preset: DeliveryPreset) -> bool:
    # Narrow YCbCr has headroom above nominal white. Full-range integer RGB/YUV
    # cannot promise that headroom; those outputs reject unrepresentable HLG.
    return preset.color.output == "HLG" and preset.data_range == "limited" and not (
        preset.pixel_format.startswith(("gbr", "rgb", "bgr"))
    )


def _validate_hlg_storage(frame: NDArray[np.float32], preset: DeliveryPreset) -> None:
    """Reject unrepresentable HLG instead of silently changing its color volume."""
    rgb = frame[..., :3]
    if not _hlg_float_input(preset):
        # Allow only floating-point roundoff around black/white for integer Full.
        if np.min(rgb) < -1e-6 or np.max(rgb) > 1 + 1e-6:
            raise ChromaError(
                "hlg_storage_range",
                "HLG 超白超出 Full/RGB 整数输出范围；请选择 Limited YCbCr 输出以保留高光。",
            )
        return
    # Test the actual BT.2020 NCL components, not RGB against nominal 0..1.
    # BT.2100 narrow 10-bit permits codes 4..1019 (scaled for other depths),
    # including the overshoots needed by saturated 1000-nit HLG colors.
    scale = 2 ** (preset.bit_depth - 8)
    maximum = (1 << preset.bit_depth) - scale - 1
    luma = rgb[..., 0] * 0.2627 + rgb[..., 1] * 0.6780 + rgb[..., 2] * 0.0593
    for component in ("Y", "Cb", "Cr"):
        signal = luma if component == "Y" else (
            (rgb[..., 2] - luma) / 1.8814 if component == "Cb"
            else (rgb[..., 0] - luma) / 1.4746
        )
        codes = np.rint((signal * (219 if component == "Y" else 224)
                         + (16 if component == "Y" else 128)) * scale)
        if np.min(codes) < scale or np.max(codes) > maximum:
            raise ChromaError(
                "hlg_storage_range", "HLG 信号超出所选位深的窄范围超白存储范围，请调整画面。",
                component=component, minimum=int(np.min(codes)), maximum=int(np.max(codes)),
            )


def _boxes(stream: BinaryIO, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Walk bounded box headers, skipping media payloads even for large exports."""
    while start + 8 <= end:
        stream.seek(start)
        size, kind = struct.unpack(">I4s", stream.read(8))
        header = 8
        if size == 1:
            if start + 16 > end:
                return
            size = struct.unpack(">Q", stream.read(8))[0]
            header = 16
        if size == 0:
            size = end - start
        if size < header or start + size > end:
            return
        yield kind, start + header, start + size
        start += size


def quicktime_color_metadata(path: Path) -> list[dict[str, Any]]:
    """Read actual visual sample-entry colr/gama boxes, independently of codec VUI."""
    entries: list[dict[str, Any]] = []
    with path.open("rb") as stream:

        def visit(start: int, end: int, depth: int = 0) -> None:
            if depth > 8:
                return
            for kind, payload, box_end in _boxes(stream, start, end):
                if kind in {b"moov", b"trak", b"mdia", b"minf", b"stbl"}:
                    visit(payload, box_end, depth + 1)
                elif kind == b"stsd":
                    # Full-box flags and entry count precede the sample entries;
                    # each visual entry has a fixed 78-byte header before children.
                    for _, sample, sample_end in _boxes(stream, payload + 8, box_end):
                        record: dict[str, Any] = {}
                        for child, data, child_end in _boxes(stream, sample + 78, sample_end):
                            stream.seek(data)
                            if child == b"colr" and child_end - data >= 10:
                                color_type = stream.read(4)
                                if color_type in {b"nclc", b"nclx"}:
                                    primaries, transfer, matrix = struct.unpack(
                                        ">HHH", stream.read(6)
                                    )
                                    record.update(
                                        type=color_type.decode(),
                                        primaries=primaries,
                                        transfer=transfer,
                                        matrix=matrix,
                                    )
                                    if color_type == b"nclx" and child_end - data >= 11:
                                        record["full_range"] = bool(stream.read(1)[0] & 128)
                            elif child == b"gama" and child_end - data == 4:
                                record["gamma"] = struct.unpack(">I", stream.read(4))[0] / 65536
                        if record:
                            entries.append(record)

        visit(0, path.stat().st_size)
    return entries


def preset_tag_issue(preset: DeliveryPreset) -> str | None:
    """A narrow, model-validated scene declaration never relaxes other recipes."""
    if preset.color_metadata_policy == "declared-scene":
        return None
    return video_tag_issue(preset.color.output, preset.container)


def command(preset: DeliveryPreset, settings: RenderSettings, destination: Path) -> list[str]:
    # Comments/sidecars cannot replace native gamut and transfer tags. This gate
    # also covers legacy preset calls that bypass the configured-delivery UI.
    if issue := preset_tag_issue(preset):
        raise ChromaError("video_color_tags", issue)
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ChromaError("ffmpeg_unavailable", "Install FFmpeg and add it to PATH")
    color_tags = _video_tags(preset)
    # H.273 transfer 4 is "bt470m" in filters/probe output and "gamma22" in
    # encoder options. Keep the verified tag canonical and adapt only this CLI spelling.
    encoder_transfer = "gamma22" if color_tags["transfer"] == "bt470m" else color_tags["transfer"]
    pixel = preset.pixel_format
    rgb = pixel.startswith(("gbr", "rgb", "bgr"))
    alpha = preset.alpha
    chroma_location = _hdr_chroma_location(preset)
    floating = _hlg_float_input(preset)
    if rgb:
        video_filter = f"format={pixel}"
    else:
        source_format = "gbrap16le" if alpha else "gbrp16le"
        conversion = (
            f"zscale=matrixin=gbr:matrix={color_tags['zscale_matrix']}:"
            f"rangein=full:range={preset.data_range}:dither=error_diffusion"
            + (f":chromal={chroma_location}" if chroma_location else "")
        )
        if floating and alpha:
            # Dropping float Alpha via format/swscale clips RGB super-whites.
            # Rewire RGB planes without conversion, process coverage separately,
            # then merge individual planes (alphamerge would reduce to 8 bits).
            color_pixel = pixel.replace("yuva", "yuv", 1)
            alpha_depth = pixel_format_info(pixel)["components"][3]["bit_depth"]
            gray = "gray" if alpha_depth == 8 else f"gray{alpha_depth}le"
            video_filter = (
                "split[rgbin][ain];[rgbin]"
                "mergeplanes=map0p=0:map1p=1:map2p=2:format=gbrpf32le,"
                f"{conversion},format={color_pixel},extractplanes=y+u+v[y][u][v];"
                f"[ain]alphaextract,format={gray}[a];[y][u][v][a]"
                f"mergeplanes=map0s=0:map1s=1:map2s=2:map3s=3:format={pixel}"
            )
        else:
            # Keep HLG floating point until matrix/range conversion and final
            # codec quantization. An integer RGB intermediate would clip >1.
            video_filter = (
                ("" if floating else f"format={source_format},")
                + f"{conversion},format={pixel}"
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
        ("gbrapf32le" if alpha else "gbrpf32le") if floating else "rgba64le",
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
    if preset.codec == "prores_videotoolbox":
        # Apple software encoding is allowed; never substitute the 10-bit KS path.
        args += ["-allow_sw", "1"]
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
        encoder_transfer,
        "-colorspace",
        "rgb" if rgb else color_tags["matrix"],
        "-color_range",
        "pc" if preset.data_range == "full" else "tv",
    ]
    if chroma_location:
        args += ["-chroma_sample_location", chroma_location]
    if preset.container in {"mov", "mp4"}:
        # QuickTime forbids relying on gama alongside colr: compliant readers
        # ignore gama in that combination. Never emit it as a gamma certificate.
        args += ["-movflags", "+faststart+write_colr"]
        # QuickTime tmcd stores nominal fps in one byte. Keep arbitrary source
        # rates exact instead of letting an optional timecode track break muxing.
        if 1 <= settings.fps.value and math.ceil(settings.fps.value) <= 255:
            args += ["-timecode", settings.timecode]
    if preset.container == "mxf":
        args += ["-timecode", settings.timecode, "-f", "mxf"]
    declaration = _encoding_declaration(preset)
    if declaration is not None:
        args += ["-metadata", "comment=" + json.dumps(declaration, separators=(",", ":"))]
    args += ["-threads", "4", str(destination)]
    return args


def encode(
    preset: DeliveryPreset,
    settings: RenderSettings,
    path: Path,
    frames: Iterable[NDArray[np.float32]],
    progress: Callable[[int], None],
    audio_path: Path | None = None,
    on_process: Callable[[subprocess.Popen[Any]], None] | None = None,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.stem + ".partial" + path.suffix)
    log_path = path.with_suffix(path.suffix + ".ffmpeg.log")
    args = command(preset, settings, partial)
    if audio_path is not None:
        # Audio is a separate PCM input; video/color filters never see its samples.
        # Match the pre-rendered exact-length WAV, without -shortest dropping video frames.
        index = args.index("-an")
        codec = "aac" if preset.container == "mp4" else "pcm_s24le"
        args[index:index + 1] = ["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0",
                                "-c:a", codec, "-ar", "48000", "-ac", "2"]
        if codec == "aac":
            args[index:index] = ["-thread_queue_size", "512"]
        # Match audio properties against the actual muxed stream after encoding.
    count = 0
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log
        )
        if on_process is not None:
            on_process(process)
        try:
            assert process.stdin is not None
            for frame in frames:
                if (
                    frame.shape != (settings.height, settings.width, 4)
                    or not np.isfinite(frame).all()
                    or np.any((frame[..., 3] < 0) | (frame[..., 3] > 1))
                ):
                    raise ChromaError("encoder_input", "Invalid frame delivered to encoder")
                if preset.color.output == "ACEScct" and (
                    np.min(frame[..., :3]) < 0 or np.max(frame[..., :3]) > 1
                ):
                    raise ChromaError(
                        "log_storage_range",
                        "ACEScct code values exceed integer storage; choose float EXR to preserve them.",
                        frame=count,
                        minimum=float(frame[..., :3].min()),
                        maximum=float(frame[..., :3].max()),
                        out_of_range_components=int(((frame[..., :3] < 0) | (frame[..., :3] > 1)).sum()),
                    )
                if preset.color.output == "HLG":
                    _validate_hlg_storage(frame, preset)
                if _hlg_float_input(preset):
                    # FFmpeg planar GBR(A) is G, B, R, then coverage. Preserve
                    # HLG overshoots; Alpha never enters the color/range transform.
                    channels = [1, 2, 0, 3] if preset.alpha else [1, 2, 0]
                    pixels = np.ascontiguousarray(frame[..., channels].transpose(2, 0, 1), dtype="<f4")
                else:
                    # SDR/PQ retain the established 16-bit display RGB transport.
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
    info = probe(path, on_process=on_process) if on_process is not None else probe(path)
    if audio_path is not None:
        audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
        duration = float(audio.get("duration", info["format"].get("duration", 0))) if audio else 0
        # AAC may include two packets of padding. PCM MOV/MKV must decode to
        # exactly the frozen sample count; a low video fps never relaxes that check.
        expected_samples = round(Fraction(settings.frames * settings.fps.denominator,
                                          settings.fps.numerator) * 48000)
        padding = (2048 if codec == "aac" else
                   math.ceil(48000 / settings.fps.value) if preset.container == "mxf" else 0)
        tolerance = max(padding / 48000, 0.001)  # Container time fields may use milliseconds.
        if (audio is None or audio.get("codec_name") != codec
                or int(audio.get("sample_rate", 0)) != 48000 or audio.get("channels") != 2
                or abs(duration - settings.duration) > tolerance):
            raise ChromaError("audio_verification", "Muxed audio properties failed verification", stream=audio)
        decoded = decode_audio_evidence(path)
        if abs(decoded["decoded_samples"] - expected_samples) > padding:
            raise ChromaError("audio_verification", "Decoded audio duration failed verification", evidence=decoded)
        info["audio_verification"] = {"passed": True, "stream": audio, **decoded,
                                      "expected_samples": expected_samples,
                                      "padding_tolerance_samples": padding,
                                      "expected_duration_seconds": settings.duration,
                                      "duration_tolerance_seconds": tolerance}
    return info


def decode_audio_evidence(path: Path) -> dict[str, Any]:
    """Decode the actual muxed audio with bounded memory, not just container labels."""
    count, peak = 0, 0
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-err_detect", "explode",
            "-i", str(path), "-map", "0:a:0", "-c:a", "pcm_s32le", "-f", "s32le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=errors)
        try:
            assert process.stdout is not None
            with process.stdout:
                while data := process.stdout.read(65536):
                    count += len(data)
                    peak = max(peak, int(np.max(np.abs(np.frombuffer(data, dtype="<i4").astype(np.int64)))))
            result = process.wait()
            if result or count == 0 or count % 8:
                errors.seek(0)
                raise ChromaError("audio_verification", "Audio stream did not fully decode", diagnostic=errors.read(2000).decode(errors="replace"))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return {"decoded_samples": count // 8, "decoded_peak": peak / 2**31}


def _run_probe(args: list[str], on_process: Callable[[subprocess.Popen[Any]], None] | None = None,
               **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Sampling cancellation also owns probe/verification children, not only encoding."""
    if on_process is None:
        return subprocess.run(args, **kwargs)
    timeout = kwargs.pop("timeout", None)
    process = subprocess.Popen(args, **kwargs)
    on_process(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def strict_video_decode_evidence(
    path: Path, on_process: Callable[[subprocess.Popen[Any]], None] | None = None,
) -> dict[str, Any]:
    """Fully decode the first video stream without accepting concealed bitstream errors."""
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ChromaError("ffmpeg_unavailable", "ffmpeg is required for strict video verification")
    # A damaged stream can retain its advertised properties and frame count while
    # FFmpeg conceals missing references. Abort on decoder errors and discard frame
    # payloads; spill diagnostics to disk so malformed input cannot grow RAM usage.
    # Full decoding has no duration-independent timeout: valid long deliveries may
    # take longer than a metadata probe, especially for high-resolution intra codecs.
    with tempfile.TemporaryFile() as errors:
        result = _run_probe(
            [
                executable, "-v", "error", "-nostdin", "-xerror",
                "-abort_on", "empty_output", "-err_detect", "explode",
                "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
                "-fps_mode", "passthrough", "-f", "null", "-",
            ],
            on_process=on_process,
            stdout=subprocess.DEVNULL,
            stderr=errors,
        )
        error_bytes = errors.tell()
        errors.seek(max(0, error_bytes - 4000))
        diagnostic = errors.read(4000).decode(errors="replace")
        if result.returncode or error_bytes:
            raise ChromaError(
                "video_decode_verification",
                "Video stream did not fully decode without errors",
                diagnostic=diagnostic,
                return_code=result.returncode,
            )
    return {"passed": True, "complete": True, "method": "ffmpeg-strict-full-decode", "stream": "0:v:0"}


def probe(path: Path, count_frames: bool = True,
          on_process: Callable[[subprocess.Popen[Any]], None] | None = None) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise ChromaError("ffprobe_unavailable", "ffprobe is required for delivery verification")
    args = [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json"]
    if count_frames:
        args += ["-count_frames"]
    # FFprobe may emit decoder errors yet return zero after error concealment.
    # Preserve the exact counted-frame metadata, but fail closed on its error log.
    # Counting decodes the whole file, so only the metadata-only path has a fixed
    # timeout. Diagnostics use a temporary file and a bounded excerpt in errors.
    with tempfile.TemporaryFile() as errors:
        result = _run_probe(
            [*args, str(path)], stdout=subprocess.PIPE, stderr=errors,
            text=True, timeout=None if count_frames else 180,
            on_process=on_process,
        )
        error_bytes = errors.tell()
        errors.seek(max(0, error_bytes - 4000))
        diagnostic = errors.read(4000).decode(errors="replace")
        if result.returncode or error_bytes:
            raise ChromaError("probe_failed", diagnostic or "ffprobe failed", return_code=result.returncode)
    info = json.loads(result.stdout)
    # Audio callers share this metadata helper. Strict video evidence is only
    # added for an actual video stream and never for count_frames=False inspection.
    if count_frames and any(stream.get("codec_type") == "video" for stream in info["streams"]):
        info["video_decode_verification"] = (
            strict_video_decode_evidence(path, on_process=on_process)
            if on_process is not None else strict_video_decode_evidence(path)
        )
    if path.suffix.lower() in {".mov", ".mp4"}:
        info["container_color"] = quicktime_color_metadata(path)
    return info


def check_probe(
    info: dict[str, Any], preset: DeliveryPreset, settings: RenderSettings
) -> dict[str, Any]:
    from fractions import Fraction

    video = next((stream for stream in info["streams"] if stream["codec_type"] == "video"), None)
    if not video:
        raise ChromaError("delivery_verification", "Output has no video stream")
    issues = []
    # Stream headers and a counted frame total cannot prove error-free decode.
    # Metadata-only inspection must never certify a delivery as fully verified.
    decode = info.get("video_decode_verification", {})
    decode_verified = (
        decode.get("passed") is True and decode.get("complete") is True
        and decode.get("method") == "ffmpeg-strict-full-decode"
        and decode.get("stream") == "0:v:0"
    )
    if not decode_verified:
        issues.append("missing or incomplete strict video decode verification")
    if location := _hdr_chroma_location(preset):
        if video.get("chroma_location") != location:
            issues.append(f"chroma location mismatch: expected {location}, got {video.get('chroma_location')}")
    if issue := preset_tag_issue(preset):
        issues.append(issue)
    expected_codec = {"libx264": "h264", "libx265": "hevc", "prores_ks": "prores", "prores_videotoolbox": "prores"}.get(
        preset.codec, preset.codec
    )
    if video.get("codec_name") != expected_codec:
        issues.append("codec mismatch")
    # QuickTime/MP4 timecode may live on a separate tmcd stream, MXF on the container.
    timecodes = [stream.get("tags", {}).get("timecode") for stream in info["streams"]]
    timecodes.append(info.get("format", {}).get("tags", {}).get("timecode"))
    embedded_timecode = preset.container == "mxf" or (
        preset.container in {"mov", "mp4"} and 1 <= settings.fps.value and math.ceil(settings.fps.value) <= 255
    )
    if embedded_timecode and settings.timecode not in timecodes:
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
    expected = _video_tags(preset)
    for key, value in [
        ("color_primaries", expected["primaries"]),
        ("color_transfer", expected["transfer"]),
    ]:
        # Some legacy uncompressed MOV encoders cannot carry all color tags. The file
        # remains experimental instead of being falsely certified by encoder presence.
        if video.get(key, "unknown") != value:
            issues.append(f"{key}: expected {value}, got {video.get(key)}")
    expected_matrix = (
        "gbr" if preset.pixel_format.startswith(("gbr", "rgb", "bgr")) else expected["matrix"]
    )
    if video.get("color_space") != expected_matrix:
        issues.append(f"matrix mismatch: {video.get('color_space')}")
    if preset.container in {"mov", "mp4"}:
        # A valid codec VUI alone does not prove Resolve can read container color
        # metadata. Verify physical colr without claiming automatic Gamma interpretation.
        expected_colr = {
            "primaries": {"bt2020": 9, "smpte432": 12, "unknown": 2}.get(expected["primaries"], 1),
            "transfer": {
                "bt709": 1,
                "unknown": 2,
                "bt470m": 4,
                "iec61966-2-1": 13,
                "smpte2084": 16,
                "arib-std-b67": 18,
            }[expected["transfer"]],
            "matrix": 0 if expected_matrix == "gbr" else 9 if expected_matrix == "bt2020nc" else 1,
        }
        container_color = info.get("container_color", [])
        if len(container_color) != 1 or any(
            container_color[0].get(key) != value for key, value in expected_colr.items()
        ):
            issues.append("QuickTime/MP4 colr mismatch or missing")
        elif container_color[0].get("type") == "nclx" and container_color[0].get("full_range") != (
            preset.data_range == "full"
        ):
            issues.append("MP4 colr range mismatch")
        if any("gamma" in entry for entry in container_color):
            issues.append("Conflicting QuickTime colr/gama metadata")
    expected_declaration = _encoding_declaration(preset)
    if expected_declaration is not None:
        try:
            declaration = json.loads(info.get("format", {}).get("tags", {}).get("comment", ""))
        except (ValueError, TypeError):
            declaration = {}
        if declaration != expected_declaration:
            issues.append(f"missing or incorrect native {preset.color.output} declaration")
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
    storage = video.get("pix_fmt", "")
    actual_depth = 0
    try:
        actual_pixel = pixel_format_info(storage)
        expected_pixel = pixel_format_info(preset.pixel_format)
    except ChromaError:
        issues.append("missing or unknown decoded pixel format")
    else:
        actual_depth = min(c["bit_depth"] for c in actual_pixel["components"][:3])
        # Compare semantics, not format strings: ProRes 4444 legitimately decodes
        # wider than its input and may expose an extra opaque alpha channel.
        if actual_pixel["flags"]["rgb"] != expected_pixel["flags"]["rgb"]:
            issues.append("RGB/YCbCr component type mismatch")
        if any(
            actual_pixel.get(key, 0) != expected_pixel.get(key, 0)
            for key in ("log2_chroma_w", "log2_chroma_h")
        ):
            issues.append("chroma sampling mismatch")
        if actual_pixel["nb_components"] < (4 if preset.alpha else 3):
            issues.append("missing color/alpha components")
        if preset.alpha and not actual_pixel["flags"]["alpha"]:
            issues.append("missing alpha channel")
    if actual_depth < preset.bit_depth:
        issues.append(
            f"precision mismatch: decoded {actual_depth}-bit < requested {preset.bit_depth}-bit"
        )
    if preset.profile is not None:
        expected_profile = PROFILE_NAMES.get(preset.codec, {}).get(preset.profile)
        # Legacy ProRes presets may explicitly select the encoder's auto (-1)
        # profile. Verify a known decoded profile without inventing a fixed request.
        automatic = preset.codec == "prores_ks" and preset.profile in {"auto", "-1"}
        profile_matches = (
            video.get("profile") in PROFILE_NAMES["prores_ks"].values()
            if automatic
            else expected_profile is not None and video.get("profile") == expected_profile
        )
        if not profile_matches:
            issues.append(
                f"profile mismatch: requested {preset.profile}, got {video.get('profile')}"
            )
    return {
        "passed": not issues,
        "issues": issues,
        "decode_verified": decode_verified,
        "video_decode_verification": decode,
        "video": video,
        "frame_rate_source": rate_source,
        "verified_frame_rate": str(actual_rate) if actual_rate is not None else None,
        "render_precision": "float32",
        "encoder_input_precision": "32-bit float RGB" if _hlg_float_input(preset) else "16-bit RGB",
        "hlg_extended_range": _hlg_float_input(preset),
        "codec_input_precision": "16-bit YCbCr / P416" if preset.codec == "prores_videotoolbox" else f"{preset.bit_depth}-bit",
        "decoded_storage_precision": f"{actual_depth}-bit",
        "range_signaling": "codec convention" if implicit_range else "explicit",
        "effective_range": expected_range,
        "timecode_signaling": "embedded" if embedded_timecode else "sidecar only",
        "container": preset.container,
        "color_encoding": output_metadata(preset.color.output),
        "container_color": info.get("container_color"),
        "native_color_tags_verified": not issues and preset.color_metadata_policy == "standard",
        "color_declaration_verified": not issues and preset.color_metadata_policy == "declared-scene",
        "color_signaling": "native ACEScct declaration; AP1/transfer flags unspecified, manual interpretation required"
        if preset.color.output == "ACEScct"
        else "colr/codec tags and native declaration; manual gamma interpretation required"
        if expected_declaration is not None
        else "QuickTime/MP4 colr and codec tags"
        if preset.container in {"mov", "mp4"}
        else "container and codec tags",
    }


# Encoder option names and ffprobe profile names are different vocabularies.
# Unknown requested profiles fail closed rather than certifying from the codec alone.
PROFILE_NAMES = {
    "prores_videotoolbox": {"4": "4444"},
    "prores_ks": {
        alias: decoded
        for i, (name, decoded) in enumerate(
            zip(
                ("proxy", "lt", "standard", "hq", "4444", "4444xq"),
                ("Proxy", "LT", "Standard", "HQ", "4444", "XQ"),
                strict=True,
            )
        )
        for alias in (str(i), name)
    },
    "dnxhd": {
        alias: "DNXHR " + name.upper()
        for i, name in enumerate(("lb", "sq", "hq", "hqx", "444"), start=1)
        for alias in (str(i), "dnxhr_" + name)
    },
    "libx264": {
        "baseline": "Constrained Baseline",
        "main": "Main",
        "high": "High",
        "high10": "High 10",
        "high422": "High 4:2:2",
        "high444": "High 4:4:4 Predictive",
    },
    "libx265": {"main": "Main", "main10": "Main 10"},
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
        # Fail closed for unchecked historical presets too; absent native color
        # support must never be mistaken for a successfully verified EXR encoding.
        if expected is None:
            issues.append("Unsupported EXR color encoding")
        elif np.shape(actual_chroma) != (8,) or not np.allclose(actual_chroma, expected, atol=1e-7):
            issues.append("EXR chromaticities mismatch")
        interop_id = {"ACEScg": "lin_ap1_scene", "ACES2065-1": "lin_ap0_scene"}.get(
            preset.color.output
        )
        if preset.color.output == "ACEScct":
            interop_id = "ocio:acescct_ap1_scene"
            if metadata.get("chromascript:gamma") != "acescct":
                issues.append("EXR ACEScct transfer mismatch")
        if interop_id and metadata.get("colorInteropID") != interop_id:
            issues.append("EXR colorInteropID mismatch")
        if preset.id == "aces2065-archive" and metadata.get("acesImageContainerFlag") != 1:
            issues.append("missing ACES image container flag")
        if preset.color.output in {"ACEScg", "ACEScct"} and metadata.get("acesImageContainerFlag"):
            issues.append("AP1 data must not carry the AP0 ACES image container flag")
    elif preset.container == "png":
        # Require physical PNG color chunks, not the reader's assumed sRGB space.
        if metadata.get("png:sRGB") not in {0, 1, 2, 3}:
            issues.append("missing PNG sRGB chunk")
        if not np.isclose(metadata.get("png:Gamma", 0), 0.45455, atol=1e-5):
            issues.append("PNG gamma mismatch")
        chroma = metadata.get("png:Chromaticities", [])
        if np.shape(chroma) != (8,) or not np.allclose(
            chroma, (0.3127, 0.329, 0.64, 0.33, 0.30, 0.60, 0.15, 0.06), atol=1e-5
        ):
            issues.append("PNG chromaticities mismatch")
    elif preset.container == "tiff":
        if not metadata.get("ICCProfile", {}).get("matches_srgb"):
            issues.append("missing or mismatched TIFF sRGB ICC profile")
    elif preset.container == "dpx":
        if metadata.get("dpx:Colorimetric") != "ITU-R 709-4":
            issues.append("DPX colorimetric mismatch")
        if metadata.get("dpx:Transfer") != "User defined" or not np.isclose(
            metadata.get("dpx:Gamma", 0), 2.4, atol=1e-6
        ):
            issues.append("DPX gamma mismatch")
    if preset.alpha and preset.container in {"png", "tiff"}:
        associated = not metadata.get("oiio:UnassociatedAlpha", 0)
        if associated != (preset.alpha_mode == "premultiplied"):
            issues.append("image alpha association mismatch")
    return issues
