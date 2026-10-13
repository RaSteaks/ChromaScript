# Architecture

## Shared service

Python models define versioned Project, SceneSpec, RenderSettings, ColorPipeline, DeliveryConfiguration, Job and Artifact contracts. operations.py derives input schemas from typed public service methods. The same operations power FastAPI routes, generic CLI calls and namespaced MCP tools. Product transitions are serialized per service; SQLite transactions protect immutable revisions and idempotent jobs.

## Rendering and color

A worker owns one native wgpu device. It compiles a generated uniform/context wrapper around user WGSL, dispatches bounded tiles and checks every sample. Sampling reduces coverage-weighted linear radiance with balanced pairwise means, unassociates output, and writes the original float cache atomically. Normal float32 values are halved through exponent bits to prevent fast-math reassociation into an overflowing sum; ordinary underflow limitations remain.

OCIO owns input transforms, ACES view rendering and display encoding. RGB is processed independently from Alpha. HLG adds an official OCIO display encoding to the fixed CG config. Stored thumbnails decode already-rendered sRGB to display-linear light, filter premultiplied float planes, unassociate and re-encode before integer quantization. FFmpeg changes matrix/range/sampling without another tone map. MOV power curves without exact TRC identifiers use explicit comments and manual-import guidance; colr and gama are never combined. Delivery checks compare decoded profiles, component types, chroma subsampling and required channels while allowing legitimately wider decoder storage.

`render_live_preview` accepts a complete unsaved draft and renders one transient frame through the daemon's reusable device. Raster dimensions are capped at 480 pixels per side with single sampling, while shader coordinates retain the requested logical canvas and rational frame time. The display copy becomes an in-memory, straight-alpha 8-bit sRGB PNG; it never enters durable jobs or the EXR cache. Compiled pipelines use a 16-entry LRU bound to limit memory across code edits. The browser debounces edits, serializes requests and discards obsolete drafts; explicit playback drops display frames and stops in hidden views.

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
