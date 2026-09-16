"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { fetchHomeStats } from "@/flows/app/createDemo/api";
import type { HomeStats, Run } from "@/flows/app/createDemo/types";
import { DemoCard } from "@/flows/app/library/DemoCard";
import { DemoPlayer } from "@/flows/app/library/DemoPlayer";
import { formatWhen, runTitle, stageLabel } from "@/flows/app/library/format";
import {
  MetricRow,
  StatusPill,
  StudioBody,
  StudioHeader,
  StudioPage,
  StudioPanel,
  StudioSectionTitle,
} from "@/components/studioPage";
import { useAuthStore } from "@/flows/auth/store";

const PIPELINE = [
  { name: "Discover", detail: "Inspect relevant product pages" },
  { name: "Plan", detail: "Objective → validated walkthrough" },
  { name: "Film", detail: "Evidence-backed capture" },
  { name: "Narrate", detail: "Voiceover timed to the take" },
  { name: "Render", detail: "Captions, polish, QA export" },
];

export default function HomePage() {
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);
  const [stats, setStats] = useState<HomeStats | null>(null);
  const [playing, setPlaying] = useState<Run | null>(null);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    const load = () => {
      fetchHomeStats(token)
        .then((next) => {
          if (!cancelled) setStats(next);
        })
        .catch(() => {
          if (!cancelled) setStats(null);
        });
    };
    load();
    const id = window.setInterval(load, 8_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [token]);

  const empty = !stats || (stats.demosCreated === 0 && stats.recentRuns.length === 0);
  const greeting = user?.displayName?.split(" ")[0] || "there";

  return (
    <StudioPage>
      <StudioHeader
        eyebrow="Workspace"
        title={
          <>
            Welcome back, <em className="em">{greeting}</em>
          </>
        }
        blurb="Recent films and live runs — create when you are ready."
        actions={
          <Button asChild size="lg">
            <Link href="/createDemo" data-tour="home-cta-create">
              Create demo
            </Link>
          </Button>
        }
      />

      <StudioBody className="space-y-10">
        <MetricRow
          items={[
            { label: "Demos", value: stats?.demosCreated ?? 0 },
            { label: "Running", value: stats?.runsRunning ?? 0 },
            { label: "Failed", value: stats?.runsFailed ?? 0 },
          ]}
        />

        {empty ? (
          <div className="grid gap-6 lg:grid-cols-[1.1fr_0.9fr]">
            <StudioPanel>
              <p className="text-sm font-medium text-ink">Nothing here yet</p>
              <p className="mt-2 max-w-lg text-sm leading-relaxed text-soft">
                Point ProductLens at a product URL and describe the story. We plan, film,
                narrate, and finish a shareable cut.
              </p>
              <Button asChild size="lg" className="mt-6">
                <Link href="/createDemo" data-tour="home-cta-create">
                  Create your first demo
                </Link>
              </Button>
            </StudioPanel>
            <StudioPanel>
              <StudioSectionTitle title="How a demo is made" hint="Discovery through QA" />
              <ol className="space-y-3">
                {PIPELINE.map((step, i) => (
                  <li key={step.name} className="flex gap-3">
                    <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-ink text-[11px] font-medium text-[var(--bg)]">
                      {i + 1}
                    </span>
                    <span>
                      <span className="block text-sm font-medium text-ink">{step.name}</span>
                      <span className="block text-[12px] text-faint">{step.detail}</span>
                    </span>
                  </li>
                ))}
              </ol>
            </StudioPanel>
          </div>
        ) : (
          <div className="space-y-8">
            {(stats?.recentComplete ?? []).length > 0 ? (
              <div>
                <StudioSectionTitle
                  title="Recent demos"
                  action={
                    <Link
                      href="/myDemos"
                      className="text-[12px] text-soft hover:text-ink hover:underline"
                    >
                      View library
                    </Link>
                  }
                />
                <div className="flex gap-3 overflow-x-auto pb-2">
                  {(stats?.recentComplete ?? []).map((run) => (
                    <DemoCard
                      key={run.id}
                      run={run}
                      compact
                      onPlay={setPlaying}
                      onDeleted={() => {
                        if (!token) return;
                        fetchHomeStats(token)
                          .then(setStats)
                          .catch(() => undefined);
                      }}
                    />
                  ))}
                </div>
              </div>
            ) : null}

            <StudioPanel>
              <StudioSectionTitle
                title="Recent activity"
                hint="Live runs refresh every few seconds"
                action={
                  <Link
                    href="/myDemos"
                    className="text-[12px] text-soft hover:text-ink hover:underline"
                  >
                    Open My demos
                  </Link>
                }
              />
              <ul className="divide-y divide-[var(--line)]">
                {(stats?.recentRuns ?? []).map((run) => (
                  <li
                    key={run.id}
                    className="flex items-start justify-between gap-3 py-3 first:pt-0 last:pb-0"
                  >
                    <div className="min-w-0">
                      <Link
                        href={`/timeline?run=${run.id}`}
                        className="text-sm font-medium text-ink hover:underline"
                      >
                        {runTitle(run)}
                      </Link>
                      <p className="mt-0.5 truncate text-[12px] text-faint">
                        {stageLabel(run.stage)}
                        {" · "}
                        {formatWhen(run.created_at)}
                      </p>
                    </div>
                    <StatusPill status={String(run.status)} />
                  </li>
                ))}
                {(stats?.recentRuns.length ?? 0) === 0 ? (
                  <li className="py-2 text-sm text-faint">No recent runs.</li>
                ) : null}
              </ul>
            </StudioPanel>
          </div>
        )}
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
