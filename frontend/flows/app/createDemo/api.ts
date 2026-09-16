import { http, httpDownload, runPosterUrl, runStreamUrl, runVideoUrl } from "@/utilities/http";
import type {
  CreateRunBody,
  CreateRunResponse,
  CredentialItem,
  HomeStats,
  ProjectSummary,
  Readiness,
  Run,
  RunDetails,
  RunStagesResponse,
  UnderstandingPreview,
} from "@/flows/app/createDemo/types";
import {
  isCompleteStatus,
  isFailedStatus,
  isRunningStatus,
} from "@/flows/app/library/format";

export function listRuns(token: string, limit = 50, offset = 0) {
  return http<{ items: Run[]; limit: number; offset: number }>(
    `/runs?limit=${limit}&offset=${offset}`,
    { token },
  );
}

export function fetchRun(token: string, runId: string) {
  return http<Run>(`/runs/${runId}`, { token });
}

export function fetchRunDetails(token: string, runId: string) {
  return http<RunDetails>(`/runs/${runId}/details`, { token });
}

export function fetchRunStages(token: string, runId: string) {
  return http<RunStagesResponse>(`/runs/${runId}/stages`, { token });
}

export function createRun(token: string, body: CreateRunBody) {
  return http<CreateRunResponse>("/runs", {
    method: "POST",
    token,
    body: JSON.stringify(body),
  });
}

export function previewUnderstanding(
  token: string,
  body: {
    url: string;
    prompt?: string;
    audience?: string | null;
    max_pages?: number;
    use_stagehand?: boolean;
    force_refresh?: boolean;
  },
) {
  return http<UnderstandingPreview>("/understanding/preview", {
    method: "POST",
    token,
    body: JSON.stringify(body),
  });
}

export function retryRun(
  token: string,
  runId: string,
  body: {
    retry_from_stage?:
      | "PLANNING"
      | "EXECUTION"
      | "NARRATION"
      | "RENDER"
      | "VIDEO_QA"
      | null;
    allow_external_side_effects?: boolean;
    allow_isolated_record_creation?: boolean;
    cloud_discovery?: boolean | null;
    credential_reference?: string | null;
  } = {},
) {
  return http<CreateRunResponse>(`/runs/${runId}/retry`, {
    method: "POST",
    token,
    body: JSON.stringify(body),
  });
}

export function resumeRun(token: string, runId: string) {
  return http<{ run_id: string; job_id: string; status: string }>(`/runs/${runId}/resume`, {
    method: "POST",
    token,
  });
}

export function cancelRun(token: string, runId: string) {
  return http<{ run_id: string; status: string }>(`/runs/${runId}/cancel`, {
    method: "POST",
    token,
  });
}

export function deleteRun(token: string, runId: string) {
  return http<void>(`/runs/${runId}`, { method: "DELETE", token });
}

export function downloadRunVideo(token: string, runId: string, filename?: string) {
  return httpDownload(`/runs/${runId}/video`, token, filename || `productlens-${runId}.mp4`);
}

export function streamRunUrl(token: string, runId: string) {
  return runStreamUrl(runId, token);
}

export function posterRunUrl(token: string, runId: string) {
  return runPosterUrl(runId, token);
}

export function videoRunUrl(token: string, runId: string) {
  return runVideoUrl(runId, token);
}

export async function fetchHomeStats(token: string): Promise<HomeStats> {
  const { items } = await listRuns(token, 50, 0);
  const running = items.filter((r) => isRunningStatus(r.status)).length;
  const failed = items.filter((r) => isFailedStatus(r.status)).length;
  const complete = items.filter((r) => isCompleteStatus(r.status));
  return {
    demosCreated: complete.length,
    runsRunning: running,
    runsFailed: failed,
    recentRuns: items.slice(0, 8),
    recentComplete: complete.slice(0, 8),
  };
}

export function listProjects(token: string) {
  return http<ProjectSummary[]>("/projects", { token });
}

export function createProject(token: string, name: string) {
  return http<ProjectSummary>("/projects", {
    method: "POST",
    token,
    body: JSON.stringify({ name }),
  });
}

export function renameProject(token: string, projectId: string, name: string) {
  return http<ProjectSummary>(`/projects/${projectId}`, {
    method: "PATCH",
    token,
    body: JSON.stringify({ name }),
  });
}

export function listCredentials(token: string) {
  return http<CredentialItem[]>("/credentials", { token });
}

export function createCredential(
  token: string,
  body: { name: string; username: string; password: string; project_id?: string | null },
) {
  return http<CredentialItem>("/credentials", {
    method: "POST",
    token,
    body: JSON.stringify(body),
  });
}

export function deleteCredential(token: string, credentialId: string) {
  return http<void>(`/credentials/${credentialId}`, { method: "DELETE", token });
}

export function fetchReadiness() {
  return http<Readiness>("/readiness");
}

export function fetchProviders(token: string) {
  return http<Array<Record<string, unknown>>>("/providers", { token });
}
