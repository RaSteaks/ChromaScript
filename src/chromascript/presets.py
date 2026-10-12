"""Delivery families are explicit specifications, never inferred from filename extensions."""

from __future__ import annotations

from .models import ColorPipeline, DeliveryPreset, Rational, RenderSettings


def builtins() -> dict[str, DeliveryPreset]:
    result: dict[str, DeliveryPreset] = {}

    def add(id: str, name: str, family: str, container: str, codec: str, **kwargs: object) -> None:
        result[id] = DeliveryPreset.model_validate(
            {
                "id": id,
                "name": name,
                "family": family,
                "container": container,
                "codec": codec,
                **kwargs,
            }
        )

    for depth in (16, 32):
        for compression in ("zip", "piz"):
            add(
                f"exr-{depth}-{compression}",
                f"ACEScg EXR {'Half' if depth == 16 else 'Float'} / {compression.upper()}",
                "浮点母版",
                "exr",
                "exr",
                bit_depth=depth,
                alpha=True,
                compression=compression,
                color=ColorPipeline(output="ACEScg"),
            )
    add(
        "aces2065-archive",
        "ACES2065-1 EXR Half / Uncompressed",
        "浮点母版",
        "exr",
        "exr",
        bit_depth=16,
        compression="none",
        alpha=True,
        color=ColorPipeline(output="ACES2065-1"),
    )
    add(
        "h264-review",
        "H.264 · SDR 审片",
        "通用播放",
        "mp4",
        "libx264",
        pixel_format="yuv420p",
        bit_depth=8,
        crf=18,
    )
    add(
        "h264-mov",
        "H.264 · QuickTime",
        "通用播放",
        "mov",
        "libx264",
        pixel_format="yuv420p",
        bit_depth=8,
        crf=18,
    )
    add(
        "hevc-sdr",
        "HEVC Main10 · SDR",
        "通用播放",
        "mp4",
        "libx265",
        pixel_format="yuv420p10le",
        bit_depth=10,
        crf=16,
    )
    add(
        "hevc-mov",
        "HEVC Main10 · QuickTime",
        "通用播放",
        "mov",
        "libx265",
        pixel_format="yuv420p10le",
        bit_depth=10,
        crf=16,
    )
    for output in ("PQ", "HLG"):
        add(
            f"hdr-{output.lower()}",
            f"HEVC Main10 · {output} 1000 nit",
            "HDR 播放",
            "mp4",
            "libx265",
            pixel_format="yuv420p10le",
            bit_depth=10,
            crf=16,
            color=ColorPipeline(output=output),
        )
    for profile, name in enumerate(("Proxy", "LT", "422", "HQ", "4444", "4444 XQ")):
        is444 = profile >= 4
        add(
            f"prores-{profile}",
            f"ProRes {name}",
            "Apple 专业素材",
            "mov",
            "prores_ks",
            profile=str(profile),
            pixel_format="yuva444p10le" if is444 else "yuv422p10le",
            bit_depth=10,
            alpha=is444,
        )
    for dnx_profile in ("lb", "sq", "hq", "hqx", "444"):
        depth = 10 if dnx_profile in ("hqx", "444") else 8
        pix = "yuv444p10le" if dnx_profile == "444" else "yuv422p10le" if depth == 10 else "yuv422p"
        for container in ("mov", "mxf"):
            add(
                f"dnxhr-{dnx_profile}-{container}",
                f"DNxHR {dnx_profile.upper()} / {container.upper()}",
                "跨平台专业素材",
                container,
                "dnxhd",
                profile=f"dnxhr_{dnx_profile}",
                pixel_format=pix,
                bit_depth=depth,
            )
    for ext in ("png", "tiff"):
        for depth in (8, 16):
            add(
                f"{ext}-{depth}",
                f"{ext.upper()} {depth}-bit",
                "整数图像序列",
                ext,
                ext,
                bit_depth=depth,
                alpha=True,
                color=ColorPipeline(output="sRGB"),
            )
    for depth in (10, 12, 16):
        add(f"dpx-{depth}", f"DPX {depth}-bit", "整数图像序列", "dpx", "dpx", bit_depth=depth)
    add(
        "ffv1-rgb16",
        "FFV1 RGB 16-bit / MKV",
        "无损与无压缩",
        "mkv",
        "ffv1",
        pixel_format="gbrp16le",
        bit_depth=16,
    )
    add(
        "uncompressed-rgb10",
        "RGB 10-bit 无压缩 / MOV",
        "无损与无压缩",
        "mov",
        "r210",
        pixel_format="gbrp10le",
        bit_depth=10,
    )
    add(
        "uncompressed-yuv10",
        "YUV 4:2:2 10-bit 无压缩 / MOV",
        "无损与无压缩",
        "mov",
        "v210",
        pixel_format="yuv422p10le",
        bit_depth=10,
    )
    for id, name, w, h in (
        ("landscape", "横屏", 1920, 1080),
        ("portrait", "竖屏", 1080, 1920),
        ("square", "方形", 1080, 1080),
    ):
        add(
            f"platform-{id}",
            f"{name} · H.264",
            "平台构图",
            "mp4",
            "libx264",
            pixel_format="yuv420p",
            bit_depth=8,
            crf=18,
            settings=RenderSettings(width=w, height=h, frames=300),
        )
    return result


RESOLUTIONS = {
    "HD": [1920, 1080],
    "DCI 2K": [2048, 1080],
    "UHD 4K": [3840, 2160],
    "DCI 4K": [4096, 2160],
    "UHD 8K": [7680, 4320],
    "竖屏": [1080, 1920],
    "方形": [1080, 1080],
}
FRAME_RATES = [
    Rational(numerator=n, denominator=d).model_dump()
    for n, d in [
        (24000, 1001),
        (24, 1),
        (25, 1),
        (30000, 1001),
        (30, 1),
        (48, 1),
        (50, 1),
        (60000, 1001),
        (60, 1),
        (100, 1),
        (120000, 1001),
        (120, 1),
    ]
]
