export type Parameter = {
  name: string;
  label: string;
  kind: "number" | "color" | "boolean";
  default: unknown;
  minimum: number | null;
  maximum: number | null;
  color_space: string;
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
  color_output: "ACEScg" | "ACES2065-1" | "sRGB" | "Rec.709" | "PQ" | "HLG";
  data_range: "full" | "limited";
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
export type DeliveryOptions = {
  configuration: DeliveryConfiguration;
  options: Record<
    DeliverySelectField,
    { value: string | number | null; label: string }[]
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
};
export type Template = { id: string; name: string; description: string };
export type Operation = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
};
