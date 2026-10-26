"""Measure ordered 1/2/4-worker video export of an isolated source segment in WebKit."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from contextlib import closing
from pathlib import Path

from chromascript import motion_render
from chromascript.color import color_info, render_output
from chromascript.encoding import check_probe, encode
from chromascript.motion import MotionOutput, MotionSpec, motion_video_preset
from chromascript.motion_parallel import iter_parallel_frames
from chromascript.motion_processing import prepare_float_frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--start-frame", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    spec = MotionSpec.model_validate(snapshot["scene"])
    original = MotionOutput.model_validate(snapshot["output"])
    if original.video == "none":
        parser.error("Choose a snapshot with a video output")
    if args.frames < 1 or args.start_frame < 0 or args.start_frame + args.frames > spec.frames:
        parser.error("The requested segment must fit the source frame range")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    motion_render.RUNTIME = Path(__file__).resolve().parents[1] / "frontend/tests/float-export-runner.mjs"
    indices = list(range(args.start_frame, args.start_frame + args.frames))
    settings = spec.settings().model_copy(update={"frames": args.frames})
    preset = motion_video_preset(original.video)
    config_hash = color_info()["sha256"]
    reference: list[str] | None = None
    results = []
    for repeat in range(args.repeats):
        counts = [1, 2, 4]
        counts = counts[repeat % 3:] + counts[:repeat % 3]
        for count in counts:
            output = original.model_copy(update={"master_bit_depth": None, "render_workers": count})
            directory = root / f"{repeat}-{count}"
            directory.mkdir()
            hashes = []

            def frames(count=count, directory=directory, output=output, hashes=hashes):
                if count == 1:
                    with closing(motion_render.iter_float_frames(
                        spec, directory, indices, render_scale=output.render_scale,
                    )) as rendered:
                        for index, raw in rendered:
                            prepared = prepare_float_frame(index, raw, spec, output, config_hash)
                            pixels = render_output(prepared.pixels, preset.color.output, False)
                            hashes.append(hashlib.sha256(memoryview(pixels)).hexdigest())
                            yield pixels
                else:
                    with closing(iter_parallel_frames(spec, output, directory, indices,
                                                       config_hash=config_hash)) as rendered:
                        for prepared in rendered:
                            hashes.append(hashlib.sha256(memoryview(prepared.pixels)).hexdigest())
                            yield prepared.pixels

            start = time.monotonic()
            with closing(frames()) as produced:
                info = encode(preset, settings, directory / f"video.{preset.container}", produced, lambda _: None)
            verified = check_probe(info, preset, settings)
            elapsed = time.monotonic() - start
            if not verified["passed"]:
                raise RuntimeError(verified)
            if reference is None:
                reference = hashes
            if reference != hashes:
                mismatches = [index for index, (a, b) in zip(indices, zip(reference, hashes, strict=True), strict=True) if a != b]
                raise RuntimeError(f"Parallel source pixels differ at frames {mismatches}")
            result = {"repeat": repeat, "workers": count, "seconds": elapsed,
                      "render_fps": args.frames / elapsed, "pixels_equal": True,
                      "video_verified": verified["passed"],
                      "transport": json.loads((directory / "transport.json").read_text())}
            results.append(result)
            print(json.dumps(result), flush=True)
    medians = {count: statistics.median(r["seconds"] for r in results if r["workers"] == count) for count in [1, 2, 4]}
    report = {
        "browser": "headless WebKit", "source_sha256": spec.source_digest(),
        "width": spec.width, "height": spec.height, "render_scale": original.render_scale,
        "fps": spec.fps.model_dump(), "video": original.video, "source_frames": spec.frames,
        "start_frame": args.start_frame, "measured_frames": args.frames,
        "median_seconds": medians, "speedup": {n: medians[1] / medians[n] for n in medians},
        "samples": results,
        "limitations": ["Segment benchmark includes compilation, rendering, processing, video encoding and full verification.",
                        "Original absolute frame indices/time are preserved. Audio mix and final delivery bundle publication are excluded.",
                        "Runs alongside the user's existing serial export; timings may reflect resource contention.",
                        "WebKit measurements do not certify production Chromium performance."],
    }
    (root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"median_seconds": medians, "speedup": report["speedup"]}), flush=True)


if __name__ == "__main__":
    main()
