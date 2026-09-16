import type { Run, RunStageJob } from "@/flows/app/createDemo/types";

export const STAGE_LABELS: Record<string, string> = {
  QUEUED: "Queued",
  FEASIBILITY_CHECK: "Checking feasibility",
  DISCOVERING: "Discovering product",
  DISCOVERY: "Discovering product",
  PLAN_READY: "Plan ready",
  PLAN_VALIDATED: "Plan validated",
  PLANNING: "Plan",
  EXPLORATORY_EXECUTION: "Exploring",
  WORKFLOW_VALIDATED: "Workflow validated",
  PRODUCTION_EXECUTION: "Filming",
  EXECUTION: "Film",
  TRACE_READY: "Trace ready",
  PRESENTATION_PLANNED: "Presentation planned",
  NARRATION_READY: "Narration ready",
  NARRATION: "Narrate",
  RENDERING: "Rendering",
  RENDERED: "Rendered",
  RENDER: "Render",
  VIDEO_QA: "Quality check",
  QA_PASSED: "QA passed",
  RUNNING: "Running",
  RETRYING: "Retrying",
  COMPLETE: "Complete",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

/** Map fine-grained run.stage values onto durable stage-job names. */
const LIFECYCLE_TO_JOB: Record<string, string> = {
  QUEUED: "DISCOVERY",
  FEASIBILITY_CHECK: "DISCOVERY",
  DISCOVERING: "DISCOVERY",
  DISCOVERY: "DISCOVERY",
  PLAN_READY: "PLANNING",
  PLAN_VALIDATED: "PLANNING",
  PLANNING: "PLANNING",
  EXPLORATORY_EXECUTION: "EXECUTION",
  WORKFLOW_VALIDATED: "EXECUTION",
  PRODUCTION_EXECUTION: "EXECUTION",
  EXECUTION: "EXECUTION",
  TRACE_READY: "EXECUTION",
  PRESENTATION_PLANNED: "NARRATION",
  NARRATION_READY: "NARRATION",
  NARRATION: "NARRATION",
  RENDERING: "RENDER",
  RENDERED: "RENDER",
  RENDER: "RENDER",
  VIDEO_QA: "VIDEO_QA",
  QA_PASSED: "VIDEO_QA",
};

export type RunChapterStatus = "pending" | "running" | "done" | "failed";

export type RunChapter = {
  id: string;
  title: string;
  detail: string;
  status: RunChapterStatus;
};

const CHAPTERS: {
  id: string;
  title: string;
  stages: string[];
  copy: Record<RunChapterStatus, string>;
}[] = [
  {
    id: "discover",
    title: "Discover",
    stages: ["DISCOVERY"],
    copy: {
      pending: "Inspect the product",
      running: "Discovering relevant pages",
      done: "Discovery ready",
      failed: "Discovery failed",
    },
  },
  {
    id: "plan",
    title: "Plan",
    stages: ["PLANNING"],
    copy: {
      pending: "Scope the objective",
      running: "Planning the walkthrough",
      done: "Plan ready",
      failed: "Planning failed",
    },
  },
  {
    id: "film",
    title: "Film",
    stages: ["EXECUTION"],
    copy: {
      pending: "Capture the product",
      running: "Filming the walkthrough",
      done: "Footage captured",
      failed: "Execution failed",
    },
  },
  {
    id: "narrate",
    title: "Narrate",
    stages: ["NARRATION"],
    copy: {
      pending: "Write voiceover",
      running: "Writing narration",
      done: "Narration ready",
      failed: "Narration failed",
    },
  },
  {
    id: "render",
    title: "Render",
    stages: ["RENDER", "VIDEO_QA"],
    copy: {
      pending: "Compose the cut",
      running: "Rendering and checking quality",
      done: "Film ready",
      failed: "Render or QA failed",
    },
  },
];

function normalizeStatus(status: string | null | undefined) {
  return String(status || "").toUpperCase();
}

function stageStatus(
  stageName: string,
  run: Run,
  stages: RunStageJob[] | undefined,
): RunChapterStatus {
  const runStatus = normalizeStatus(run.status);
  const currentLifecycle = normalizeStatus(run.stage);
  const current = LIFECYCLE_TO_JOB[currentLifecycle] || currentLifecycle;
  const row = stages?.find((s) => normalizeStatus(String(s.stage)) === stageName);
  const rowStatus = normalizeStatus(String(row?.status || ""));

  if (rowStatus === "FAILED" || rowStatus === "ERROR") return "failed";
  if (rowStatus === "COMPLETE" || rowStatus === "SUCCEEDED" || rowStatus === "DONE") return "done";
  if (rowStatus === "SKIPPED") return "done";
  if (rowStatus === "RUNNING" || rowStatus === "ACTIVE") return "running";

  if (runStatus === "FAILED" && current === stageName) return "failed";
  if (runStatus === "COMPLETE") return "done";
  if (
    current === stageName &&
    (runStatus === "RUNNING" || runStatus === "QUEUED" || runStatus === "RETRYING")
  ) {
    return runStatus === "QUEUED" ? "pending" : "running";
  }

  const order = ["DISCOVERY", "PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA"];
  const currentIdx = order.indexOf(current);
  const stageIdx = order.indexOf(stageName);
  if (currentIdx > stageIdx && currentIdx >= 0) return "done";
  return "pending";
}

export function runChapters(run: Run, stages?: RunStageJob[]): RunChapter[] {
  return CHAPTERS.map((chapter) => {
    const statuses = chapter.stages.map((s) => stageStatus(s, run, stages));
    let status: RunChapterStatus = "pending";
    if (statuses.includes("failed")) status = "failed";
    else if (statuses.includes("running")) status = "running";
    else if (statuses.every((s) => s === "done")) status = "done";
    else if (statuses.includes("done")) status = "running";
    return {
      id: chapter.id,
      title: chapter.title,
      detail: chapter.copy[status],
      status,
    };
  });
}

export function stageLabel(stage: string | null | undefined) {
  if (!stage) return "Waiting";
  return STAGE_LABELS[stage] || STAGE_LABELS[normalizeStatus(stage)] || stage.replaceAll("_", " ");
}

export function runTitle(run: Run) {
  const objective = (run.objective || "").trim();
  if (objective) return objective.length > 72 ? `${objective.slice(0, 69)}…` : objective;
  if (run.url) return String(run.url);
  return `Run ${run.id.slice(0, 8)}`;
}

export function formatWhen(iso?: string | null) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function formatDuration(seconds?: number | null) {
  if (seconds == null || Number.isNaN(seconds)) return "";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

export function isTerminalStatus(status: string | null | undefined) {
  const s = normalizeStatus(status);
  return s === "COMPLETE" || s === "FAILED" || s === "CANCELLED" || s === "SUCCEEDED";
}

export function isCompleteStatus(status: string | null | undefined) {
  const s = normalizeStatus(status);
  return s === "COMPLETE" || s === "SUCCEEDED";
}

export function isRunningStatus(status: string | null | undefined) {
  const s = normalizeStatus(status);
  if (!s) return false;
  if (s === "RUNNING" || s === "QUEUED" || s === "RETRYING") return true;
  // Treat any non-terminal status as in-flight (covers odd intermediate writes).
  return !isTerminalStatus(s);
}

export function isFailedStatus(status: string | null | undefined) {
  const s = normalizeStatus(status);
  return s === "FAILED" || s === "CANCELLED";
}
