"""One capability matrix drives linked UI choices and strict direct-delivery submission."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .encoding import available_encoders
from .errors import ChromaError
from .models import ColorPipeline, DeliveryConfiguration, DeliveryPreset
from .presets import FRAME_RATES
from .store import canonical

CONTAINERS = {
    "mp4": "MP4",
    "mov": "QuickTime MOV",
    "mxf": "MXF OP1a",
    "mkv": "Matroska MKV",
    "exr": "OpenEXR 序列",
    "png": "PNG 序列",
    "tiff": "TIFF 序列",
    "dpx": "DPX 序列",
}
CODECS = {
    "h264": ("H.264", "libx264", ("mp4", "mov")),
    "hevc": ("H.265 / HEVC", "libx265", ("mp4", "mov")),
    "prores": ("Apple ProRes", "prores_ks", ("mov",)),
    "dnxhr": ("Avid DNxHR", "dnxhd", ("mov", "mxf")),
    "ffv1": ("FFV1 无损", "ffv1", ("mkv",)),
    "rgb": ("无压缩 RGB", "r210", ("mov",)),
    "yuv": ("无压缩 YUV", "v210", ("mov",)),
    "exr": ("OpenEXR", "exr", ("exr",)),
    "png": ("PNG", "png", ("png",)),
    "tiff": ("TIFF", "tiff", ("tiff",)),
    "dpx": ("DPX", "dpx", ("dpx",)),
}
LABELS = {
    "container": "封装 / 图像格式",
    "codec": "视频 / 图像编码",
    "profile": "编码档位",
    "bit_depth": "位深",
    "sampling": "色度采样",
    "alpha_mode": "Alpha",
    "color_output": "输出色彩",
    "data_range": "数据范围",
    "compression": "图像压缩",
    "quality_mode": "质量控制",
    "crf": "CRF",
    "bitrate_kbps": "码率",
    "gop_seconds": "关键帧间隔",
}
PROFILE_LABELS = {
    "high": "High",
    "high10": "High 10",
    "high422": "High 4:2:2",
    "high444": "High 4:4:4",
    "main": "Main",
    "main10": "Main 10",
    "proxy": "Proxy",
    "lt": "LT",
    "422": "422",
    "hq": "HQ",
    "4444": "4444",
    "4444xq": "4444 XQ",
    "lb": "LB",
    "sq": "SQ",
    "hqx": "HQX",
    "444": "444",
}
COLOR_LABELS = {
    "ACEScg": "ACEScg / AP1 线性",
    "ACES2065-1": "ACES2065-1 / AP0 线性",
    "sRGB": "sRGB",
    "Rec.709": "Rec.709 / Rec.1886",
    "PQ": "PQ · 1000 nit",
    "HLG": "HLG · 1000 nit",
}
ALPHA_LABELS = {"none": "无 Alpha", "straight": "直通 Alpha", "premultiplied": "预乘 Alpha"}
MXF_RATES = {"24/1", "25/1", "30/1", "50/1", "60/1", "24000/1001", "30000/1001", "60000/1001"}
IMAGES = {"exr", "png", "tiff", "dpx"}


@dataclass(frozen=True)
class Variant:
    profile: str | None
    depth: int
    sampling: str
    pixel: str = ""
    alpha_pixel: str | None = None
    encoder_profile: str | None = None


VARIANTS = {
    "h264": [
        Variant("high", 8, "420", "yuv420p", encoder_profile="high"),
        Variant("high10", 10, "420", "yuv420p10le", encoder_profile="high10"),
        *[
            Variant(
                "high422",
                b,
                "422",
                "yuv422p" + ("10le" if b == 10 else ""),
                encoder_profile="high422",
            )
            for b in (8, 10)
        ],
        *[
            Variant(
                "high444",
                b,
                "444",
                "yuv444p" + ("10le" if b == 10 else ""),
                encoder_profile="high444",
            )
            for b in (8, 10)
        ],
    ],
    "hevc": [
        Variant("main", 8, "420", "yuv420p", encoder_profile="main"),
        Variant("main10", 10, "420", "yuv420p10le", encoder_profile="main10"),
    ],
    "prores": [
        Variant(
            p,
            10,
            "422" if n < 4 else "444",
            "yuv422p10le" if n < 4 else "yuv444p10le",
            "yuva444p10le" if n >= 4 else None,
            str(n),
        )
        for n, p in enumerate(("proxy", "lt", "422", "hq", "4444", "4444xq"))
    ],
    "dnxhr": [
        Variant(
            p,
            10 if p in {"hqx", "444"} else 8,
            "444" if p == "444" else "422",
            "yuv444p10le" if p == "444" else "yuv422p10le" if p == "hqx" else "yuv422p",
            encoder_profile="dnxhr_" + p,
        )
        for p in ("lb", "sq", "hq", "hqx", "444")
    ],
    "ffv1": [
        Variant(None, 8, "RGB", "bgr0", "bgra"),
        Variant(None, 16, "RGB", "gbrp16le", "gbrap16le"),
    ],
    "rgb": [Variant(None, 10, "RGB", "gbrp10le")],
    "yuv": [Variant(None, 10, "422", "yuv422p10le")],
    "exr": [Variant(None, d, "RGB") for d in (16, 32)],
    "png": [Variant(None, d, "RGB") for d in (8, 16)],
    "tiff": [Variant(None, d, "RGB") for d in (8, 16)],
    "dpx": [Variant(None, d, "RGB") for d in (10, 12, 16)],
}


@lru_cache(maxsize=24)
def _pixel_formats(executable: str, modified: int, encoder: str) -> frozenset[str]:
    # Probe the installed encoder, not just the existence of a similarly named codec.
    result = subprocess.run(
        [executable, "-hide_banner", "-h", "encoder=" + encoder],
        capture_output=True,
        text=True,
        timeout=10,
    )
    match = re.search(r"Supported pixel formats:\s*([^\n]+)", result.stdout + result.stderr)
    return frozenset(match[1].split()) if match else frozenset()


def supported_variants() -> dict[str, list[Variant]]:
    encoders = available_encoders()
    executable = shutil.which("ffmpeg")
    modified = Path(executable).stat().st_mtime_ns if executable else 0
    result = {}
    for codec, (_, encoder, _) in CODECS.items():
        if codec in IMAGES:
            result[codec] = VARIANTS[codec]
        elif encoder in encoders and executable:
            pixels = _pixel_formats(executable, modified, encoder)
            result[codec] = [v for v in VARIANTS[codec] if v.pixel in pixels]
            # Alpha must also be advertised by this native encoder build.
            result[codec] = [
                Variant(
                    v.profile,
                    v.depth,
                    v.sampling,
                    v.pixel,
                    v.alpha_pixel if v.alpha_pixel in pixels else None,
                    v.encoder_profile,
                )
                for v in result[codec]
            ]
    return {k: v for k, v in result.items() if v}


def _alpha_modes(codec: str, variant: Variant) -> list[str]:
    if codec == "png":
        return ["none", "straight"]
    if codec in {"exr", "tiff"} or variant.alpha_pixel:
        return ["none", "straight", "premultiplied"]
    return ["none"]


def _colors(codec: str, depth: int = 32) -> list[str]:
    if codec == "exr":
        return ["ACEScg", "ACES2065-1"]
    if codec in {"png", "tiff"}:
        return ["sRGB"]
    if codec == "dpx":
        return ["Rec.709"]
    return ["Rec.709", "sRGB"] + (["PQ", "HLG"] if depth >= 10 and codec != "h264" else [])


def _pick(value: Any, values: list[Any]) -> Any:
    return value if value in values else values[0]


def _options(values: list[Any], labels: dict[Any, str] | None = None) -> list[dict[str, Any]]:
    return [
        {"value": v, "label": (labels or {}).get(v, str(v) if v is not None else "格式固定")}
        for v in dict.fromkeys(values)
    ]


def delivery_options(
    configuration: DeliveryConfiguration | None = None, changed_field: str | None = None
) -> dict[str, Any]:
    if changed_field is not None and changed_field not in LABELS:
        raise ChromaError("delivery_field", "Unknown delivery field", field=changed_field)
    requested = configuration or DeliveryConfiguration()
    values = requested.model_dump()
    supported = supported_variants()
    containers = [c for c in CONTAINERS if any(c in CODECS[k][2] for k in supported)]
    if not containers:
        raise ChromaError("delivery_unavailable", "没有可用的交付格式，请检查环境。")
    if configuration is not None and values["container"] not in containers:
        raise ChromaError(
            "encoder_unavailable",
            "该封装没有可用编码器，请检查环境。",
            container=values["container"],
        )
    values["container"] = _pick(values["container"], containers)
    codecs = [k for k in supported if values["container"] in CODECS[k][2]]
    values["codec"] = codec = _pick(values["codec"], codecs)
    variants = supported[codec]
    candidates = variants

    def matches(v: Variant, field: str) -> bool:
        if field == "profile":
            return v.profile == values[field]
        if field == "bit_depth":
            return v.depth == values[field]
        if field == "sampling":
            return v.sampling == values[field]
        if field == "alpha_mode":
            return values[field] in _alpha_modes(codec, v)
        if field == "color_output":
            return values[field] in _colors(codec, v.depth)
        return True

    # The field the user changed wins; compatible choices survive, conflicts are explicit.
    priorities = [changed_field, "profile", "bit_depth", "sampling", "alpha_mode", "color_output"]
    for field in dict.fromkeys(f for f in priorities if f is not None):
        matching = [v for v in candidates if matches(v, field)]
        if matching:
            candidates = matching
    # If a depth is unavailable in the new format, choose the nearest storage
    # precision instead of always falling back to the lowest-quality variant.
    preferred = {"prores": "hq", "dnxhr": "hq"}.get(codec)
    variant = min(
        candidates,
        key=lambda v: (
            abs(v.depth - requested.bit_depth),
            preferred is not None and v.profile != preferred,
        ),
    )
    values.update(profile=variant.profile, bit_depth=variant.depth, sampling=variant.sampling)
    alpha_modes = _alpha_modes(codec, variant)
    if values["alpha_mode"] not in alpha_modes:
        # Preserve transparency when only its association changes (EXR/TIFF -> PNG).
        values["alpha_mode"] = (
            "straight"
            if values["alpha_mode"] != "none" and "straight" in alpha_modes
            else alpha_modes[0]
        )
    colors = _colors(codec, variant.depth)
    values["color_output"] = _pick(values["color_output"], colors)
    ranges = (
        ["full"]
        if codec in IMAGES or variant.sampling == "RGB"
        else ["limited"]
        if codec in {"prores", "dnxhr", "yuv"}
        else ["limited", "full"]
    )
    values["data_range"] = _pick(values["data_range"], ranges)
    compression = (
        ["zip", "piz", "none"]
        if codec == "exr"
        else ["zip"]
        if codec in {"png", "tiff"}
        else [None]
    )
    values["compression"] = _pick(values["compression"], compression)
    qualities = ["crf", "bitrate"] if codec in {"h264", "hevc"} else ["fixed"]
    values["quality_mode"] = _pick(values["quality_mode"], qualities)
    values["crf"] = (
        (18 if values["crf"] is None else values["crf"])
        if values["quality_mode"] == "crf"
        else None
    )
    values["bitrate_kbps"] = (
        (20000 if values["bitrate_kbps"] is None else values["bitrate_kbps"])
        if values["quality_mode"] == "bitrate"
        else None
    )
    normalized = DeliveryConfiguration.model_validate(values)
    changes = [
        {"field": f, "label": LABELS.get(f, f), "previous": requested.model_dump()[f], "value": v}
        for f, v in normalized.model_dump().items()
        if requested.model_dump()[f] != v
    ]
    rates = [
        f
        for f in FRAME_RATES
        if values["container"] != "mxf" or f"{f['numerator']}/{f['denominator']}" in MXF_RATES
    ]
    return {
        "configuration": normalized.model_dump(),
        "options": {
            "container": _options(containers, CONTAINERS),
            "codec": _options(codecs, {k: v[0] for k, v in CODECS.items()}),
            "profile": _options([v.profile for v in variants], PROFILE_LABELS),
            "bit_depth": _options(
                [v.depth for v in variants],
                {
                    d: (
                        f"{d}-bit float"
                        if d == 32
                        else "16-bit half"
                        if codec == "exr"
                        else f"{d}-bit"
                    )
                    for d in (8, 10, 12, 16, 32)
                },
            ),
            "sampling": _options(
                [v.sampling for v in variants],
                {"RGB": "RGB", "420": "4:2:0", "422": "4:2:2", "444": "4:4:4"},
            ),
            "alpha_mode": _options(
                list(dict.fromkeys(a for v in variants for a in _alpha_modes(codec, v))),
                ALPHA_LABELS,
            ),
            "color_output": _options(_colors(codec, max(v.depth for v in variants)), COLOR_LABELS),
            "data_range": _options(ranges, {"full": "完整范围", "limited": "视频范围"}),
            "compression": _options(
                compression,
                {None: "不适用", "zip": "ZIP 无损", "piz": "PIZ 无损", "none": "无压缩"},
            ),
            "quality_mode": _options(
                qualities, {"crf": "固定质量 CRF", "bitrate": "目标码率", "fixed": "格式固定"}
            ),
        },
        "adjustments": changes,
        "frame_rates": rates,
        "constraints": {
            "min_width": 256 if codec == "dnxhr" else 16,
            "min_height": 120 if codec == "dnxhr" else 16,
            "width_multiple": 2 if variant.sampling in {"420", "422"} else 1,
            "height_multiple": 2 if variant.sampling == "420" else 1,
        },
        "numeric_fields": {
            "crf": {"min": 0, "max": 51, "step": 1},
            "bitrate_kbps": {"min": 100, "max": 1000000, "step": 100},
            "gop_seconds": {"min": 0.01, "max": 30, "step": 0.1},
        },
        "precision_note": "本机 ProRes / DNxHR 编码输入为 10-bit 或更低，解码存储位深单独报告。"
        if codec in {"prores", "dnxhr"}
        else "",
    }


def configured_output(configuration: DeliveryConfiguration) -> DeliveryPreset:
    result = delivery_options(configuration)
    if result["adjustments"]:
        raise ChromaError(
            "delivery_combination",
            "交付参数组合不兼容，请先更新联动选项。",
            adjustments=result["adjustments"],
            suggested_configuration=result["configuration"],
        )
    c = configuration
    v = next(
        v
        for v in supported_variants()[c.codec]
        if v.profile == c.profile and v.depth == c.bit_depth and v.sampling == c.sampling
    )
    digest = hashlib.sha256(canonical(c.model_dump()).encode()).hexdigest()[:12]
    # IDs identify immutable output files; nothing is saved to the user's preset library.
    return DeliveryPreset(
        id=f"delivery-{c.codec}-{c.profile or c.bit_depth}-{re.sub(r'[^a-z0-9-]', '', c.color_output.lower())}-{digest}",
        name=f"{CONTAINERS[c.container]} · {CODECS[c.codec][0]} {PROFILE_LABELS.get(c.profile or '', '')} · {c.bit_depth}-bit · {c.color_output}",
        family="自主交付",
        container=c.container,
        codec=CODECS[c.codec][1],
        profile=v.encoder_profile,
        pixel_format=v.alpha_pixel if c.alpha_mode != "none" and v.alpha_pixel else v.pixel,
        bit_depth=c.bit_depth,
        alpha=c.alpha_mode != "none",
        alpha_mode="premultiplied" if c.alpha_mode == "premultiplied" else "straight",
        data_range=c.data_range,
        compression=c.compression or "none",
        color=ColorPipeline(output=c.color_output),
        crf=c.crf,
        bitrate_kbps=c.bitrate_kbps,
        gop_seconds=c.gop_seconds,
        builtin=False,
    )
