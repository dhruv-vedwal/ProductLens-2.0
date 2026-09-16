"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Play } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { StudioPanel } from "@/components/studioPage";
import {
  cancelRun,
  deleteRun,
  fetchRun,
  fetchRunStages,
  posterRunUrl,
  resumeRun,
  retryRun,
  streamRunUrl,
} from "@/flows/app/createDemo/api";
import type { Run, RunStageJob } from "@/flows/app/createDemo/types";
import { DemoPlayer } from "@/flows/app/library/DemoPlayer";
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

function chapterMark(status: string, index: number) {
  if (status === "done") return "✓";
  if (status === "failed") return "!";
  return String(index + 1);
}

export function RunProgress({
  runId,
  initial,
  onTerminal,
}: {
  runId: string;
  initial?: Run | null;
  onTerminal?: (run: Run) => void;
}) {
  const token = useAuthStore((s) => s.token);
  const [run, setRun] = useState<Run | null>(initial ?? null);
  const [stages, setStages] = useState<RunStageJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    if (!token || !runId) return;
    let cancelled = false;
    void fetchRun(token, runId)
      .then((r) => {
        if (!cancelled) setRun(r);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not load run");
      });
    void fetchRunStages(token, runId)
      .then((payload) => {
        if (!cancelled) setStages(payload.stages || []);
      })
      .catch(() => undefined);

    const unsubscribe = subscribeRunEvents(runId, token, (payload) => {
      setRun((prev) => ({
        ...(prev || { id: runId, status: payload.status }),
        id: runId,
        status: payload.status,
        stage: payload.stage,
        error_code: payload.error_code,
        updated_at: payload.updated_at || prev?.updated_at,
      }));
      if (payload.stages) setStages(payload.stages as RunStageJob[]);
      if (
        ["COMPLETE", "FAILED", "CANCELLED"].includes(String(payload.status).toUpperCase())
      ) {
        void fetchRun(token, runId).then((fresh) => {
          setRun(fresh);
          onTerminal?.(fresh);
        });
      }
    });

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [token, runId, onTerminal]);

  if (!run) {
    return <p className="text-sm text-faint">Loading run…</p>;
  }

  const chapters = runChapters(run, stages);
  const complete = isCompleteStatus(run.status);
  const failed = isFailedStatus(run.status);
  const running = isRunningStatus(run.status);

  return (
    <StudioPanel className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] font-medium tracking-[0.1em] text-faint uppercase">
            Live run
          </p>
          <h2 className="mt-1 text-lg font-medium tracking-[-0.02em] text-ink">
            {runTitle(run)}
          </h2>
          <p className="mt-1 text-[12px] text-faint">
            {stageLabel(run.stage)} · {String(run.status).toLowerCase()}
            {run.updated_at ? ` · ${formatWhen(run.updated_at)}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {complete ? (
            <Button type="button" onClick={() => setPlaying(true)}>
              <Play className="size-3.5" /> Play
            </Button>
          ) : null}
          <Button type="button" variant="secondary" asChild>
            <Link href={`/timeline?run=${run.id}`}>Details</Link>
          </Button>
        </div>
      </div>

      {error ? (
        <Alert variant="destructive">
          <AlertTitle>Error</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      {run.error_code ? (
        <Alert variant="destructive">
          <AlertTitle>Run failed</AlertTitle>
          <AlertDescription>{String(run.error_code)}</AlertDescription>
        </Alert>
      ) : null}

      <ol className="space-y-2">
        {chapters.map((chapter, i) => (
          <li
            key={chapter.id}
            className={cn(
              "flex items-start gap-3 rounded-[calc(var(--radius)-4px)] px-2 py-2",
              chapter.status === "running" && "bg-[var(--bg)]",
            )}
          >
            <span
              className={cn(
                "flex size-7 shrink-0 items-center justify-center rounded-full text-[11px] font-medium",
                chapter.status === "done" && "bg-ink text-[var(--bg)]",
                chapter.status === "running" && "bg-record text-white",
                chapter.status === "failed" && "bg-record/15 text-record",
                chapter.status === "pending" && "border border-[var(--line)] text-faint",
              )}
            >
              {chapterMark(chapter.status, i)}
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-medium text-ink">{chapter.title}</span>
              <span className="block text-[12px] text-faint">{chapter.detail}</span>
            </span>
          </li>
        ))}
      </ol>

      <div className="flex flex-wrap gap-2 border-t border-[var(--line)] pt-4">
        {failed ? (
          <Button
            type="button"
            disabled={busy || !token}
            onClick={async () => {
              if (!token) return;
              setBusy(true);
              setError(null);
              try {
                const next = await retryRun(token, run.id, {});
                window.location.href = `/timeline?run=${next.run_id}`;
              } catch (err) {
                setError(err instanceof Error ? err.message : "Retry failed");
              } finally {
                setBusy(false);
              }
            }}
          >
            Retry
          </Button>
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
                  const fresh = await fetchRun(token, run.id);
                  setRun(fresh);
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
        <Button
          type="button"
          variant="ghost"
          disabled={busy || !token}
          onClick={async () => {
            if (!token) return;
            setBusy(true);
            try {
              await deleteRun(token, run.id);
              window.location.href = "/myDemos";
            } catch (err) {
              setError(err instanceof Error ? err.message : "Delete failed");
              setBusy(false);
            }
          }}
        >
          Delete
        </Button>
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

      <DemoPlayer run={playing ? run : null} open={playing} onOpenChange={setPlaying} />
    </StudioPanel>
  );
}

/** @deprecated Use RunProgress */
export const JobProgress = RunProgress;
