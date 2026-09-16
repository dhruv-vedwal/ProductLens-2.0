"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  StudioBody,
  StudioHeader,
  StudioPage,
  StudioPanel,
  StudioSectionTitle,
  StatusPill,
} from "@/components/studioPage";
import {
  cancelRun,
  fetchRun,
  fetchRunDetails,
  fetchRunStages,
  posterRunUrl,
  resumeRun,
  retryRun,
  streamRunUrl,
} from "@/flows/app/createDemo/api";
import type { Run, RunDetails, RunStageJob } from "@/flows/app/createDemo/types";
import {
  formatWhen,
  isCompleteStatus,
  isFailedStatus,
  isRunningStatus,
  runChapters,
  runTitle,
  stageLabel,
} from "@/flows/app/library/format";
import { useAuthStore } from "@/flows/auth/store";
import { subscribeRunEvents } from "@/utilities/http";
import { cn } from "@/lib/utils";

const RETRY_STAGES = ["PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA"] as const;

export default function TimelineInner() {
  const token = useAuthStore((s) => s.token);
  const params = useSearchParams();
  const runId = params.get("run") || params.get("job") || "";
  const [run, setRun] = useState<Run | null>(null);
  const [details, setDetails] = useState<RunDetails | null>(null);
  const [stages, setStages] = useState<RunStageJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [retryStage, setRetryStage] = useState<(typeof RETRY_STAGES)[number] | "">("");

  useEffect(() => {
    if (!token || !runId) return;
    let cancelled = false;

    const load = async () => {
      try {
        const [r, d, s] = await Promise.all([
          fetchRun(token, runId),
          fetchRunDetails(token, runId).catch(() => null),
          fetchRunStages(token, runId).catch(() => null),
        ]);
        if (cancelled) return;
        setRun(r);
        setDetails(d);
        setStages(s?.stages || []);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Could not load run");
        }
      }
    };

    void load();

    const unsubscribe = subscribeRunEvents(runId, token, (payload) => {
      setRun((prev) => ({
        ...(prev || { id: runId, status: payload.status }),
        id: runId,
        status: payload.status,
        stage: payload.stage,
        error_code: payload.error_code,
        updated_at: payload.updated_at || prev?.updated_at,
        objective: prev?.objective,
        url: prev?.url,
      }));
      if (payload.stages) setStages(payload.stages as RunStageJob[]);
      if (["COMPLETE", "FAILED", "CANCELLED"].includes(String(payload.status).toUpperCase())) {
        void fetchRunDetails(token, runId)
          .then((d) => {
            if (!cancelled) setDetails(d);
          })
          .catch(() => undefined);
      }
    });

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [token, runId]);

  const chapters = run ? runChapters(run, stages) : [];
  const complete = run ? isCompleteStatus(run.status) : false;
  const failed = run ? isFailedStatus(run.status) : false;
  const running = run ? isRunningStatus(run.status) : false;
  const qa = details?.quality_reports?.[details.quality_reports.length - 1];

  return (
    <StudioPage>
      <StudioHeader
        eyebrow="Run"
        title={
          <>
            {run ? (
              <>
                <em className="em">{runTitle(run)}</em>
              </>
            ) : (
              <>
                Run <em className="em">detail</em>
              </>
            )}
          </>
        }
        blurb="Live stages over SSE, evidence details, playback, and retry — not scene regenerate."
        actions={
          <Button type="button" variant="ghost" asChild>
            <Link href={runId ? `/myDemos?run=${runId}` : "/myDemos"}>Back to library</Link>
          </Button>
        }
      />
      <StudioBody className="space-y-6">
        {!runId ? (
          <Alert>
            <AlertDescription>Open a run from My demos to inspect progress.</AlertDescription>
          </Alert>
        ) : null}
        {error ? (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}

        {run ? (
          <div className="grid gap-6 lg:grid-cols-[1.1fr_0.9fr]">
            <StudioPanel className="space-y-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-ink">{runTitle(run)}</p>
                  <p className="mt-1 text-[12px] text-faint">
                    {stageLabel(run.stage)} · updated {formatWhen(run.updated_at)}
                  </p>
                </div>
                <StatusPill status={String(run.status)} />
              </div>

              <ol className="space-y-2">
                {chapters.map((chapter, i) => (
                  <li key={chapter.id} className="flex gap-3">
                    <span
                      className={cn(
                        "flex size-7 shrink-0 items-center justify-center rounded-full text-[11px] font-medium",
                        chapter.status === "done" && "bg-ink text-[var(--bg)]",
                        chapter.status === "running" && "bg-record text-white",
                        chapter.status === "failed" && "bg-record/15 text-record",
                        chapter.status === "pending" && "border border-[var(--line)] text-faint",
                      )}
                    >
                      {chapter.status === "done" ? "✓" : i + 1}
                    </span>
                    <span>
                      <span className="block text-sm font-medium text-ink">{chapter.title}</span>
                      <span className="block text-[12px] text-faint">{chapter.detail}</span>
                    </span>
                  </li>
                ))}
              </ol>

              <div className="flex flex-wrap gap-2 border-t border-[var(--line)] pt-4">
                {failed ? (
                  <>
                    <select
                      className="h-10 rounded-lg border border-[var(--line)] bg-[var(--bg)] px-3 text-sm"
                      value={retryStage}
                      onChange={(e) =>
                        setRetryStage(e.target.value as (typeof RETRY_STAGES)[number] | "")
                      }
                    >
                      <option value="">Retry from (auto)</option>
                      {RETRY_STAGES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                    <Button
                      type="button"
                      disabled={busy || !token}
                      onClick={async () => {
                        if (!token) return;
                        setBusy(true);
                        setError(null);
                        try {
                          const next = await retryRun(token, run.id, {
                            retry_from_stage: retryStage || null,
                          });
                          window.location.href = `/timeline?run=${next.run_id}`;
                        } catch (err) {
                          setError(err instanceof Error ? err.message : "Retry failed");
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      Retry run
                    </Button>
                  </>
                ) : null}
                {running ? (
                  <>
                    <Button
                      type="button"
                      variant="secondary"
                      disabled={busy || !token}
                      onClick={async () => {
                        if (!token) return;
                        setBusy(true);
                        try {
                          await resumeRun(token, run.id);
                          setRun(await fetchRun(token, run.id));
                        } catch (err) {
                          setError(err instanceof Error ? err.message : "Resume failed");
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      Resume
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      disabled={busy || !token}
                      onClick={async () => {
                        if (!token) return;
                        setBusy(true);
                        try {
                          await cancelRun(token, run.id);
                          setRun(await fetchRun(token, run.id));
                        } catch (err) {
                          setError(err instanceof Error ? err.message : "Cancel failed");
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      Cancel
                    </Button>
                  </>
                ) : null}
              </div>

              {complete && token ? (
                <div className="overflow-hidden rounded-[calc(var(--radius)-2px)] border border-[var(--line)] bg-black">
                  <video
                    className="aspect-video w-full"
                    src={streamRunUrl(token, run.id)}
                    poster={posterRunUrl(token, run.id)}
                    controls
                    playsInline
                  />
                </div>
              ) : null}
            </StudioPanel>

            <div className="space-y-6">
              <StudioPanel>
                <StudioSectionTitle title="Request" />
                <dl className="space-y-3 text-sm">
                  <div>
                    <dt className="text-[11px] tracking-[0.08em] text-faint uppercase">URL</dt>
                    <dd className="mt-1 break-all text-ink">
                      {String(details?.request?.url || run.url || "—")}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[11px] tracking-[0.08em] text-faint uppercase">
                      Objective
                    </dt>
                    <dd className="mt-1 text-ink">
                      {String(details?.request?.objective || run.objective || "—")}
                    </dd>
                  </div>
                  {details?.retry_of ? (
                    <div>
                      <dt className="text-[11px] tracking-[0.08em] text-faint uppercase">
                        Retry of
                      </dt>
                      <dd className="mt-1">
                        <Link
                          href={`/timeline?run=${details.retry_of}`}
                          className="text-soft underline-offset-2 hover:underline"
                        >
                          {details.retry_of}
                        </Link>
                      </dd>
                    </div>
                  ) : null}
                </dl>
              </StudioPanel>

              <StudioPanel>
                <StudioSectionTitle title="QA / details" hint="From /runs/{id}/details" />
                {qa ? (
                  <pre className="max-h-64 overflow-auto rounded-lg bg-[var(--bg)] p-3 text-[11px] leading-relaxed text-soft">
                    {JSON.stringify(qa, null, 2)}
                  </pre>
                ) : (
                  <p className="text-sm text-faint">
                    {running
                      ? "Quality reports appear when the run finishes."
                      : "No quality report yet."}
                  </p>
                )}
                {Array.isArray(details?.attempts) && details!.attempts!.length > 0 ? (
                  <ul className="mt-4 divide-y divide-[var(--line)] text-[12px]">
                    {details!.attempts!.map((attempt, idx) => (
                      <li key={idx} className="flex justify-between gap-2 py-2">
                        <span className="text-ink">
                          {String(attempt.stage || "stage")} · {String(attempt.status || "")}
                        </span>
                        <span className="text-faint">
                          {attempt.failure_code ? String(attempt.failure_code) : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </StudioPanel>
            </div>
          </div>
        ) : runId ? (
          <p className="text-sm text-faint">Loading run…</p>
        ) : null}
      </StudioBody>
    </StudioPage>
  );
}
