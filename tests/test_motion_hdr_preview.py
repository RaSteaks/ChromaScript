"""Real local encode/decode evidence; never launches a production Chrome renderer."""

import base64
import json
import subprocess
from fractions import Fraction

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from chromascript.api import create_app
from chromascript.color import render_output
from chromascript.errors import ChromaError
from chromascript.motion_color import decode_float_frame
from chromascript.motion_hdr_preview import (
    HDRPreviewChunk,
    HDRPreviewHeader,
    encode_hdr_preview,
    encode_hdr_preview_binary,
)
from chromascript.operations import REGISTRY, execute
from chromascript.service import Service


def chunk():
    pixels = np.ones((32, 32, 4), dtype="<f4")
    # Neutral highlights exercise the ACES output curve without chroma-subsample
    # ambiguity. Associated RGB = radiance * coverage, read bottom-up by WebGL.
    pixels[:16, :, :3] = 8.0
    pixels[16:, :, :3] = 0.25
    pixels[..., 3] = 0.5
    return {
        "width": 32, "height": 32,
        "fps": {"numerator": 24000, "denominator": 1001},
        "source_color_space": "Linear Rec.2020",
        "frames": [base64.b64encode(pixels.tobytes()).decode()] * 3,
    }


@pytest.mark.parametrize("change", [
    {"width": 321}, {"height": 31}, {"width": True},
    {"fps": {"numerator": 0, "denominator": 1}},
    {"source_color_space": "PQ"}, {"frames": []},
    {"frames": ["AAAA"]}, {"unexpected": True},
])
def test_invalid_chunks_rejected(change):
    with pytest.raises(ValidationError):
        HDRPreviewChunk.model_validate({**chunk(), **change})


def test_nonfinite_and_bad_base64_rejected():
    source = chunk()
    source["frames"] = ["!" * len(source["frames"][0])]
    with pytest.raises(ChromaError, match="Base64"):
        encode_hdr_preview(HDRPreviewChunk.model_validate(source))
    pixels = np.ones((32, 32, 4), dtype="<f4")
    pixels[0, 0, 0] = np.nan
    source["frames"] = [base64.b64encode(pixels.tobytes()).decode()]
    with pytest.raises(ChromaError, match="非有限"):
        encode_hdr_preview(HDRPreviewChunk.model_validate(source))


def test_actual_hdr_fragment_color_and_transient_contract(tmp_path):
    source = chunk()
    request = HDRPreviewChunk.model_validate(source)
    service = Service(tmp_path / "workspace")
    result = execute(service, "encode_motion_hdr_preview", {"chunk": source})
    assert result["transient"] is True
    assert result["verification"]["strict_decode"] is True
    assert result["verification"]["decoded"]["pix_fmt"] == "yuv420p10le"
    assert result["verification"]["decoded"]["r_frame_rate"] == "24000/1001"
    assert result["mime_type"].startswith('video/mp4; codecs="hvc1.2.')
    path = tmp_path / "hdr-preview.mp4"
    path.write_bytes(base64.b64decode(result["data"]))
    assert b"moof" in path.read_bytes()
    # Decode the actual video back into encoded RGB. This reverses YCbCr/range
    # only, so the comparison catches double tone mapping and wrong alpha order.
    decoded = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
        "-vf", "zscale=matrixin=2020_ncl:matrix=gbr:rangein=limited:range=full,format=gbrpf32le",
        "-f", "rawvideo", "-",
    ], capture_output=True, check=True, timeout=10)
    planar = np.frombuffer(decoded.stdout, "<f4").reshape(3, 32, 32)
    rgb = np.stack([planar[2], planar[0], planar[1]], axis=-1)
    linear = decode_float_frame(base64.b64decode(source["frames"][0]), 32, 32,
                               source=request.source_color_space)
    expected = render_output(linear, "PQ", keep_alpha=False)[..., :3]
    np.testing.assert_allclose(rgb[4:12, 4:28], expected[4:12, 4:28], atol=0.008)
    np.testing.assert_allclose(rgb[20:28, 4:28], expected[20:28, 4:28], atol=0.008)
    assert float(rgb[24, 16, 0]) > float(rgb[8, 16, 0]) + 0.15
    (tmp_path / "verification.json").write_text(json.dumps(result["verification"], indent=2))
    assert not list((tmp_path / "workspace").rglob("*.mp4"))


def test_http_registry_parity_and_encoder_failure_recovery(tmp_path, monkeypatch):
    import chromascript.motion_hdr_preview as hdr

    # Registry reflection supplies the same strict nested model to MCP and CLI.
    assert not REGISTRY["encode_motion_hdr_preview"].serialized
    schema = REGISTRY["encode_motion_hdr_preview"].input_model.model_json_schema()
    assert "HDRPreviewChunk" in schema["$defs"]
    with TestClient(create_app(tmp_path / "workspace", 0)) as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post("/api/operations/encode_motion_hdr_preview", headers=headers,
                               json={"chunk": {**chunk(), "frames": []}})
        assert response.status_code >= 400
        original = hdr.subprocess.run
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("ffmpeg", 20)
        monkeypatch.setattr(hdr.subprocess, "run", timeout)
        with pytest.raises(ChromaError, match="超时"):
            encode_hdr_preview(HDRPreviewChunk.model_validate(chunk()))
        monkeypatch.setattr(hdr.subprocess, "run", original)
        # Timeout releases the single encoder lease; the next explicit retry works.
        response = client.post("/api/operations/encode_motion_hdr_preview", headers=headers,
                               json={"chunk": chunk()})
        assert response.status_code == 200
        assert response.json()["verification"]["strict_decode"]


def test_720p_and_original_rational_rates_have_bounded_payloads():
    # FPS is never reduced to fit a preview cap. Memory is bounded independently.
    for width, height in [(1280, 720), (720, 1280)]:
        header = HDRPreviewHeader(width=width, height=height,
            fps={"numerator":60000, "denominator":1001}, frame_count=64,
            source_color_space="ACEScg", input_format="p010-pq")
        assert header.fps.ffmpeg == "60000/1001"
    for change in ({"width":1282}, {"height":722}, {"frame_count":65},
                   {"input_format":"rgba32f-linear"}):
        with pytest.raises(ValidationError):
            HDRPreviewHeader.model_validate({
                "width":1280, "height":720, "fps":{"numerator":60,"denominator":1},
                "frame_count":64, "source_color_space":"ACEScg", "input_format":"p010-pq",
                **change,
            })


def test_binary_and_json_use_identical_p010_processing(tmp_path, monkeypatch):
    import chromascript.motion_hdr_preview as hdr

    monkeypatch.setattr(hdr, "_hardware_available", lambda: False)
    monkeypatch.setattr(hdr, "render_output", lambda *a, **k: pytest.fail("P010 must not be tone mapped again"))
    width, height = 64, 32
    pixels = np.empty(width*height*3//2, dtype="<u2")
    pixels[:width*height] = 400 << 6
    pixels[width*height:] = 512 << 6
    header = HDRPreviewHeader(width=width, height=height, frame_count=2,
        fps={"numerator":60000,"denominator":1001}, source_color_space="Linear Rec.2020",
        input_format="p010-pq")
    binary = encode_hdr_preview_binary(header, pixels.tobytes()*2)
    legacy_transport = encode_hdr_preview(HDRPreviewChunk.model_validate({
        **header.model_dump(exclude={"frame_count"}),
        "frames":[base64.b64encode(pixels.tobytes()).decode()]*2,
    }))
    assert binary["verification"] == legacy_transport["verification"]
    assert binary["verification"]["decoded"]["r_frame_rate"] == "60000/1001"
    with TestClient(create_app(tmp_path / "binary", 0)) as client:
        token = client.get("/api/session").json()["token"]
        route = "/api/operations/encode_motion_hdr_preview/binary"
        headers = {"Authorization":f"Bearer {token}", "X-Chroma-HDR":header.model_dump_json(),
                   "Content-Type":"application/octet-stream"}
        assert client.post(route, content=pixels.tobytes()).status_code == 401
        assert client.post(route, headers=headers, content=b"short").status_code == 400
        response = client.post(route, headers=headers, content=pixels.tobytes()*2)
        assert response.status_code == 200
        assert response.json()["verification"] == binary["verification"]
    pixels[0] |= 1
    with pytest.raises(ChromaError, match="低 6 位"):
        encode_hdr_preview_binary(header, pixels.tobytes()*2)


@pytest.mark.parametrize("fps", [(60000, 1001), (24000, 1001), (60, 1)])
@pytest.mark.parametrize("count", [1, 8])
def test_p010_packet_clock_matches_rational_seek_frames(tmp_path, monkeypatch, fps, count):
    import chromascript.motion_hdr_preview as hdr

    # Force the portable encoder too: nominal SPS frame rate can be right while
    # the MP4 packet duration is wrong. A paused seek must cover exactly one frame.
    monkeypatch.setattr(hdr, "_hardware_available", lambda: False)
    width, height = 64, 32
    pixels = np.full(width * height * 3 // 2, 512 << 6, dtype="<u2")
    result = encode_hdr_preview_binary(HDRPreviewHeader(
        width=width, height=height, frame_count=count,
        fps={"numerator": fps[0], "denominator": fps[1]},
        source_color_space="Linear Rec.2020", input_format="p010-pq",
    ), pixels.tobytes() * count)
    path = tmp_path / "seek.mp4"
    path.write_bytes(base64.b64decode(result["data"]))
    decoded = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_packets", "-of", "json", str(path),
    ]))
    clock = Fraction(decoded["streams"][0]["time_base"])
    step = Fraction(fps[1], fps[0])
    packets = decoded["packets"]
    assert len(packets) == count
    assert result["verification"]["packet_timing"] is True
    for i, packet in enumerate(packets):
        assert (packet["pts"] - packets[0]["pts"]) * clock == i * step
        assert packet["dts"] == packet["pts"]
        assert packet["duration"] * clock == step


@pytest.mark.parametrize("count,numerator,denominator", [(1, 60000, 1001), (1, 24, 1), (8, 30, 1)])
def test_native_reused_fragments_never_include_drain_pictures(tmp_path, count, numerator, denominator):
    import chromascript.motion_hdr_preview as hdr
    from chromascript.hdr_encoder import close_encoder

    if not hdr._hardware_available():
        pytest.skip("Native VideoToolbox is unavailable")
    close_encoder()
    header = HDRPreviewHeader(width=64, height=32, frame_count=count,
        fps={"numerator": numerator, "denominator": denominator},
        source_color_space="Linear Rec.2020", input_format="p010-pq")
    samples = []
    try:
        for level in (240, 700):
            pixels = np.full(64 * 32 * 3 // 2, 512 << 6, dtype="<u2")
            pixels[:64 * 32] = level << 6
            result = encode_hdr_preview_binary(header, pixels.tobytes() * count)
            if result["encoder"] != "hevc_videotoolbox":
                pytest.skip("The native encoder cannot run in this environment")
            assert result["persistent_encoder"] is True
            assert result["verification"]["packet_timing"] is True
            path = tmp_path / f"{level}.mp4"
            path.write_bytes(base64.b64decode(result["data"]))
            decoded = subprocess.check_output([
                "ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-fps_mode", "passthrough",
                "-pix_fmt", "yuv420p10le", "-f", "rawvideo", "-",
            ], timeout=10)
            planes = np.frombuffer(decoded, dtype="<u2").reshape(count, -1)
            # Every independently decoded picture must belong to this request,
            # not a previous request's drain frames or a shifted IDR boundary.
            assert planes.shape[1] == 64 * 32 * 3 // 2
            assert np.max(np.abs(planes[:, :64 * 32].astype(float) - level)) < 10
            samples.append(result["timestamp_base"])
        assert samples[1] > samples[0]
    finally:
        close_encoder()


def test_combined_strict_probe_rejects_truncated_encoded_pixels(monkeypatch):
    import chromascript.motion_hdr_preview as hdr
    from chromascript.encoding import _boxes

    monkeypatch.setattr(hdr, "_hardware_available", lambda: False)
    metadata = hdr.quicktime_color_metadata
    def truncate_payload(path):
        result = metadata(path)
        data = path.read_bytes()
        with path.open("rb") as stream:
            ends = [end for kind, _, end in _boxes(stream, 0, len(data)) if kind == b"mdat"]
        # Corrupt actual compressed pixels while leaving the PQ/10-bit metadata
        # intact. Nominal tags and packet counts must never certify this file.
        path.write_bytes(data[:ends[-1] - 10])
        return result
    monkeypatch.setattr(hdr, "quicktime_color_metadata", truncate_payload)
    header = HDRPreviewHeader(width=64, height=32, frame_count=1,
        fps={"numerator":60,"denominator":1}, source_color_space="Linear Rec.2020", input_format="p010-pq")
    pixels = np.full(64 * 32 * 3 // 2, 512 << 6, dtype="<u2")
    with pytest.raises(ChromaError):
        encode_hdr_preview_binary(header, pixels.tobytes())
