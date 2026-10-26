"""GPU output is checked against independent native YUV conversion of CPU OCIO."""

import json
import subprocess
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from chromascript import motion_render
from chromascript.color import render_output
from chromascript.errors import ChromaError
from chromascript.motion import MotionSpec
from chromascript.motion_color import decode_float_frame
from chromascript.motion_video_render import iter_video_frames

SOURCE = '''import * as THREE from "three";
export default function createScene(){
 const scene=new THREE.Scene(),camera=new THREE.Camera();
 const material=new THREE.ShaderMaterial({transparent:true,depthTest:false,
 uniforms:{frame:{value:0}},vertexShader:`varying vec2 p;void main(){p=uv;gl_Position=vec4(position.xy,0.,1.);}`,
 fragmentShader:`varying vec2 p;uniform float frame;
 void main(){gl_FragColor=vec4(p.x*4.-.25,p.y*2.+frame*.125,.1234567,.25+p.x*.75);}`});
 scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
 return {scene,camera,update({frame}){material.uniforms.frame.value=frame;}};
}'''


@pytest.fixture
def webkit(monkeypatch):
    monkeypatch.setattr(motion_render, "RUNTIME", Path(__file__).resolve().parents[1] /
                        "frontend/tests/float-export-runner.mjs")


@pytest.mark.integration
@pytest.mark.parametrize("output", ["sRGB", "PQ", "HLG"])
@pytest.mark.parametrize("scale", [1, 2, 4])
def test_gpu_video_matches_cpu_color_and_physical_yuv(tmp_path, webkit, output, scale):
    spec = MotionSpec(code=SOURCE, source_color_space="Linear Rec.2020", width=32, height=18, frames=3)
    with closing(motion_render.iter_float_frames(spec, tmp_path / "reference", [0, 1, 2], render_scale=scale)) as frames:
        references = [render_output(decode_float_frame(path.read_bytes(), 32, 18, scale,
                     spec.source_color_space), output, False) for _, path in frames]
    thumbnail = tmp_path / "thumbnail.png"
    with closing(iter_video_frames(spec, tmp_path / "gpu", [0, 1, 2], output,
                                   render_scale=scale, thumbnail=thumbnail)) as frames:
        actual = list(frames)
    assert thumbnail.exists()
    assert not list((tmp_path / "gpu").glob("*.f32"))
    stats = json.loads((tmp_path / "gpu/transport.json").read_text())
    assert stats["binary_pipe"] and stats["in_flight"] == 3
    assert stats["temporary_frame_files"] == 0
    ten = output != "sRGB"
    for pixels, data in zip(references, actual, strict=True):
        # zimg supplies an independent bilinear chroma implementation and phases.
        rgb = np.ascontiguousarray(pixels[..., [1, 2, 0]].transpose(2, 0, 1), dtype="<f4")
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", "gbrpf32le",
            "-video_size", "32x18", "-i", "pipe:0", "-vf",
            f"zscale=matrixin=gbr:matrix={'2020_ncl' if ten else '709'}:rangein=full:range=limited:"
            f"chromal={'topleft' if ten else 'left'}:dither=none,format={'yuv420p10le' if ten else 'yuv420p'},"
            f"format={'p010le' if ten else 'nv12'}",
            "-frames:v", "1", "-f", "rawvideo", "pipe:1",
        ], input=rgb.tobytes(), capture_output=True, check=True)
        dtype = "<u2" if ten else "u1"
        values = np.frombuffer(data, dtype).astype(int)
        reference = np.frombuffer(result.stdout, dtype).astype(int)
        if ten:
            assert not np.any(values & 63)
            values //= 64
            reference //= 64
        assert np.max(np.abs(values - reference)) <= (3 if ten else 1)


@pytest.mark.integration
def test_invalid_alpha_rejected_before_video_packet(tmp_path, webkit):
    spec = MotionSpec(code=SOURCE.replace(".25+p.x*.75", "2."), width=18, height=18, frames=1)
    with pytest.raises(ChromaError, match="Alpha"), closing(iter_video_frames(spec, tmp_path, [0], "sRGB")) as frames:
        next(frames)


@pytest.mark.integration
@pytest.mark.parametrize("output,pixel_format,depth", [("sRGB", "p210le", 10), ("HLG", "p210le", 10), ("ACEScct", "p416le", 16)])
def test_gpu_prores_transport_matches_cpu(tmp_path, webkit, output, pixel_format, depth):
    spec = MotionSpec(code=SOURCE.replace("p.x*4.-.25", ".25+p.x*4."), source_color_space="ACEScg", width=32, height=18, frames=1)
    with closing(motion_render.iter_float_frames(spec, tmp_path / "reference", [0])) as frames:
        _, path = next(frames)
        pixels = render_output(decode_float_frame(path.read_bytes(), 32, 18, source="ACEScg"), output, False)
    with closing(iter_video_frames(spec, tmp_path / "gpu", [0], output, pixel_format=pixel_format)) as frames:
        data = next(frames)
    rgb = np.ascontiguousarray(pixels[..., [1, 2, 0]].transpose(2, 0, 1), dtype="<f4")
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", "gbrpf32le", "-video_size", "32x18", "-i", "pipe:0",
        "-vf", f"zscale=matrixin=gbr:matrix={'2020_ncl' if output == 'HLG' else '709'}:rangein=full:range=limited:chromal=left:dither=none,"
        f"format={'yuv422p10le' if depth == 10 else 'yuv444p16le'},format={pixel_format}",
        "-frames:v", "1", "-f", "rawvideo", "pipe:1",
    ], input=rgb.tobytes(), capture_output=True, check=True)
    actual = np.frombuffer(data, "<u2").astype(int) >> (16 - depth)
    expected = np.frombuffer(result.stdout, "<u2").astype(int) >> (16 - depth)
    assert np.max(np.abs(actual - expected)) <= 3


@pytest.mark.integration
@pytest.mark.parametrize("source", ["Linear Rec.709 (sRGB)", "ACEScg", "ACES2065-1"])
def test_gpu_source_bases_and_hdr_superwhite(tmp_path, webkit, source):
    code = SOURCE.replace("p.x*4.-.25,p.y*2.+frame*.125,.1234567,.25+p.x*.75",
                          "p.x<.5?16.:0.,0.,p.x<.5?0.:16.,1.")
    spec = MotionSpec(code=code, source_color_space=source, width=64, height=48, frames=1)
    with closing(motion_render.iter_float_frames(spec, tmp_path / "reference", [0])) as frames:
        _, path = next(frames)
        pixels = render_output(decode_float_frame(path.read_bytes(), 64, 48, source=source), "HLG", False)
    assert pixels[..., :3].max() > 1
    with closing(iter_video_frames(spec, tmp_path / "gpu", [0], "HLG")) as frames:
        data = next(frames)
    words = np.frombuffer(data, "<u2").astype(int) >> 6
    rgb = np.ascontiguousarray(pixels[..., [1, 2, 0]].transpose(2, 0, 1), dtype="<f4")
    reference = subprocess.run([
        "ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", "gbrpf32le", "-video_size", "64x48", "-i", "pipe:0",
        "-vf", "zscale=matrixin=gbr:matrix=2020_ncl:rangein=full:range=limited:chromal=topleft:dither=none,format=yuv420p10le,format=p010le",
        "-frames:v", "1", "-f", "rawvideo", "pipe:1",
    ], input=rgb.tobytes(), capture_output=True, check=True).stdout
    expected = np.frombuffer(reference, "<u2").astype(int) >> 6
    assert np.max(np.abs(words - expected)) <= 3
