"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { fetchRun, listRuns } from "@/flows/app/createDemo/api";
import type { Run } from "@/flows/app/createDemo/types";
import { DemoCard } from "@/flows/app/library/DemoCard";
import { DemoPlayer } from "@/flows/app/library/DemoPlayer";
import { RunProgress } from "@/flows/app/library/JobProgress";
import { isCompleteStatus, isFailedStatus, isRunningStatus } from "@/flows/app/library/format";
import { Segmented } from "@/components/studioControls";
import {
  StudioBody,
  StudioHeader,
  StudioPage,
  StudioPanel,
} from "@/components/studioPage";
import { useAuthStore } from "@/flows/auth/store";

type Filter = "all" | "ready" | "running" | "failed";

function MyDemosInner() {
  const token = useAuthStore((s) => s.token);
  const router = useRouter();
  const params = useSearchParams();
  const watchRun = params.get("run") || params.get("job");
  const [runs, setRuns] = useState<Run[]>([]);
  const [watch, setWatch] = useState<Run | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [playing, setPlaying] = useState<Run | null>(null);

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      const payload = await listRuns(token);
      setRuns(payload.items || []);
    } catch {
      setRuns([]);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!token || !watchRun) {
      setWatch(null);
      return;
    }
    void fetchRun(token, watchRun)
      .then(setWatch)
      .catch(() => setWatch(null));
  }, [token, watchRun]);

  const filtered = useMemo(() => {
    return runs.filter((run) => {
      if (filter === "ready") return isCompleteStatus(run.status);
      if (filter === "running") return isRunningStatus(run.status);
      if (filter === "failed") return isFailedStatus(run.status);
      return true;
    });
  }, [runs, filter]);

  const readyRuns = filtered.filter((r) => isCompleteStatus(r.status));
  const otherRuns = filtered.filter((r) => !isCompleteStatus(r.status));

  return (
    <StudioPage>
      <StudioHeader
        eyebrow="Library"
        title={
          <>
            My <em className="em">demos</em>
          </>
        }
        blurb="Finished films and in-flight runs from the 2.0 generation engine."
        actions={
          <Button asChild>
            <Link href="/createDemo">Create demo</Link>
          </Button>
        }
      />

      <StudioBody className="space-y-8">
        {watchRun ? (
          <RunProgress
            runId={watchRun}
            initial={watch}
            onTerminal={async () => {
              await refresh();
            }}
          />
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-3">
          <Segmented
            value={filter}
            onChange={(v) => setFilter(v as Filter)}
            options={[
              { value: "all", label: "All" },
              { value: "ready", label: "Ready" },
              { value: "running", label: "Running" },
              { value: "failed", label: "Failed" },
            ]}
          />
          <Button type="button" variant="ghost" onClick={() => void refresh()}>
            Refresh
          </Button>
        </div>

        {readyRuns.length > 0 ? (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {readyRuns.map((run) => (
              <DemoCard
                key={run.id}
                run={run}
                onPlay={setPlaying}
                onDeleted={() => void refresh()}
              />
            ))}
          </div>
        ) : null}

        {otherRuns.length > 0 ? (
          <StudioPanel>
            <ul className="divide-y divide-[var(--line)]">
              {otherRuns.map((run) => (
                <li key={run.id} className="flex items-center justify-between gap-3 py-3">
                  <button
                    type="button"
                    className="min-w-0 text-left text-sm font-medium text-ink hover:underline"
                    onClick={() => router.push(`/myDemos?run=${run.id}`)}
                  >
                    {(run.objective as string) || run.id}
                  </button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => router.push(`/timeline?run=${run.id}`)}
                  >
                    Open
                  </Button>
                </li>
              ))}
            </ul>
          </StudioPanel>
        ) : null}

        {filtered.length === 0 ? (
          <StudioPanel>
            <p className="text-sm text-ink">No demos in this filter.</p>
            <Button asChild className="mt-4">
              <Link href="/createDemo">Create a demo</Link>
            </Button>
          </StudioPanel>
        ) : null}
      </StudioBody>

      <DemoPlayer
        run={playing}
        open={Boolean(playing)}
        onOpenChange={(open) => {
          if (!open) setPlaying(null);
        }}
      />
    </StudioPage>
  );
}

export default function MyDemosPage() {
  return (
    <Suspense fallback={<p className="text-sm text-faint">Loading library…</p>}>
      <MyDemosInner />
    </Suspense>
  );
}
