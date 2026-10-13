"""Model-facing guidance ships with the tool, independent of any chat transcript."""

DOCUMENTS = {
    # The browser branch has a distinct source-precision/color contract on all transports.
    "motion": (
        "Discover get_motion_capabilities and list_motion_templates for offline Remotion/Three.js. "
        "create_motion_scene creates a MotionSpec: name, default-exported React TSX component, "
        "even width/height (16..4096), frames (1..36000) and rational fps. Animate declaratively "
        "with useCurrentFrame()/useVideoConfig() and ThreeCanvas, not useFrame/delta or wall time. "
        "Allowed imports: react, react/jsx-runtime, remotion, @remotion/three, @react-three/fiber, "
        "three. No file/network imports or remote assets. compile_motion_preview accepts an unsaved "
        "spec and returns HTML/channel without revisions/jobs. Embed only in an allow-scripts "
        "iframe WITHOUT allow-same-origin. get_motion_scene reads current/historical source; "
        "update_motion_scene requires expected_revision_id. submit_motion_render requires saved "
        "scene_id, expected_revision_id, idempotency_key and optional writable export_directory. "
        "Poll get_job/list_jobs(kind=motion); cancel_job/resume_job are explicit. Retry rerenders "
        "frozen browser frames and refuses runtime drift. Chrome/Chromium, Node and FFmpeg must "
        "already be installed; no runtime browser download. Output: H.264 MP4, yuv420p, 8-bit "
        "browser sRGB, no audio, exact rational file fps, actual decoded-property verification. "
        "Composite transparency on black in linear sRGB. No ACES view or float/scene-linear master. "
        "Bundles include Animation.tsx, animation.json, snapshot, thumbnail, video and report. "
        "WGSL/ACEScg remains an independent capability."
    ),
    # Code and picker contracts apply identically to browser, MCP and CLI clients.
    "workflow": "Discover capabilities and templates. Create a project and scene. Read the revision before editing. Compile WGSL, render keyframes and a low-resolution motion preview. Poll get_job, then get_artifact(include_image=true) for native images. Inspect original EXR values with sample_pixels/inspect_frame. Change parameters using expected_revision_id. Call get_delivery_options with configuration and changed_field to resolve linked format choices. Validate the complete configuration with validate_delivery_configuration and submit_configured_delivery with configurations and an idempotency key, inspect reports and actual streams. Resume interrupted jobs using their frozen snapshots. Rendering is offline; never invent a verified combination.",
    "scene": "Define fn shade(ctx: SceneContext) -> vec4<f32>. Return finite straight-alpha ACEScg linear RGBA with alpha in 0..1; RGB may be negative or above 1. ctx.pixel is global top-left pixel coordinates; ctx.uv is bottom-left normalized coordinates; ctx.resolution is the full canvas; ctx.time is frame_index*fps.denominator/fps.numerator; ctx.frame and ctx.seed are u32. Access named parameters as ctx.params.NAME (number:f32, color:vec3<f32>, boolean:bool). Color parameters are converted from their declared storage encoding to linear ACEScg before shader execution; literal RGB values are also interpreted as linear ACEScg. There is no selectable scene input space. Do not bake sRGB/Rec.709 encoding or an ACES output transform into shade(). Declare no bindings or entry points. Use only supplied time/seed; no previous-frame state. Sampling accumulates premultiplied radiance and coverage, then unassociates. Get templates for compilable examples.",
    "color": "Code generation and internal processing are fixed to float32 linear ACEScg/AP1. Parameter.color_space identifies only stored color parameter values (sRGB or ACEScg), never the whole generated frame. Choose delivery color_space and gamma independently; output choices never change code interpretation or the fixed sRGB web preview. All color conversions use the pinned OCIO ACES configuration. Fixed ACES 2.0 CG config v4.0.0 is checksummed. EXR can carry unbaked ACEScg, AP0/ACES2065-1 or AP1/ACEScct. Scene encodings use color-space conversion without a display tone map; display deliverables apply one ACES output transform. Live PNGs, stored PNGs and motion previews use the sRGB SDR view. render_preview freezes the dedicated h264-srgb-preview recipe, independently of project/delivery settings. The browser requires verified sRGB output metadata and matching native tags; regenerate legacy Rec.709/Gamma 2.4 motion previews. The h264-review delivery preset remains Rec.709; default SDR video uses Rec.1886 Rec.709. P3-D65 uses its actual P3 ACES view; selecting a pure Gamma 2.2/2.4/2.6 changes display encoding only, without another tone map. PQ uses the ACES 2.0 1000-nit Rec.2020-limited view. HLG uses the same rendering with OCIO's official 1000-nit HLG display encoding. Alpha is never color transformed. FFmpeg changes matrix/range/sampling without another output transform. Model-visible PNGs are not calibrated HDR references. No fabricated mastering/MaxCLL/MaxFALL metadata is emitted. resolve_color takes request.color_space (ACEScg or sRGB) and exactly one of rgb (read-only inspection, original extended floats preserved) or oklch (lightness 0..1, chroma 0..0.4, hue 0..360). OKLCH is a D65 perceptual SDR picker, not an HDR appearance model. Out-of-sRGB selections reduce chroma at fixed lightness/hue; the exact ACES sRGB view is inverted and then forward-verified. Returned rgb is in the parameter's declared space, display_rgb is the separate display swatch, and gamut_mapped/round_trip_error/config describe actual conversion. Apply rgb through revision-checked scene operations only when requested. Utility convert_colors is not an inverse display transform.",
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
        "P3 gamma2.4/gamma2.6 and Rec.709 gamma2.6 currently use a MOV comment declaration "
        "requiring manual interpretation; other containers disable and reject those combinations. "
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
        "ACEScct integer video requires at least 10-bit and code values in [0,1]; float EXR "
        "preserves unbounded log values. AP1/ACEScct video uses unspecified native tags plus "
        "an explicit comment declaration; interpret it as ACES(AP1)/ACEScct when importing. "
        "Files carry native EXR chromaticities/colorInteropID, PNG sRGB/gAMA/cHRM, TIFF sRGB "
        "ICC, DPX Rec.709/numeric gamma, or video container/codec tags. MOV pure gamma "
        "2.4/2.6 uses colr transfer=2 and a native comment declaration, requiring manual "
        "Gamma interpretation. colr supersedes gama, so gama is never written alongside it. "
        "Gamma 2.2 uses transfer=4. MP4 Rec.709 retains the BT.709 convention; all Gamma "
        "2.4/2.6 video requires checking the intended input interpretation. "
        "Reports provide exact encoding and Resolve input suggestions, not certified "
        "application imports. Current FFmpeg ProRes/DNxHR input precision is at most 10-bit; "
        "decoded storage depth is reported separately. Verification compares decoded profile, "
        "RGB/YCbCr type, chroma subsampling, required channels and component precision. "
        "Evidence applies to exact dimensions, "
        "fps, codec/profile/pixel format and configuration. submit_configured_delivery takes "
        "1..20 distinct outputs. Legacy preset APIs remain compatible. 4K60 is an output "
        "acceptance target, not realtime. DCP, IMF and audio remain outside v1."
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
