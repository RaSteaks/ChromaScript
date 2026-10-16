"""Model-facing guidance ships with the tool, independent of any chat transcript."""

DOCUMENTS = {
    # Native float generation and display copies have different, explicit contracts.
    "motion": (
        "Animation contract motion-dual-v1 supports both three-float-v1 and remotion-sdr-v1. "
        "Discover get_motion_capabilities, get_motion_delivery_options(engine) and list_motion_templates. "
        "MotionSpec has engine=three-float-v1, name, code, positive signed-32-bit width/height, frames "
        "and rational fps components; actual GPU/memory/codec limits are checked. Optional files maps relative TS/TSX/JS/JSX paths to chapter source. "
        "code is the fixed Animation.ts (float) or Animation.tsx (SDR) entry. Import chapters with ./chapters/intro; "
        "relative imports resolve exact paths, then .ts/.tsx/.js/.jsx, then directory index files. "
        "Paths use ASCII letters/digits/underscore/hyphen and slash, with a required extension; no absolute paths, URLs, external files, entry collisions or case-only duplicates. "
        "Limits: 128 extra files, 200000 characters per file, 2000000 total. All files freeze atomically with revisions and jobs; source reviews hash the entire graph. "
        "The entry controls chapter ordering and timing: SDR can compose chapter components with Remotion Sequence; "
        "float factories import chapter update functions and pass absolute frame/time or explicit local offsets. Set complete state on every frame, including when seeking backwards. "
        "Preview and final render compile the same graph. Multi-file exports include a complete sources/ tree. "
        "Float package imports allow only three; the entry default-exports a synchronous "
        "createScene({width,height,frames,fps,frameRate}) factory returning {scene,camera,update}. "
        "Factory/update also receive rasterWidth/rasterHeight for gl_FragCoord normalization at preview/export sampling sizes. Logical width/height stay unchanged. update({frame,time,fps,width,height}) must set state from the frame, never integrate deltas "
        "or use wall time/randomness. time=frame*fps.denominator/fps.numerator. The renderer "
        "owns WebGL and RGBA32F targets. Three numeric RGB is scene-linear Rec.709/D65, not "
        "encoded sRGB or AP1; use values above 1 for radiance. No tone mapping/output encoding "
        "in materials. NormalBlending only for transparency; fractional-alpha shaders must set "
        "transparent=true. The host unassociates linear RGB and converts through pinned OCIO "
        "to ACEScg/AP1; alpha never enters OCIO. No DOM, CSS, SVG screenshot, remote asset or "
        "Node/file import. GPU capability is actually tested with negative, high and non-half "
        "float values; unsupported float readback fails rather than silently downgrading. "
        "compile_motion_preview returns offline HTML/channel; embed in allow-scripts WITHOUT "
        "allow-same-origin. Preview uses the pinned OCIO ACES 2.0 SDR shader and is always sRGB. "
        "render_motion_frame(spec,frame) returns a transient native sRGB PNG plus original "
        "ACEScg range statistics without saving/queueing. create_motion_scene(project_id) creates a blank SDR composition; engine=three-float-v1 creates a blank float scene. Omit template_id (or use null); examples are optional. Passing spec creates directly from source. Explicit engine must match template/spec. create_motion_scene/update_motion_scene "
        "preserve revisions; updates require expected_revision_id. submit_motion_render requires "
        "saved scene_id, expected_revision_id, idempotency_key and optional export_directory/output. "
        # ProRes identifiers freeze the codec and transfer; discovery checks the local encoder.
        "Float output={master_bit_depth:16|32|null,video:none|pq|hlg|srgb|prores-hq-srgb|prores-hq-pq|prores-hq-hlg,render_scale:1|2|4}; defaults 32/none/2. Null master depth selects video-only and requires a video format. SDR output={master_bit_depth:null,video:srgb|prores-hq-srgb,render_scale:1|2|4}; defaults null/srgb/2. ProRes is Apple ProRes 422 HQ MOV, prores_ks, 10-bit yuv422p10le, opaque. SDR screenshots retain their 8-bit source precision. ProRes disk budgets use the prores_ks HQ compressed macroblock target, padded 16x16 blocks and exact frame counts, plus 20% margin and mux overhead; not uncompressed storage or H.264/HEVC CRF rates. This planning estimate does not change encoder quality or impose a bitrate cap. Master mode produces straight "
        "ACEScg EXR masters; optional HDR video is HEVC Main10 yuv420p10le or ProRes 422 HQ yuv422p10le with one ACES 1000-nit "
        "PQ/HLG view, exact rational fps and decoded property verification. Video requires even "
        "dimensions and the corresponding encoder (libx265 HEVC, libx264 H.264, prores_ks ProRes); EXR supports odd dimensions and needs no video encoder. "
        "Do not invent mastering/MaxCLL/MaxFALL metadata. Half EXR is explicitly a quantized "
        "delivery from float32; inspect its actual round-trip error. Poll get_job/list_jobs(kind=motion), "
        "inspect_frame/sample_pixels on master-frame artifacts, get_artifact(include_image=true) "
        "on preview-image, and download master-archive/sequence-manifest/report/video artifacts. "
        "cancel_job/resume_job are explicit; master-mode retry reuses checksummed EXRs, video-only restarts from zero, and both reject runtime "
        "or OCIO drift. New HDR/SDR jobs render frames directly in a resumable directory "
        "on the frozen export volume and publish by rename; workspace retains metadata/logs only. "
        "Bundles contain Animation.ts, specification/snapshot, selected EXRs/video, sRGB thumbnail "
        "and verification report. master-archive is a virtual ZIP64 download (representation=zip-stream, "
        "size_bytes=null, local_path=null); HTTP streams it without a server-side ZIP. export_artifact "
        "writes that ZIP to the explicit CLI/MCP destination. Interrupted downloads restart from zero. "
        "Rendering is local/offline; historical files/snapshots are not migrated. "
        "SDR templates title/sdr-three use remotion-sdr-v1 and default-export React components, with useCurrentFrame/useVideoConfig and optional ThreeCanvas. Allow imports react, react/jsx-runtime, remotion, @remotion/three, @react-three/fiber, three. Historical records missing an engine are read as SDR without changing source/revision. SDR supports DOM/SVG, sRGB preview, and exactly tagged sRGB H.264 or ProRes 422 HQ video; no ACES display transform, native HDR, or float-master claim. Float sources may also render SDR through the ACES view. Select the engine explicitly for new source; do not reinterpret screenshot data as native float HDR."
    ),
    # Code and picker contracts apply identically to browser, MCP and CLI clients.
    "workflow": "Discover capabilities. Create a project, then call create_scene with only project_id for a blank WGSL canvas, or pass spec directly. Templates are optional examples. Read the revision before editing. Compile WGSL, render keyframes and a low-resolution motion preview. Poll get_job, then get_artifact(include_image=true) for native images. Inspect original EXR values with sample_pixels/inspect_frame. Change parameters using expected_revision_id. Call get_delivery_options with configuration and changed_field to resolve linked format choices. Validate the complete configuration with validate_delivery_configuration and submit_configured_delivery with configurations and an idempotency key, inspect reports and actual streams. Resume interrupted jobs using their frozen snapshots. Rendering is offline; never invent a verified combination.",
    "scene": "Define fn shade(ctx: SceneContext) -> vec4<f32>. Return finite straight-alpha ACEScg linear RGBA with alpha in 0..1; RGB may be negative or above 1. ctx.pixel is global top-left pixel coordinates; ctx.uv is bottom-left normalized coordinates; ctx.resolution is the full canvas; ctx.time is frame_index*fps.denominator/fps.numerator; ctx.frame and ctx.seed are u32. Access named parameters as ctx.params.NAME (number:f32, color:vec3<f32>, boolean:bool). Color parameters are converted from their declared storage encoding to linear ACEScg before shader execution; literal RGB values are also interpreted as linear ACEScg. There is no selectable scene input space. Do not bake sRGB/Rec.709 encoding or an ACES output transform into shade(). Declare no bindings or entry points. Use only supplied time/seed; no previous-frame state. Sampling accumulates premultiplied radiance and coverage, then unassociates. Get templates for compilable examples.",
    "color": "WGSL code generation and master processing are fixed to float32 linear ACEScg/AP1. Three.js HDR sources use their native linear Rec.709 and cross a pinned OCIO conversion boundary into the same ACEScg master pipeline; see the motion topic. Parameter.color_space identifies only stored color parameter values (sRGB or ACEScg), never the whole generated frame. Choose delivery color_space and gamma independently; output choices never change code interpretation or the fixed sRGB web preview. All color conversions use the pinned OCIO ACES configuration. Fixed ACES 2.0 CG config v4.0.0 is checksummed. EXR can carry unbaked ACEScg, AP0/ACES2065-1 or AP1/ACEScct. Scene encodings use color-space conversion without a display tone map; display deliverables apply one ACES output transform. Live PNGs, stored PNGs and motion previews use the sRGB SDR view. render_preview freezes the dedicated h264-srgb-preview recipe, independently of project/delivery settings. The browser requires verified sRGB output metadata and matching native tags; regenerate legacy Rec.709/Gamma 2.4 motion previews. The historical h264-review preset retains its Rec.709 definition but cannot be newly exported under exact-tag policy; default SDR video uses Rec.709 primaries with the sRGB transfer. P3-D65 uses its actual P3 ACES view; selecting a pure Gamma 2.2/2.4/2.6 changes display encoding only, without another tone map. PQ uses the ACES 2.0 1000-nit Rec.2020-limited view. HLG uses the same rendering with OCIO's official 1000-nit HLG display encoding. Alpha is never color transformed. FFmpeg changes matrix/range/sampling without another output transform. Model-visible PNGs are not calibrated HDR references. No fabricated mastering/MaxCLL/MaxFALL metadata is emitted. resolve_color takes request.color_space (ACEScg or sRGB) and exactly one of rgb (read-only inspection, original extended floats preserved) or oklch (lightness 0..1, chroma 0..0.4, hue 0..360). OKLCH is a D65 perceptual SDR picker, not an HDR appearance model. Out-of-sRGB selections reduce chroma at fixed lightness/hue; the exact ACES sRGB view is inverted and then forward-verified. Returned rgb is in the parameter's declared space, display_rgb is the separate display swatch, and gamut_mapped/round_trip_error/config describe actual conversion. Apply rgb through revision-checked scene operations only when requested. Utility convert_colors is not an inverse display transform.",
    # Model clients receive the same Resolve-style output, file-tag and Auto-level
    # semantics as the browser; stale prose must not imply that P3 always uses sRGB.
    "delivery": (
        "DeliveryConfiguration specifies container, codec, profile, bit_depth, sampling, "
        "color_space, gamma, alpha_mode (none/straight/premultiplied), data_range, "
        "compression, quality_mode, CRF or bitrate, and GOP. The implemented local subset "
        "follows Resolve 21 Deliver semantics; never infer codec precision from a profile name. "
        "color_space is P3-D65/Rec.709/Rec.2020/ACEScct/ACEScg/ACES2065-1. gamma is "
        "linear/acescct/gamma2.2/gamma2.4/gamma2.6/srgb/st2084/hlg. P3-D65 is a gamut, "
        "independent of transfer: P3-D65/srgb is Display P3; ProRes does not imply sRGB. "
        "Rec.709 supports gamma2.2/gamma2.4/gamma2.6/srgb; P3-D65 also supports st2084. "
        "Pure gamma2.4/gamma2.6 video is disabled in all containers because native transfer identifiers are not exact. "
        "Rec.2020 supports st2084/hlg. ACEScct uses AP1 log; ACEScg/AP0 use linear. "
        "EXR accepts only ACEScg, ACES2065-1 or ACEScct in both legacy presets and direct "
        "delivery; other encodings are rejected before saving or queueing. "
        "Previously stored unsupported presets remain readable/exportable and are listed "
        "as unavailable with validation_error; they cannot validate or render. "
        "Query get_delivery_options first and use its returned configuration for strict "
        "validate_delivery_configuration/submit_configured_delivery. Invalid submissions "
        "are rejected with suggested_configuration; no preset saving or ID is required. "
        "The compatibility color_output alias must agree with color_space/gamma. "
        "get_delivery_options returns specification=resolve-21-delivery-v1, gamma_note, "
        "file_tags, resolved_data_range and color_encoding. File tags describe the actual "
        "rendered encoding, not an independent color transform or a metadata-only override. "
        "data_range defaults to auto (Resolve Auto), with full and limited (Resolve Video) "
        "choices; video/limit aliases normalize to limited. Auto resolves by codec and freezes "
        "a concrete range in each immutable output before queueing. Auto and an equivalent "
        "explicit range count as duplicate targets. Disabled choices include reasons. "
        "color_encoding separately records white point, output view, transfer and target "
        "display peak: fixed 100 nits SDR, 1000 nits HDR, or null for scene encodings. "
        "Gamma 2.6 does not imply a DCI white point or 48-nit theatrical output. "
        "ACEScct float EXR preserves unbounded log values. New AP1/ACEScct video is disabled "
        "because native primaries/transfer tags are unspecified; old comment declarations are historical only. "
        "Files carry native EXR chromaticities/colorInteropID, PNG sRGB/gAMA/cHRM, TIFF sRGB "
        "ICC, DPX Rec.709/numeric gamma, or video container/codec tags. Historical MOV declarations cannot satisfy new native-tag policy. "
        "colr supersedes gama, so gama is never written alongside it. "
        "Gamma 2.2 uses transfer=4; sRGB uses transfer=13. Pure Gamma 2.4/2.6 video is rejected. "
        "Reports provide exact encoding and Resolve input suggestions, not certified "
        "application imports. Current FFmpeg ProRes/DNxHR input precision is at most 10-bit; "
        "decoded storage depth is reported separately. Verification compares decoded profile, "
        "RGB/YCbCr type, chroma subsampling, required channels and component precision. "
        "Evidence applies to exact dimensions, "
        "fps, codec/profile/pixel format and configuration. submit_configured_delivery takes "
        "1..20 distinct outputs. Legacy preset APIs remain compatible. 4K60 is an output "
        "acceptance target, not realtime. DCP and IMF remain outside v1."
    ),
}

# Draft inspection is a read-only image operation across all transports; durable
# previews and deliveries still use immutable revisions and explicit submissions.
DOCUMENTS["workflow"] += (
    " Use render_live_preview with request.spec (the complete unsaved SceneSpec), width, height,"
    " rational fps and integer frame for a transient native sRGB PNG. The raster's longest side"
    " is at most 480 pixels with one sample per pixel, preserving logical scene coordinates and"
    " rational frame time. It writes no revisions, artifacts, EXR caches or jobs. Do not use its"
    " 8-bit display values for original-pixel inspection; render a stored frame for that."
)

# Stored previews filter the already-rendered display light; this is not another
# ACES output transform and never alters the original floating-point master.
DOCUMENTS["color"] += (
    " Stored previews above 1280 pixels are downsampled after decoding sRGB to display-linear "
    "light, with premultiplied alpha, then unassociated, re-encoded and quantized once. "
    "Spatial samples use balanced pairwise averages with overflow-safe halving; float32 "
    "rounding and underflow limits still apply."
)

# Export intent is frozen with the same revision/format recipe across every transport.
DOCUMENTS["delivery"] += (
    " New deliveries automatically publish a folder beneath export_directory (an optional "
    "absolute parent path, default workspace/exports). get_delivery_export_options returns "
    "the default, unrestricted=true and directory_picker capability; it validates a supplied "
    "writable absolute directory without creating it. No startup directory allowlist is required. "
    "submit_configured_delivery and submit_delivery both accept export_directory; CLI render "
    "exposes --export-directory. choose_export_directory opens the native macOS folder dialog "
    "with optional initial_directory and returns directory or cancelled=true without changing "
    "drafts or storing grants. The dialog does not block other operations. open_export_directory "
    "takes an existing directory and opens the local file manager. Show the open-folder action "
    "after successful export_completed. Legacy --export-root is accepted but not needed. Each folder "
    "is named from the scene and job ID and contains frozen shader.wgsl, reimportable scene.json, "
    "scene-snapshot.json, a first-frame sRGB thumbnail.png, every selected video/image sequence, "
    "verification.json and delivery.json with file hashes. Job.export_path names the folder; "
    "export_completed is true only after verified output and publication. Never overwrite "
    "existing user files. A retry may reuse an unchanged complete bundle from the same job. "
    "Changing the parent requires a new idempotency key. Failed copying retains managed outputs."
)

# Master-first creation, explicit attestations and exact video metadata are shared
# by web, MCP and CLI; historical declarations never bypass the current policy.
DOCUMENTS["motion"] += (
    " New blank animations default to three-float-v1; specify remotion-sdr-v1 for SDR. "
    "MotionOutput defaults to master_bit_depth=32, video=none, render_scale=2. "
    "render_scale is 1, 2 or 4 per axis; preview always uses one sample. No temporal blur. "
    "Both engines write intermediates/video directly to export_directory, including external disks; "
    "a missing/replaced disk fails without local fallback. Final report separates float_pipeline_verified "
    "from source_color_contract. record_motion_color_review(scene_id, expected_revision_id, reviewer, notes) "
    "records an explicit reviewer attestation, not an automated semantic audit; editing invalidates it. "
    "Unreviewed custom shaders remain renderable. rec709/Gamma 2.4 video is now disabled because no exact "
    "native transfer identifier exists. SDR sRGB and HDR PQ/HLG require matching stream/container color tags."
)
DOCUMENTS["delivery"] += (
    " Current mandatory policy supersedes historical declaration-only video workflows: every new video "
    "requires exact native gamut and transfer identifiers. Pure Gamma 2.4/2.6 and AP1/ACEScct video are "
    "disabled, including MOV, legacy preset and direct encoder calls. Comments are not a substitute. "
    "Use sRGB, Gamma 2.2, PQ or HLG where supported, or scene-referred EXR masters. "
    "Missing or inconsistent stream tags or MOV/MP4 colr makes native_color_tags_verified false and blocks publication."
)

# Audio is authored outside visual code, then frozen with each delivery. Browser
# previews stay silent; neither OCIO nor the shader sandbox processes PCM data.
DOCUMENTS["audio"] = (
    "Offline audio workflow: create_audio(project_id,name,spec) synthesizes a WAV asset; "
    "import_audio(project_id,name,source_path OR data_base64) imports a local file or encoded file bytes. "
    "No URLs, speech models or cloud services. AudioSynthesis has duration_seconds (0.001..600), "
    "seed (uint32), and up to 256 events. Events have waveform=sine|triangle|noise, start_seconds, "
    "duration_seconds, frequency_hz (20..20000), gain_db (-96..0; default -18), pan (-1 left..1 right), "
    "fade_seconds (default .005; capped to half the event). Empty events generate silence. "
    "Schedule repeated events for beats, simultaneous pitches for chords, and noise for effects. "
    "Triangle harmonics are not band-limited; choose sine for spectrally pure tones. All events "
    "must fit the asset duration. Assets are immutable 48 kHz stereo signed 32-bit PCM WAV; this "
    "storage format is not a claim of source precision. Imports resample/downmix the first audio "
    "stream, accept at most 128 MiB and 600 seconds, and retain source_probe plus normalized probe. "
    "get_project_audio(project_id) returns assets and revision_id (initially '0'). "
    "set_project_audio(project_id,expected_revision_id,timeline={clips:[...]}) explicitly replaces "
    "up to 32 clips. Each clip requires asset_id; optional start_seconds is a project/scene "
    "timeline offset, source_start_seconds trims the source, duration_seconds=null plays to its end, "
    "and gain_db (-96..12) changes mix volume. Only assets from this project are accepted. "
    "Read before editing and preserve drafts on revision_conflict. Empty clips clear future audio. "
    "Creating/importing an asset alone does not attach it: add its asset.id as a clip and save. "
    "submit_delivery, submit_configured_delivery, submit_batch, submit_default_delivery and submit_motion_render "
    "freeze the current project timeline, asset checksums and audio implementation. Project tracks "
    "apply to every scene's new deliveries. A changed timeline requires a new idempotency key. "
    "Later edits cannot affect queued jobs; missing/changed source or implementation drift rejects retry. "
    "Preview/frame/render_sequence operations remain silent; use a delivery for a sequence plus audio. "
    "Mix duration is frames*fps.denominator/fps.numerator; start_frame crops the project timeline. "
    "Convert absolute boundaries to nearest 48 kHz sample (ties to even), never round each frame. "
    "Pad gaps with silence and trim excess at the delivery end. Saturation is explicit and "
    "clipped_samples counts channel samples; lower gains to avoid clipping. WAV mixes must fit under 4 GiB. "
    "Every audio-enabled delivery includes audio.wav; image sequences cannot embed audio. MP4 uses AAC, "
    "MOV/MKV/MXF use PCM s24le where the selected video combination is supported. Video verification "
    "includes actual audio stream properties, full decoded sample count/peak and codec padding tolerance. "
    "The independent WAV keeps exact sample length. Color and alpha processing are unchanged. "
    "Asset IDs also identify audio-source artifacts: get_artifact/probe_media/export_artifact work "
    "normally, and the authenticated artifact URL supports browser playback and range reads. "
    "The UI Project Audio editor supports import, single-event synthesis, source audition and clip editing; "
    "MCP/CLI support full multi-event synthesis. Editing previews do not play the project soundtrack. "
    "CLI equivalence: chromascript call create_audio --arguments @audio.json; "
    "chromascript call get_project_audio --arguments '{\"project_id\":\"PROJECT_ID\"}'. "
    "MCP tools use the same operation names with the chromascript. prefix."
)
DOCUMENTS["workflow"] += (
    " For sound, read the audio topic, create_audio or import_audio, then set_project_audio "
    "before submitting the visual delivery. Audio tools run locally and are independent of shaders."
)
DOCUMENTS["motion"] += " Project soundtracks are supported through the audio tools; read the audio topic."
DOCUMENTS["delivery"] += " Project soundtracks are supported through the audio tools; read the audio topic."

# Storage estimates are a read-only view of the same budget checked at submission.
DOCUMENTS["motion"] += (
    " estimate_motion_render(settings, engine=three-float-v1, output?, export_directory?, project_id?) "
    "estimates a fresh animation delivery without rendering, saving revisions or creating directories. "
    "settings uses MotionRenderSettings: width, height, frames and rational fps; code is unnecessary. "
    "master_uncompressed_bytes = width*height*4*(master_bit_depth/8)*frames. Spatial render_scale "
    "changes transient raster storage only. compressed_master_bytes is null: ZIP size depends on content. "
    "The returned destination_required_bytes budget includes the master, 10% master overhead, temporary "
    "frames, optional video/audio and auxiliary files. Video budgets include a PCM-sized allowance "
    "for the muxed audio stream in addition to the separate WAV. Pass project_id to include its current audio "
    "timeline; otherwise audio_bytes is zero. workspace_required_bytes is counted on the same volume "
    "only once; separate volumes have independent free-space checks. destination_free_bytes and "
    "workspace_free_bytes are current observations, not reservations. sufficient indicates budget "
    "availability, not GPU/codec support or guaranteed completion. SDR has zero EXR master bytes and "
    "budgets screenshot intermediates/video instead. Existing caches are not deducted; streamed master "
    "ZIP downloads do not require a second server-side sequence. submit_motion_render uses this same "
    "calculation and rechecks space, without freezing fluctuating free-space values into job identity."
)


DOCUMENTS["motion"] += (
    " Float animation output also supports video-only: set master_bit_depth=null and video=pq, hlg or srgb. "
    "The UI labels this 仅视频. Existing master defaults remain unchanged. Each float frame is converted "
    "to ACEScg, composited on black and display-rendered once directly into FFmpeg; no EXR sequence "
    "or second GPU render is created. A single disposable .f32 transport lives on the export volume. "
    "The verification report sets scene_linear_master=false while independently verifying the float "
    "pipeline and actual video codec, depth, rational rate and native color tags. Interrupted video-only "
    "jobs restart at frame zero; already-published bundles recover without rerendering. "
    "estimate_motion_render reports streaming-video-budget-v1, zero master bytes and a single-frame "
    "intermediate. video_planning_bps=max(8000000,ceil(width*height*fps/5)); the duration-based estimate "
    "adds 20% and muxing allowance. This is not an encoder cap or size guarantee; CRF quality is "
    "unchanged and the worker checks remaining free space. UI, CLI call and MCP use the same operations."
)
