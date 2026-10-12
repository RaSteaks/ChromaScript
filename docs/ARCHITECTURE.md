# Architecture

## Shared service

Python models define versioned Project, SceneSpec, RenderSettings, ColorPipeline, DeliveryConfiguration, Job and Artifact contracts. operations.py derives input schemas from typed public service methods. The same operations power FastAPI routes, generic CLI calls and namespaced MCP tools. Product transitions are serialized per service; SQLite transactions protect immutable revisions and idempotent jobs.

## Rendering and color

A worker owns one native wgpu device. It compiles a generated uniform/context wrapper around user WGSL, dispatches bounded tiles and checks every sample. Sampling accumulates coverage-weighted linear radiance, unassociates output, and writes the original float cache atomically.

OCIO owns input transforms, ACES view rendering and display encoding. RGB is processed independently from Alpha. HLG adds an official OCIO display encoding to the fixed CG config. Integer display quantization happens after output rendering; FFmpeg changes matrix/range/sampling without another tone map.

## Durability

The daemon owns a single dispatcher lock per workspace. Workers are independent process groups and continue after MCP or HTTP clients disconnect. Snapshot identity includes code, values, seed, settings, color config, package versions and numerical source fingerprint. Shared cached frames are checksummed before reuse.

A cancelled/failed/interrupted task can resume when its frozen identity still matches. Pixel files and manifests are atomically replaced. Existing output artifacts are immutable identities by job/path; retrying an export retains the job's frozen recipe.

## Interfaces

stdio adapters proxy to the daemon. Streamable HTTP is mounted on the same local service. Tool results use generated input/output schemas, structured JSON, native image content and managed resource URIs. Native previews are bounded PNGs; original large media remain artifacts.

The React UI is another client. Native selects keep OS ownership. Media uses a same-site HttpOnly cookie; mutations use in-memory bearer tokens and local-origin checks. Local clients read their descriptor from the workspace. Export paths are constrained to declared roots.

## Limits

macOS/Metal is the validated primary target. Outputs are offline; source scenes are 2D/procedural. Float32 GPU time has finite precision, while file timestamps use rational timebases. A codec's decoded storage depth may exceed its effective input precision. Image-sequence sidecars remain authoritative where consumers ignore header timing/color metadata.

## User-defined delivery

The delivery capability matrix resolves compatible container, codec, profile, depth, sampling, color, alpha, range, compression and quality choices using the installed native encoder pixel formats. The last changed field takes priority; compatible values survive. The workbench and model clients consume the same choices. Submission strictly validates the complete configuration and freezes resolved output descriptions without saving or looking up presets. The legacy DeliveryPreset representation remains an internal worker/output contract and compatibility API for older jobs and scripts.
