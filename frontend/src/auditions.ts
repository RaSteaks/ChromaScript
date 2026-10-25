/** Public round projection deliberately omits source identifiers before reveal. */
export type Candidate = {
  id: string;
  label: string;
  playback_artifact_id: string;
  gain_db: number;
  normalization_available: boolean;
  asset_id?: string;
  name?: string;
  source_start_seconds?: number;
  note?: string;
  source?: string;
  measurement?: Record<string, unknown>;
};
export type Feedback = {
  preference: "reference" | "reject" | "unsure";
  liked: string;
  changes: string;
  scores: Partial<
    Record<"reading" | "quality" | "emotion" | "concept" | "repeat", number>
  >;
};
export type Direction = {
  outcome: "single" | "combine" | "explore";
  references: { candidate_id: string; aspects: string }[];
  instructions: string;
  next_step: "samples" | "full_score" | "pause";
};
export type Audition = {
  id: string;
  project_id: string;
  revision_id: string;
  title: string;
  purpose: string;
  duration_seconds: number;
  blind: boolean;
  revealed_at: string | null;
  archived: boolean;
  status: string;
  effective_target_lufs: number;
  loudness_target_lufs: number;
  candidates: Candidate[];
  baseline: {
    playback_artifact_id: string | null;
    gain_db: number;
    normalization_available: boolean;
  };
  context: {
    work_kind: "wgsl" | "motion";
    scene_id: string;
    revision_id: string;
    start_frame: number;
    frames: number;
    fps: { numerator: number; denominator: number };
  } | null;
  feedback: (Feedback & {
    candidate_id: string;
    reviewer: string;
    after_reveal: boolean;
  })[];
  direction: Direction | null;
  direction_confirmed: boolean;
  direction_revision_id: string | null;
  parent_direction_changed?: boolean;
  applications?: {
    id: string;
    candidate_id: string;
    timeline_revision_id: string;
    created_at: string;
    currently_used: boolean;
  }[];
  history?: {
    revision_id: string;
    updated_at: string;
    direction_confirmed: boolean;
  }[];
};
export const emptyFeedback = (): Feedback => ({
  preference: "unsure",
  liked: "",
  changes: "",
  scores: {},
});
export const emptyDirection = (): Direction => ({
  outcome: "explore",
  references: [],
  instructions: "",
  next_step: "samples",
});
