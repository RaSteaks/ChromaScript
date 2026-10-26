"""Compare reference, GPU/software and GPU/hardware on one original source segment."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from contextlib import closing
from pathlib import Path

from chromascript import motion_render
from chromascript.color import color_info, render_output
from chromascript.encoding import check_probe, encode
from chromascript.motion import MotionOutput, MotionSpec
from chromascript.motion_color import decode_float_frame
from chromascript.motion_parallel import iter_parallel_frames
from chromascript.motion_video_plan import resolve_video_execution
from chromascript.motion_video_render import iter_video_frames
from chromascript.video_transport import VideoFrame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-frame", type=int, default=6000)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--workers", type=int, choices=[1, 2, 4], default=1)
    parser.add_argument("--quality", choices=["quality", "balanced", "speed"], default="quality")
    parser.add_argument("--reference-video", type=Path)
    parser.add_argument("--modes", nargs="+", choices=["reference", "gpu-software", "gpu-hardware"],
                        default=["reference", "gpu-software", "gpu-hardware"])
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    spec = MotionSpec.model_validate(snapshot["scene"])
    original = MotionOutput.model_validate(snapshot["output"])
    indices = list(range(args.start_frame, args.start_frame + args.frames))
    if not indices or indices[0] < 0 or indices[-1] >= spec.frames:
        parser.error("Segment must fit the source")
    args.output.mkdir(parents=True, exist_ok=False)
    motion_render.RUNTIME = Path(__file__).resolve().parents[1] / "frontend/tests/float-export-runner.mjs"
    settings = spec.settings().model_copy(update={"frames": args.frames})
    results = []
    for name, processing, encoder in [("reference", "reference", "software"),
                                      ("gpu-software", "auto", "software"),
                                      ("gpu-hardware", "auto", "hardware")]:
        if name not in args.modes:
            continue
        directory = args.output / name
        directory.mkdir()
        output = original.model_copy(update={"master_bit_depth": None, "video_processing": processing,
                                             "video_encoder": encoder, "video_quality": args.quality,
                                             "render_workers": args.workers})
        plan = resolve_video_execution(spec.engine, output, settings)
        assert plan is not None

        def frames(plan=plan, directory=directory, output=output):
            if output.render_workers > 1:
                with closing(iter_parallel_frames(spec, output, directory, indices,
                    config_hash=color_info()["sha256"], video_input=plan.pixel_format)) as rendered:
                    for frame in rendered:
                        yield VideoFrame(memoryview(frame.pixels), plan.pixel_format) if plan.pixel_format else frame.pixels
            elif plan.processing == "gpu":
                assert plan.pixel_format is not None
                with closing(iter_video_frames(spec, directory, indices, plan.preset.color.output,
                                               render_scale=output.render_scale, pixel_format=plan.pixel_format)) as packets:
                    for data in packets:
                        yield VideoFrame(data, plan.pixel_format)
            else:
                with closing(motion_render.iter_float_frames(spec, directory, indices,
                                                            render_scale=output.render_scale)) as rendered:
                    for _, raw in rendered:
                        pixels = decode_float_frame(raw.read_bytes(), spec.width, spec.height,
                                                    output.render_scale, spec.source_color_space)
                        yield render_output(pixels, plan.preset.color.output, False)
        start = time.monotonic()
        with closing(frames()) as produced:
            info = encode(plan.preset, settings, directory / "video.mp4", produced, lambda _: None,
                          video_input=plan.pixel_format, speed=plan.speed)
        elapsed = time.monotonic() - start
        checked = check_probe(info, plan.preset, settings)
        assert checked["passed"], checked["issues"]
        result = {"mode": name, "seconds": elapsed, "fps": args.frames / elapsed,
                  "transport": json.loads((directory / "transport.json").read_text()),
                  "video_verified": checked["passed"], "preset": plan.preset.model_dump()}
        reference_video = args.reference_video or args.output / "reference/video.mp4"
        if name != "reference" and reference_video.exists():
            for metric in ("ssim", "psnr"):
                measured = subprocess.run([
                    "ffmpeg", "-v", "info", "-i", str(reference_video),
                    "-i", str(directory / "video.mp4"), "-lavfi", metric,
                    "-an", "-f", "null", "-",
                ], capture_output=True, text=True, check=True)
                (directory / f"{metric}.log").write_text(measured.stderr)
                match = re.search(r"All:([\d.]+)" if metric == "ssim" else r"average:([\d.inf]+)", measured.stderr)
                result[metric] = match.group(1) if match else None
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    (args.output / "summary.json").write_text(json.dumps({
        "source_sha256": spec.source_digest(), "width": spec.width, "height": spec.height,
        "frames": args.frames, "start_frame": args.start_frame, "render_scale": original.render_scale,
        "workers": args.workers, "quality": args.quality,
        "browser": "isolated headless WebKit", "results": results,
        "limitations": ["Segment benchmark; excludes audio mix and final bundle publication.",
                        "Other user exports may contend for resources; not a production Chromium certification."],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
