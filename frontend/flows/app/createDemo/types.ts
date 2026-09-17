export type RunStageJob = {
  id?: string;
  stage?: string;
  status?: string;
  attempt?: number;
  failure_code?: string | null;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
};

export type Run = {
  id: string;
  request_id?: string;
  status: string;
  stage?: string | null;
  error_code?: string | null;
  created_at?: string;
  updated_at?: string;
  url?: string | null;
  objective?: string | null;
  project_id?: string | null;
  artifact_root?: string | null;
  [key: string]: unknown;
};

export type RunDetails = {
  run: Run;
  request?: Record<string, unknown>;
  job?: Record<string, unknown> | null;
  stage_jobs?: RunStageJob[];
  plans?: unknown[];
  workflow_steps?: unknown[];
  quality_reports?: unknown[];
  narration_scripts?: unknown[];
  attempts?: Array<Record<string, unknown>>;
  retry_of?: string | null;
  retries?: string[];
  artifact_documents?: unknown[];
  [key: string]: unknown;
};

export type RunStagesResponse = {
  run: Run;
  job?: Record<string, unknown> | null;
  stages: RunStageJob[];
};

export type CreateRunBody = {
  url: string;
  objective: string;
  audience?: string;
  project_id?: string | null;
  credential_reference?: string | null;
  target_duration_seconds?: number;
  max_pages?: number;
  render?: boolean;
  cloud_discovery?: boolean | null;
  allow_external_side_effects?: boolean;
  allow_isolated_record_creation?: boolean;
  request_id?: string | null;
  presentation?: PresentationOptions;
};

export type PresentationOptions = {
  include_audio?: boolean;
  narration_style?: "teach" | "showcase";
  pace?: number;
  browser_zoom_percent?: number;
  subtitles_enabled?: boolean;
  subtitle_style?: "minimal" | "apple" | "youtube";
  subtitle_position?: "top" | "bottom";
  subtitle_font_size?: "sm" | "md" | "lg";
  intro_template?: string;
  studio_polish?: boolean;
  cursor_style?: string;
  highlight_style?: string;
  click_zoom?: boolean;
  export_aspect?: "16:9" | "9:16" | "1:1";
  export_resolution?: "720" | "1080" | "1440" | "2160";
  language?: string;
  accent?: string;
};

export type CreateRunResponse = {
  run_id: string;
  status: string;
};

export type UnderstandingPreview = {
  url?: string;
  suggested_prompt?: string;
  objective?: {
    demo_type?: string;
    primary_entity?: string | null;
    [key: string]: unknown;
  };
  relevant_areas: string[];
  evidence_refs: string[];
  pages_inspected: string[];
  pages?: unknown[];
  audience?: string | null;
  [key: string]: unknown;
};

export type HomeStats = {
  demosCreated: number;
  runsRunning: number;
  runsFailed: number;
  recentRuns: Run[];
  recentComplete: Run[];
};

export type ProjectSummary = {
  id: string;
  name: string;
  created_at?: string;
  request_count?: number;
  [key: string]: unknown;
};

export type CredentialItem = {
  id?: string;
  name: string;
  reference: string;
  available: boolean;
  source?: string;
  project_id?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type Readiness = {
  database_dialect?: string;
  database_ready?: boolean;
  providers?: Record<string, boolean>;
  fixture_generation_ready?: boolean;
  live_generation_ready?: boolean;
  narration_ready?: boolean;
  caption_only?: boolean;
  cloud_browser_ready?: boolean;
  worker_mode?: string;
  queue_ready?: boolean;
};

/** Kept for LookStep / ProductStep UI craft that still references these. */
export type DemoMode = "guided" | "goal" | "feature" | "autonomous";
export type NarrationStyle = "teach" | "showcase";
export type ScopeMode = "product" | "module" | "feature" | "task" | "page_region";
export type DemoDepth = "overview" | "standard" | "deep";

export type GuidedStep = {
  action: string;
  url?: string | null;
  selector?: string | null;
  value?: string | null;
  text?: string | null;
  ms?: number | null;
  note?: string | null;
  optional?: boolean;
  narration?: string | null;
  chapter?: string | null;
  [key: string]: unknown;
};

export type VoiceOption = {
  id: string;
  label: string;
  language: string;
  accent: string;
};
