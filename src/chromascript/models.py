"""Versioned, transport-independent contracts; frame rates never use decimal timebases."""

from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1


class Rational(Model):
    numerator: int = Field(default=60, ge=1, le=240_000)
    denominator: int = Field(default=1, ge=1, le=100_000)

    @model_validator(mode="after")
    def canonical(self) -> Rational:
        rate = Fraction(self.numerator, self.denominator)
        if not 1 <= rate <= 240:
            raise ValueError("Frame rate must be between 1 and 240 fps")
        self.numerator, self.denominator = rate.numerator, rate.denominator
        return self

    @property
    def value(self) -> float:
        return self.numerator / self.denominator

    @property
    def ffmpeg(self) -> str:
        return f"{self.numerator}/{self.denominator}"


class Parameter(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    label: str = ""
    kind: Literal["number", "color", "boolean"] = "number"
    default: Any = 0.0
    minimum: float | None = None
    maximum: float | None = None
    color_space: Literal["ACEScg", "sRGB"] = "ACEScg"

    @model_validator(mode="after")
    def valid(self) -> Parameter:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum exceeds maximum")
        self.check(self.default)
        return self

    def check(self, value: Any) -> None:
        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{self.name}: expected boolean")
        elif self.kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{self.name}: expected number")
            if not math.isfinite(value) or abs(value) > 3.4028234663852886e38:
                raise ValueError(f"{self.name}: non-finite value")
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.name}: below minimum")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.name}: above maximum")
        else:
            if not isinstance(value, list) or len(value) != 3:
                raise ValueError(f"{self.name}: expected RGB triplet")
            if any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or abs(v) > 3.4028234663852886e38
                for v in value
            ):
                raise ValueError(f"{self.name}: invalid RGB triplet")


class SceneSpec(Model):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    code: str = Field(min_length=1, max_length=200_000)
    parameters: list[Parameter] = Field(default_factory=list, max_length=24)
    values: dict[str, Any] = Field(default_factory=dict)
    seed: int = Field(default=42, ge=0, le=4_294_967_295)

    @model_validator(mode="after")
    def validate_values(self) -> SceneSpec:
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate parameter names")
        reserved = {
            "fn",
            "var",
            "let",
            "const",
            "struct",
            "return",
            "if",
            "else",
            "true",
            "false",
            "override",
        }
        if any(name in reserved for name in names):
            raise ValueError("Parameter name is reserved by WGSL")
        if set(self.values) - set(names):
            raise ValueError("Unknown scene parameters")
        for parameter in self.parameters:
            parameter.check(self.values.get(parameter.name, parameter.default))
        # Only the wrapper owns bindings and entry points. Custom code remains shader-only.
        if re.search(r"@(group|binding|compute|vertex|fragment)\b", self.code):
            raise ValueError("Scene code cannot define bindings or shader entry points")
        if not re.search(r"fn\s+shade\s*\(", self.code):
            raise ValueError("Scene must define fn shade(ctx: SceneContext) -> vec4<f32>")
        return self


class RenderSettings(Model):
    width: int = Field(default=3840, ge=16, le=8192)
    height: int = Field(default=2160, ge=16, le=8192)
    fps: Rational = Field(default_factory=Rational)
    frames: int = Field(default=300, ge=1, le=216_000)
    start_frame: int = Field(default=0, ge=0, le=10_000_000)
    samples: Literal[1, 4, 16] = 4
    tile_size: Literal[128, 256, 512] = 512
    # HFR QuickTime timecode needs a third frame digit above frame 99.
    timecode: str = Field(default="00:00:00:00", pattern=r"^\d{2}:\d{2}:\d{2}[:;]\d{2,3}$")

    @model_validator(mode="after")
    def valid_timecode(self) -> RenderSettings:
        h, m, s, f = (int(part) for part in re.split(r"[:;]", self.timecode))
        if h > 23 or m > 59 or s > 59 or f >= math.ceil(self.fps.value):
            raise ValueError("Timecode is out of range")
        if ";" in self.timecode:
            if (self.fps.numerator, self.fps.denominator) not in {(30000, 1001), (60000, 1001)}:
                raise ValueError("Drop-frame timecode requires 30000/1001 or 60000/1001")
            dropped = 2 if self.fps.numerator == 30000 else 4
            if s == 0 and m % 10 != 0 and f < dropped:
                raise ValueError("Timecode names a dropped frame")
        return self

    @property
    def duration(self) -> float:
        return self.frames * self.fps.denominator / self.fps.numerator


# Display encodings and scene-referred ACES encodings remain distinct. ACEScct
# shares AP1 with ACEScg, but its log curve must never receive a display tone map.
OutputColorSpace = Literal["P3-D65", "Rec.709", "Rec.2020", "ACEScct", "ACEScg", "ACES2065-1"]
OutputGamma = Literal[
    "linear", "acescct", "gamma2.2", "gamma2.4", "gamma2.6", "srgb", "st2084", "hlg"
]
OutputTransform = Literal[
    "ACEScg",
    "ACES2065-1",
    "ACEScct",
    "sRGB",
    "Rec.709",
    "Display-P3",
    "P3-PQ",
    "PQ",
    "HLG",
    "Rec.709-Gamma2.2",
    "Rec.709-Gamma2.6",
    "P3-Gamma2.2",
    "P3-Gamma2.4",
    "P3-Gamma2.6",
]
OUTPUT_ENCODINGS: dict[str, tuple[OutputColorSpace, OutputGamma]] = {
    "ACEScg": ("ACEScg", "linear"),
    "ACES2065-1": ("ACES2065-1", "linear"),
    "ACEScct": ("ACEScct", "acescct"),
    "Rec.709": ("Rec.709", "gamma2.4"),
    "sRGB": ("Rec.709", "srgb"),
    "Display-P3": ("P3-D65", "srgb"),
    "P3-PQ": ("P3-D65", "st2084"),
    "PQ": ("Rec.2020", "st2084"),
    "HLG": ("Rec.2020", "hlg"),
    # P3-D65 identifies primaries, not Display P3's sRGB transfer function.
    # Preserve the legacy aliases while allowing Resolve-style independent gamma choices.
    "Rec.709-Gamma2.2": ("Rec.709", "gamma2.2"),
    "Rec.709-Gamma2.6": ("Rec.709", "gamma2.6"),
    "P3-Gamma2.2": ("P3-D65", "gamma2.2"),
    "P3-Gamma2.4": ("P3-D65", "gamma2.4"),
    "P3-Gamma2.6": ("P3-D65", "gamma2.6"),
}
# Only these scene encodings have implemented native EXR primaries/interoperability IDs.
# Legacy presets and the direct-delivery matrix must share the same format boundary.
EXR_OUTPUTS = ("ACEScg", "ACES2065-1", "ACEScct")


def normalize_delivery_field(field: str, value: Any) -> Any:
    """Normalize human aliases once for HTTP, MCP and explicit CLI comparisons."""
    if not isinstance(value, str):
        return value
    key = re.sub(r"[\s._-]", "", value).lower()
    if field == "color_space":
        return {
            "p3": "P3-D65",
            "p3d65": "P3-D65",
            "displayp3": "P3-D65",
            "709": "Rec.709",
            "bt709": "Rec.709",
            "rec709": "Rec.709",
            "2020": "Rec.2020",
            "bt2020": "Rec.2020",
            "rec2020": "Rec.2020",
            "acescct": "ACEScct",
            "acescg": "ACEScg",
            "aces20651": "ACES2065-1",
        }.get(key, value)
    if field == "data_range":
        return {
            "auto": "auto",
            "full": "full",
            "video": "limited",
            "limit": "limited",
            "limited": "limited",
        }.get(key, value)
    if field == "gamma":
        return {
            "gamma22": "gamma2.2",
            "22": "gamma2.2",
            "gamma24": "gamma2.4",
            "24": "gamma2.4",
            "gamma26": "gamma2.6",
            "26": "gamma2.6",
            "srgb": "srgb",
            "linear": "linear",
            "acescct": "acescct",
            "pq": "st2084",
            "st2084": "st2084",
            "hlg": "hlg",
        }.get(key, value)
    return value


class ColorPipeline(Model):
    working_space: Literal["ACEScg"] = "ACEScg"
    config: Literal["cg-config-v4.0.0_aces-v2.0_ocio-v2.5"] = "cg-config-v4.0.0_aces-v2.0_ocio-v2.5"
    output: OutputTransform = "Rec.709"
    peak_nits: Literal[1000] = 1000


class DeliveryPreset(Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    name: str
    family: str
    container: Literal["mp4", "mov", "mxf", "mkv", "exr", "png", "tiff", "dpx"]
    codec: str
    pixel_format: str = ""
    profile: str | None = None
    bit_depth: Literal[8, 10, 12, 16, 32] = 10
    data_range: Literal["auto", "full", "limited"] = "auto"
    alpha: bool = False
    alpha_mode: Literal["straight", "premultiplied"] = "straight"
    compression: Literal["zip", "piz", "none"] = "zip"
    color: ColorPipeline = Field(default_factory=ColorPipeline)
    crf: int | None = Field(default=None, ge=0, le=51)
    bitrate_kbps: int | None = Field(default=None, ge=100, le=1_000_000)
    gop_seconds: float = Field(default=2.0, gt=0, le=30)
    settings: RenderSettings | None = None
    builtin: bool = True

    @model_validator(mode="after")
    def valid_combinations(self) -> DeliveryPreset:
        sequence = self.container in {"exr", "png", "tiff", "dpx"}
        rgb = self.pixel_format.startswith(("gbr", "rgb", "bgr"))
        natural_range: Literal["full", "limited"] = "full" if sequence or rgb else "limited"
        if self.data_range == "auto":
            self.data_range = natural_range
        if (sequence or rgb) and self.data_range != "full":
            raise ValueError("RGB/image outputs require full data range")
        if (
            self.codec in {"prores_ks", "dnxhd", "v210"}
            and not rgb
            and self.data_range != "limited"
        ):
            raise ValueError("This intra codec uses the legal YCbCr range convention")
        if self.color.output in {"ACEScg", "ACES2065-1"} and self.container != "exr":
            raise ValueError("Unbaked scene-linear masters require floating-point EXR")
        if self.container == "exr" and (self.codec != "exr" or self.bit_depth not in (16, 32)):
            raise ValueError("EXR requires half or float")
        if self.container == "exr" and self.color.output not in EXR_OUTPUTS:
            raise ValueError("EXR delivery requires ACEScg, ACES2065-1 or ACEScct encoding")
        # Integer sequence families have explicit storage depths; reject requests
        # which a native writer would silently convert to a different precision.
        legal_integer_depths = {"png": {8, 16}, "tiff": {8, 16}, "dpx": {10, 12, 16}}
        if (
            self.container in legal_integer_depths
            and self.bit_depth not in legal_integer_depths[self.container]
        ):
            raise ValueError(f"Illegal {self.container} bit depth: {self.bit_depth}")
        # Match the sequence capability matrix: unsupported encodings must never
        # fall through to a writer's default sRGB/undefined metadata.
        if self.container in {"png", "tiff"} and self.color.output != "sRGB":
            raise ValueError("PNG/TIFF delivery requires sRGB encoding")
        if self.container == "dpx" and self.color.output != "Rec.709":
            raise ValueError("DPX delivery requires Rec.709 / Gamma 2.4")
        if self.color.output == "ACEScct":
            if self.bit_depth < 10:
                raise ValueError("ACEScct delivery requires at least 10-bit precision")
            if self.container not in {"exr", "mov", "mp4", "mkv"}:
                raise ValueError("ACEScct delivery needs EXR or a declared video encoding")
        if self.color.output in {"PQ", "HLG", "P3-PQ"} and self.bit_depth < 10:
            raise ValueError("HDR output requires at least 10-bit precision")
        # These pure-power curves have no exact H.273 code. Only MOV's numeric
        # gama atom currently preserves their transfer unambiguously in this encoder.
        if (
            self.color.output in {"P3-Gamma2.4", "P3-Gamma2.6", "Rec.709-Gamma2.6"}
            and self.container != "mov"
        ):
            raise ValueError("This gamma encoding requires QuickTime MOV numeric gamma metadata")
        if self.container == "png" and self.alpha_mode != "straight":
            raise ValueError("PNG stores straight alpha; choose straight association")
        if self.alpha and self.container not in {"exr", "png", "tiff", "mov", "mkv"}:
            raise ValueError("This container does not support the requested alpha export")
        if not sequence and not self.pixel_format:
            raise ValueError("Video export needs an explicit pixel format")
        if self.crf is not None and self.codec not in {"libx264", "libx265"}:
            raise ValueError("CRF is only available for x264/x265 presets")
        if self.crf is not None and self.bitrate_kbps is not None:
            raise ValueError("Choose CRF or bitrate, not both")
        if (
            self.settings
            and not sequence
            and ("420" in self.pixel_format or "422" in self.pixel_format)
        ):
            if self.settings.width % 2 or ("420" in self.pixel_format and self.settings.height % 2):
                raise ValueError("Subsampled video dimensions must be even")
        return self


class DeliveryConfiguration(Model):
    """User-selected fields; the shared capability resolver handles linked choices."""

    container: Literal["mp4", "mov", "mxf", "mkv", "exr", "png", "tiff", "dpx"] = "mp4"
    codec: Literal[
        "h264", "hevc", "prores", "dnxhr", "ffv1", "rgb", "yuv", "exr", "png", "tiff", "dpx"
    ] = "h264"
    profile: str | None = Field(default="high", max_length=32)
    bit_depth: Literal[8, 10, 12, 16, 32] = 8
    sampling: Literal["RGB", "420", "422", "444"] = "420"
    alpha_mode: Literal["none", "straight", "premultiplied"] = "none"
    color_space: OutputColorSpace = Field(
        default="Rec.709",
        description="Output RGB primaries/gamut; linked with gamma by get_delivery_options.",
    )
    gamma: OutputGamma = Field(
        default="gamma2.4",
        description="Output transfer encoding; linear, acescct (AP1 log), gamma2.2, gamma2.4, gamma2.6, srgb, st2084 (PQ), or hlg. Gamut and transfer are independent; container support is validated.",
    )
    # Keep the combined output alias for existing scripts and frozen presets.
    color_output: OutputTransform = Field(
        default="Rec.709",
        description="Legacy combined alias; new callers select color_space and gamma. The resolved alias must agree with the pair.",
    )
    # Auto remains a draft choice; DeliveryPreset freezes the concrete codec range.
    data_range: Literal["auto", "full", "limited"] = "auto"
    compression: Literal["zip", "piz", "none"] | None = None
    quality_mode: Literal["crf", "bitrate", "fixed"] = "crf"
    crf: int | None = Field(default=18, ge=0, le=51)
    bitrate_kbps: int | None = Field(default=None, ge=100, le=1_000_000)
    gop_seconds: float = Field(default=2.0, gt=0, le=30)

    @model_validator(mode="before")
    @classmethod
    def expand_color_encoding(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        fields = {field: normalize_delivery_field(field, item) for field, item in value.items()}
        legacy = fields.get("color_output", "Rec.709")
        if not isinstance(legacy, str) or legacy not in OUTPUT_ENCODINGS:
            return fields  # Let the strict literal field report an unknown legacy value.
        if "color_space" not in fields and "gamma" not in fields:
            fields["color_space"], fields["gamma"] = OUTPUT_ENCODINGS[legacy]
        elif "color_output" not in fields:
            # New clients can specify a pair without knowing the internal OCIO alias.
            matches = [
                output
                for output, (space, gamma) in OUTPUT_ENCODINGS.items()
                if space == fields.get("color_space", space) and gamma == fields.get("gamma", gamma)
            ]
            if matches:
                fields["color_output"] = matches[0]
                space, gamma = OUTPUT_ENCODINGS[matches[0]]
                fields.setdefault("color_space", space)
                fields.setdefault("gamma", gamma)
        return fields


class Project(Model):
    id: str
    name: str
    description: str = ""
    archived: bool = False
    created_at: str
    updated_at: str


class Job(Model):
    id: str
    project_id: str
    scene_id: str
    revision_id: str
    kind: Literal["frame", "preview", "sequence", "delivery", "probe"]
    status: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
    stage: str = "queued"
    progress: float = 0
    settings: RenderSettings
    outputs: list[DeliveryPreset] = Field(default_factory=list)
    frame_indices: list[int] | None = None
    completed_frames: int = 0
    created_at: str
    updated_at: str
    snapshot_hash: str
    idempotency_key: str
    pid: int | None = None
    process_started: float | None = None
    error: dict[str, Any] | None = None
    elapsed_seconds: float = 0
    peak_rss_bytes: int = 0
    artifacts: list[str] = Field(default_factory=list)


class Artifact(Model):
    id: str
    project_id: str
    job_id: str
    name: str
    kind: str
    relative_path: str
    mime_type: str
    size_bytes: int
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeliveryRequest(Model):
    """One independently timed/sized item in an idempotent batch."""

    settings: RenderSettings
    preset_ids: list[str] = Field(min_length=1, max_length=40)
