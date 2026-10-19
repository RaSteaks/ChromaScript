"""HDR color edges verify physical co-siting independently of stream labels."""

import re
import subprocess
from copy import deepcopy

import numpy as np
import pytest

from chromascript.encoding import check_probe, encode
from chromascript.models import RenderSettings
from chromascript.presets import builtins


@pytest.mark.integration
@pytest.mark.parametrize("preset_id", ["hdr-pq", "hdr-hlg"])
def test_hdr_420_uses_top_left_samples_and_native_location(tmp_path, preset_id):
    settings = RenderSettings(width=64, height=64, frames=1)
    preset = builtins()[preset_id].model_copy(update={"crf": 0})
    frame = np.ones((64, 64, 4), np.float32)
    # A horizontal red/blue boundary distinguishes vertical chroma phases;
    # uniform swatches alone could pass with the wrong sampling lattice.
    frame[:32, :, :3] = [0.8, 0, 0]
    frame[32:, :, :3] = [0, 0, 0.8]
    path = tmp_path / "edge.mp4"
    info = encode(preset, settings, path, [frame], lambda _: None)
    assert check_probe(info, preset, settings)["passed"]
    assert info["streams"][0]["chroma_location"] == "topleft"
    headers = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(path), "-map", "0:v:0", "-c", "copy",
         "-bsf:v", "trace_headers", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stderr
    assert re.search(r"chroma_loc_info_present_flag\s+.*= 1", headers)
    assert re.search(r"chroma_sample_loc_type_top_field\s+.*= 2", headers)
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
         "-pix_fmt", "yuv420p10le", "-f", "rawvideo", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    actual = np.frombuffer(decoded, "<u2").astype(int)

    def reference(location):
        # An independent float input and explicitly chosen zimg phase provide
        # expected samples. No project command builder is used for this reference.
        planes = np.ascontiguousarray(frame[..., [1, 2, 0]].transpose(2, 0, 1), dtype="<f4")
        data = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", "gbrpf32le",
             "-video_size", "64x64", "-i", "pipe:0", "-vf",
             "zscale=matrixin=gbr:matrix=2020_ncl:rangein=full:range=limited:"
             f"chromal={location}:dither=error_diffusion,format=yuv420p10le",
             "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
            input=planes.tobytes(), capture_output=True, check=True,
        ).stdout
        return np.frombuffer(data, "<u2").astype(int)

    top_left, left = reference("topleft"), reference("left")
    assert np.max(np.abs(actual - top_left)) <= 3
    assert np.max(np.abs(actual - left)) >= 40
    # Metadata-only or left/unspecified siting must not certify the same pixels.
    wrong = deepcopy(info)
    wrong["streams"][0]["chroma_location"] = "left"
    assert any("chroma location mismatch" in issue for issue in check_probe(wrong, preset, settings)["issues"])
    missing = deepcopy(info)
    missing.pop("video_decode_verification")
    checked = check_probe(missing, preset, settings)
    assert not checked["passed"] and not checked["decode_verified"]
