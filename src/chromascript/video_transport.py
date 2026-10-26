"""Typed, display-encoded GPU video packets; never reinterpret these as scene RGB."""

from dataclasses import dataclass
from typing import Literal

import numpy as np

from .errors import ChromaError

VideoPixelFormat = Literal["nv12", "p010le", "p210le", "p416le"]


def video_frame_bytes(width: int, height: int, pixel_format: VideoPixelFormat) -> int:
    return width * height * {"nv12": 3, "p010le": 6, "p210le": 8, "p416le": 12}[pixel_format] // 2


@dataclass(frozen=True)
class VideoFrame:
    data: bytes | memoryview
    pixel_format: VideoPixelFormat

    def validate(self, width: int, height: int, output: str) -> None:
        size = self.data.nbytes if isinstance(self.data, memoryview) else len(self.data)
        if size != video_frame_bytes(width, height, self.pixel_format):
            raise ChromaError("encoder_input", "视频像素长度与编码格式不符。")
        if self.pixel_format in {"p010le", "p210le"}:
            words = np.frombuffer(self.data, "<u2")
            if np.any(words & 63):
                raise ChromaError("encoder_input", "10-bit 视频像素低 6 位必须为零。")
            if output == "HLG" and (words.min() < 4 * 64 or words.max() > 1019 * 64):
                raise ChromaError("hlg_storage_range", "HLG 视频信号超出窄范围超白存储范围。")
