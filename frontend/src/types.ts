export type Parameter = {
  name: string;
  label: string;
  kind: "number" | "color" | "boolean";
  default: unknown;
  minimum: number | null;
  maximum: number | null;
  color_space: "ACEScg" | "sRGB";
};
// Display appearance is separate from the unrestricted RGB stored in a scene.
export type OklchColor = {
  lightness: number;
  chroma: number;
  hue: number;
};
export type ColorAppearance = {
  model: "OKLCH";
  color_space: Parameter["color_space"];
  rgb: number[];
  oklch: OklchColor;
  display_rgb: number[];
  gamut_mapped: boolean;
  preview_clipped: boolean;
  extended_range: boolean;
  round_trip_error: number | null;
  display: "sRGB";
  view: string;
  config: string;
};
export type Scene = {
  schema_version: number;
  name: string;
  description: string;
  code: string;
  parameters: Parameter[];
  values: Record<string, unknown>;
  seed: number;
};
export type SceneRecord = {
  id: string;
  project_id: string;
  name: string;
  revision_id: string;
  updated_at: string;
};
export type SceneReply = {
  record: SceneRecord;
  scene: Scene;
  revision_id: string;
};
export type Project = {
  id: string;
  name: string;
  description: string;
  archived: boolean;
};
export type Settings = {
  width: number;
  height: number;
  frames: number;
  start_frame?: number;
  samples: number;
  fps: { numerator: number; denominator: number };
  timecode?: string;
};
// The configuration is user-owned; dropdown options are resolved by the shared core.
export type DeliveryConfiguration = {
  schema_version: 1;
  container: "mp4" | "mov" | "mxf" | "mkv" | "exr" | "png" | "tiff" | "dpx";
  codec:
    | "h264"
    | "hevc"
    | "prores"
    | "dnxhr"
    | "ffv1"
    | "rgb"
    | "yuv"
    | "exr"
    | "png"
    | "tiff"
    | "dpx";
  profile: string | null;
  bit_depth: 8 | 10 | 12 | 16 | 32;
  sampling: "RGB" | "420" | "422" | "444";
  alpha_mode: "none" | "straight" | "premultiplied";
  color_space:
    "P3-D65" | "Rec.709" | "Rec.2020" | "ACEScct" | "ACEScg" | "ACES2065-1";
  gamma:
    | "linear"
    | "acescct"
    | "gamma2.2"
    | "gamma2.4"
    | "gamma2.6"
    | "srgb"
    | "st2084"
    | "hlg";
  // Combined alias is retained for older API clients; the editor uses the pair.
  color_output:
    | "ACEScg"
    | "ACES2065-1"
    | "ACEScct"
    | "sRGB"
    | "Rec.709"
    | "Display-P3"
    | "P3-PQ"
    | "Rec.709-Gamma2.2"
    | "Rec.709-Gamma2.6"
    | "P3-Gamma2.2"
    | "P3-Gamma2.4"
    | "P3-Gamma2.6"
    | "PQ"
    | "HLG";
  // Auto is editing intent; the server freezes a concrete range in each output.
  data_range: "auto" | "full" | "limited";
  compression: "zip" | "piz" | "none" | null;
  quality_mode: "crf" | "bitrate" | "fixed";
  crf: number | null;
  bitrate_kbps: number | null;
  gop_seconds: number;
};
export type DeliverySelectField = Exclude<
  keyof DeliveryConfiguration,
  "schema_version" | "crf" | "bitrate_kbps" | "gop_seconds"
>;
// Fixed-view metadata is descriptive; changing it cannot request a different tone map.
export type ColorEncoding = {
  color_space_label: string;
  transfer_function_label: string;
  profile_label: string;
  white_point: "D60" | "D65";
  dynamic_range: "scene-linear" | "scene-log" | "SDR" | "HDR";
  signal_standard: string;
  target_display_peak_nits: number | null;
  target_display_peak_editable: false;
  limiting_gamut: string | null;
  output_view: string | null;
};
export type DeliveryOptions = {
  specification: "resolve-21-delivery-v1";
  color_encoding: ColorEncoding;
  file_tags: { color_space: string; gamma: string };
  resolved_data_range: "full" | "limited";
  gamma_note: string;
  color_space_note: string;
  data_range_note: string;
  color_note: string;
  configuration: DeliveryConfiguration;
  options: Record<
    DeliverySelectField,
    {
      value: string | number | null;
      label: string;
      disabled?: boolean;
      reason?: string;
    }[]
  >;
  adjustments: {
    field: string;
    label: string;
    previous: unknown;
    value: unknown;
  }[];
  frame_rates: { numerator: number; denominator: number }[];
  numeric_fields: Record<
    "crf" | "bitrate_kbps" | "gop_seconds",
    { min: number; max: number; step: number }
  >;
  constraints: {
    min_width: number;
    min_height: number;
    width_multiple: number;
    height_multiple: number;
  };
  precision_note: string;
};
export type Job = {
  // Display the server's actual job purpose instead of an opaque identifier alone.
  kind: "frame" | "preview" | "sequence" | "delivery" | "probe" | "motion";
  id: string;
  scene_id: string;
  project_id: string;
  revision_id: string;
  status: string;
  stage: string;
  progress: number;
  completed_frames: number;
  // Sparse keyframe counts differ from the source timeline's frame extent.
  frame_indices: number[] | null;
  elapsed_seconds: number;
  settings: Settings;
  error: { code: string; message: string } | null;
  artifacts: string[];
  // Older jobs have no bundle; a path alone does not certify export completion.
  export_path?: string | null;
  export_completed?: boolean;
};
export type Artifact = {
  id: string;
  name: string;
  kind: string;
  mime_type: string;
  size_bytes: number;
  metadata: Record<string, unknown>;
  job_id: string;
  relative_path: string;
};
export type Capability = {
  version: string;
  workspace: string;
  executable: string;
  gpu: { device: string; backend_type: string };
  color: { id: string; sha256: string };
  resolutions: Record<string, [number, number]>;
  frame_rates: { numerator: number; denominator: number }[];
  delivery_export?: {
    default_directory: string;
    // Legacy discovery key remains readable; native selection requires no roots.
    allowed_roots: string[];
    unrestricted?: boolean;
    directory_picker?: boolean;
  };
};
export type Template = { id: string; name: string; description: string };
export type Operation = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
};
// Browser animation specifications are separate from scene-linear WGSL sources.
export type MotionSpec = {
  schema_version?: number;
  name: string;
  code: string;
  width: number;
  height: number;
  frames: number;
  fps: { numerator: number; denominator: number };
};
export type MotionReply = {
  record: SceneRecord;
  scene: MotionSpec;
  revision_id: string;
};
export type MotionCapabilities = {
  preview_available: boolean;
  render_available: boolean;
  browser: string | null;
  imports: string[];
  packages: Record<string, string | null>;
};
