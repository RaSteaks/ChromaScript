"""Compare final float deliveries against a Git revision using headless WebKit only.

Run with the locked .venv Python. Outputs and native encoder logs stay in the
specified ignored report directory; no user projects or Chrome sessions are used.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from chromascript import motion_render, motion_worker
from chromascript.images import read_image
from chromascript.motion import MotionOutput, MotionSpec
from chromascript.service import Service

ROOT = Path(__file__).resolve().parents[1]

# The pre-optimization transport: serial capture, 1 MiB Base64 chunks, then ACK.
# Keep this reference in the benchmark only, never in a production fallback.
SERIAL_TRANSPORT = '''import {openSync,writeSync,closeSync} from "node:fs";
import {join} from "node:path";
export async function transportFloatFrames(page, indices, directory, accept) {
 const start=performance.now(); let capture_ms=0,transfer_ms=0,consumer_ms=0,bytes=0;
 for(const frame of indices) {
  let clock=performance.now();
  const capture=await page.evaluate(f=>window.__chromaFloat.capture(f),frame);
  capture_ms+=performance.now()-clock; clock=performance.now();
  const fd=openSync(join(directory,`frame-${frame}.f32`),"w");
  try { for(let offset=0;offset<capture.bytes;offset+=1048576) {
   const data=await page.evaluate(o=>window.__chromaFloat.readChunk(o,1048576),offset);
   writeSync(fd,Buffer.from(data,"base64"));
  }} finally {closeSync(fd);}
  transfer_ms+=performance.now()-clock; bytes+=capture.bytes; clock=performance.now();
  await accept(frame); consumer_ms+=performance.now()-clock;
 }
 return {capture_ms,transfer_ms,consumer_ms,bytes,total_ms:performance.now()-start,
  frames:indices.length,chunk_bytes:1048576,lookahead_frames:0};
}'''

SOURCE = '''import * as THREE from "three";
export default function createScene(){
 const scene=new THREE.Scene(),camera=new THREE.Camera();
 const material=new THREE.ShaderMaterial({transparent:true,depthTest:false,
 uniforms:{frame:{value:0}},vertexShader:`varying vec2 p; void main(){p=uv;gl_Position=vec4(position.xy,0.,1.);}`,
 fragmentShader:`varying vec2 p; uniform float frame;
 void main(){gl_FragColor=vec4(p.x*4.-.25,p.y*2.+frame/32.,.1234567,.5+p.x*.5);}`});
 scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
 return {scene,camera,update({frame}){material.uniforms.frame.value=frame;}};
}'''


def git_file(ref: str, path: str) -> bytes:
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT,
                          check=True, capture_output=True).stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--modes", nargs="+", choices=["master", "video", "combined"],
                        default=["master", "video", "combined"])
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    ref = subprocess.run(["git", "rev-parse", args.baseline_ref], cwd=ROOT,
                         check=True, capture_output=True, text=True).stdout.strip()
    baseline = root / "baseline"
    shutil.copytree(ROOT / "frontend/motion", baseline / "frontend/motion")
    (baseline / "frontend/node_modules").symlink_to(ROOT / "frontend/node_modules", target_is_directory=True)
    (baseline / "frontend/motion/float-runtime.mjs").write_bytes(git_file(ref, "frontend/motion/float-runtime.mjs"))
    baseline_worker = baseline / "motion_worker.py"
    baseline_worker.write_bytes(git_file(ref, "src/chromascript/motion_worker.py"))
    spec = importlib.util.spec_from_file_location("chromascript._baseline_export", baseline_worker)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    transport = baseline / "serial-transport.mjs"
    transport.write_text(SERIAL_TRANSPORT)
    motion_render.RUNTIME = ROOT / "frontend/tests/float-export-runner.mjs"
    scene = MotionSpec(code=SOURCE, source_color_space="ACEScg", width=args.width, height=args.height,
                       frames=args.frames, fps={"numerator": 30000, "denominator": 1001})
    samples = []
    for mode in args.modes:
        for repeat in range(args.repeats):
            deliveries = {}
            # Alternate order to reduce startup, filesystem-cache and thermal bias.
            for variant in (["before", "after"] if repeat % 2 == 0 else ["after", "before"]):
                case = root / f"{mode}-{repeat}-{variant}"
                service = Service(case / "workspace")
                project = service.create_project("export benchmark")["project"]["id"]
                saved = service.create_motion_scene(project, spec=scene)
                output = MotionOutput(master_bit_depth=None if mode == "video" else 32,
                                      video="none" if mode == "master" else "pq", render_scale=2)
                job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "benchmark",
                    export_directory=str(case / "delivery"), output=output)["job"]
                if variant == "before":
                    os.environ["CHROMASCRIPT_TEST_COMPILER"] = str(baseline / "frontend/motion/runtime.mjs")
                    os.environ["CHROMASCRIPT_TEST_TRANSPORT"] = str(transport)
                else:
                    os.environ.pop("CHROMASCRIPT_TEST_COMPILER", None)
                    os.environ.pop("CHROMASCRIPT_TEST_TRANSPORT", None)
                start = time.monotonic()
                (module if variant == "before" else motion_worker).run_motion(service.store.root, job["id"])
                elapsed = time.monotonic() - start
                result = service.get_job(job["id"])["job"]
                if result["status"] != "completed":
                    raise RuntimeError(result["error"])
                target = Path(result["export_path"])
                report = json.loads((target / "verification.json").read_text())
                deliveries[variant] = target
                samples.append({"mode": mode, "repeat": repeat, "variant": variant,
                                "seconds": elapsed, "verification": report["verification"]["passed"],
                                "performance": report.get("performance")})
                print(json.dumps(samples[-1]), flush=True)
            if mode != "video":
                for index in range(args.frames):
                    before = read_image(deliveries["before"] / f"masters/{index:08d}.exr")[0]
                    after = read_image(deliveries["after"] / f"masters/{index:08d}.exr")[0]
                    np.testing.assert_array_equal(after, before)
            if mode != "master":
                checksums = [subprocess.run(["ffmpeg", "-v", "error", "-i", str(path / "animation-pq.mp4"),
                             "-map", "0:v:0", "-f", "framemd5", "-"], check=True,
                             capture_output=True).stdout for path in (deliveries["before"], deliveries["after"])]
                assert checksums[0] == checksums[1], "Decoded video frames changed"
    medians = {}
    for mode in args.modes:
        values = {variant: statistics.median(s["seconds"] for s in samples
                  if s["mode"] == mode and s["variant"] == variant) for variant in ["before", "after"]}
        medians[mode] = {**values, "reduction_percent": 100 * (1 - values["after"] / values["before"])}
    (root / "summary.json").write_text(json.dumps({
        "baseline_revision": ref, "browser": "isolated headless Playwright WebKit",
        "width": args.width, "height": args.height, "frames": args.frames, "render_scale": 2,
        "fps": "30000/1001", "samples": samples, "median_seconds": medians,
        "exr_pixels_equal": True, "decoded_video_frames_equal": True,
        "limitations": ["Synthetic scene; not a production Chrome-driver benchmark.",
                        "Timing includes worker initialization, rendering, encoding, verification and publication; submission is excluded."],
    }, ensure_ascii=False, indent=2))
    print(json.dumps(medians), flush=True)


if __name__ == "__main__":
    main()
