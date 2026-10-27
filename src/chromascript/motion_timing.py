"""Output sampling never changes the source composition's rational timebase."""

from fractions import Fraction
from math import ceil
from typing import Literal

from pydantic import Field, field_validator

from .models import Model, MotionRate, MotionRenderSettings


class MotionTiming(Model):
    fps: MotionRate | None = Field(default=None, description="Output fps only; null inherits the unchanged source timebase.")
    temporal_samples: Literal[1, 4, 8, 16] = Field(default=1, description="1 holds the preceding original frame; 4/8/16 integrate fractional source frames over a centered shutter exposure. Source must be deterministic and seekable.")
    shutter_angle: int = Field(default=180, ge=1, le=360, strict=True)

    @field_validator("temporal_samples", mode="before")
    @classmethod
    def integer_samples(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("temporal_samples must be the integer 1, 4, 8 or 16")
        return value


class MotionTimePlan(Model):
    source_frames: int
    source_fps: MotionRate
    frames: int
    fps: MotionRate
    temporal_samples: int
    shutter_angle: int
    # Versioned sampling semantics are frozen in jobs and verified on recovery.
    policy: Literal["hold-centered-v1"] = "hold-centered-v1"

    def settings(self, source: MotionRenderSettings) -> MotionRenderSettings:
        return MotionRenderSettings.model_validate({
            **source.model_dump(), "frames": self.frames, "fps": self.fps.model_dump(),
        })


def motion_time_plan(source: MotionRenderSettings, output: MotionTiming) -> MotionTimePlan:
    fps = output.fps or source.fps
    # CFR cannot represent every source duration exactly. Cover the full source
    # interval, with less than one output-frame of tail hold; never stretch time.
    count = ceil(Fraction(
        source.frames * source.fps.denominator * fps.numerator,
        source.fps.numerator * fps.denominator,
    ))
    settings = MotionRenderSettings.model_validate({
        **source.model_dump(), "frames": count, "fps": fps.model_dump(),
    })
    return MotionTimePlan(
        source_frames=source.frames, source_fps=source.fps,
        frames=settings.frames, fps=fps,
        temporal_samples=output.temporal_samples, shutter_angle=output.shutter_angle,
    )
