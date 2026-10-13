"""Model-facing guidance ships with the tool, independent of any chat transcript."""

DOCUMENTS = {
    # The visual picker has the same read-only conversion contract for model clients.
    "workflow": "Discover capabilities and templates. Create a project and scene. Read the revision before editing. Compile WGSL, render keyframes and a low-resolution motion preview. Poll get_job, then get_artifact(include_image=true) for native images. Inspect original EXR values with sample_pixels/inspect_frame. Change parameters using expected_revision_id. Call get_delivery_options with configuration and changed_field to resolve linked format choices. Validate the complete configuration with validate_delivery_configuration and submit_configured_delivery with configurations and an idempotency key, inspect reports and actual streams. Resume interrupted jobs using their frozen snapshots. Rendering is offline; never invent a verified combination.",
    "scene": "Define fn shade(ctx: SceneContext) -> vec4<f32>. Return finite straight-alpha ACEScg linear RGBA with alpha in 0..1; RGB may be negative or above 1. ctx.pixel is global top-left pixel coordinates; ctx.uv is bottom-left normalized coordinates; ctx.resolution is the full canvas; ctx.time is frame_index*fps.denominator/fps.numerator; ctx.frame and ctx.seed are u32. Access named parameters as ctx.params.NAME (number:f32, color:vec3<f32>, boolean:bool). Declare no bindings or entry points. Use only supplied time/seed; no previous-frame state. Sampling accumulates premultiplied radiance and coverage, then unassociates. Get templates for compilable examples.",
    "color": "Work in float32 ACEScg/AP1. Explicitly identify sRGB/ACEScg input colors. Fixed ACES 2.0 CG config v4.0.0 is checksummed. EXR is unbaked ACEScg or AP0/ACES2065-1. Previews use the sRGB SDR view; default SDR video uses Rec.1886 Rec.709. P3-D65 uses its actual P3 ACES view; selecting a pure Gamma 2.2/2.4/2.6 changes display encoding only, without another tone map. PQ uses the ACES 2.0 1000-nit Rec.2020-limited view. HLG uses the same rendering with OCIO's official 1000-nit HLG display encoding. Alpha is never color transformed. FFmpeg changes matrix/range/sampling without another output transform. Model-visible PNGs are not calibrated HDR references. No fabricated mastering/MaxCLL/MaxFALL metadata is emitted. resolve_color takes request.color_space (ACEScg or sRGB) and exactly one of rgb (read-only inspection, original extended floats preserved) or oklch (lightness 0..1, chroma 0..0.4, hue 0..360). OKLCH is a D65 perceptual SDR picker, not an HDR appearance model. Out-of-sRGB selections reduce chroma at fixed lightness/hue; the exact ACES sRGB view is inverted and then forward-verified. Returned rgb is in the parameter's declared space, display_rgb is the separate display swatch, and gamut_mapped/round_trip_error/config describe actual conversion. Apply rgb through revision-checked scene operations only when requested. Utility convert_colors is not an inverse display transform.",
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
