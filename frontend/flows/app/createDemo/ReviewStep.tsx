"use client";

import { INTRO_TEMPLATES } from "@/flows/app/createDemo/introTemplates";
import type { CreateDemoState } from "@/flows/app/createDemo/useCreateDemoState";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function textList(value: unknown): string[] {
  if (!Array.isArray(value)) return value == null ? [] : [String(value)];
  return value
    .map((item) => {
      if (typeof item === "string") return item;
      const row = asRecord(item);
      return String(row.title ?? row.name ?? row.label ?? row.outcome ?? row.id ?? "");
    })
    .filter(Boolean);
}

function coverageRows(value: unknown): { label: string; status: string }[] {
  if (Array.isArray(value)) {
    return value.map((item, index) => {
      const row = asRecord(item);
      return {
        label: String(row.label ?? row.name ?? row.workflow ?? `Area ${index + 1}`),
        status: String(row.status ?? row.coverage ?? row.completeness ?? "available"),
      };
    });
  }
  return Object.entries(asRecord(value)).map(([label, raw]) => {
    const row = asRecord(raw);
    return {
      label,
      status:
        typeof raw === "string" || typeof raw === "number"
          ? String(raw)
          : String(row.status ?? row.coverage ?? row.completeness ?? "available"),
    };
  });
}

function ambiguityText(value: unknown): string {
  if (typeof value === "string") return value;
  const row = asRecord(value);
  return String(row.prompt ?? row.message ?? row.description ?? row.field ?? "Scope needs clarification");
}

function beatLabel(step: {
  chapter?: string | null;
  capabilityType?: string | null;
  action?: string;
  narration?: string | null;
  compressed?: boolean | null;
}) {
  if (step.chapter?.trim()) return step.chapter.trim();
  if (step.capabilityType) return String(step.capabilityType);
  if (step.narration?.trim()) {
    const n = step.narration.trim();
    return n.length > 42 ? `${n.slice(0, 39)}…` : n;
  }
  return step.action || "Beat";
}

export function ReviewStep({ s }: { s: CreateDemoState }) {
  const brief = s.goalText.trim();
  const project = s.projectName.trim() || "Demo";
  const url = s.baseUrl.trim() || "—";
  const lookLabel = s.studioPolish
    ? (INTRO_TEMPLATES.find((t) => t.id === s.introTemplate)?.label ?? s.introTemplate)
    : "Full-bleed";
  const lengthLabel =
    s.targetDurationMinutes < 1
      ? `${Math.round(s.targetDurationMinutes * 60)}s`
      : `${s.targetDurationMinutes}m`;
  const accessParts: string[] = [];
  if (s.connectionBadge.tone === "ok") accessParts.push("Connected");
  else if (s.browserSessionId) accessParts.push("Saved login");
  if (s.loginUsername.trim()) accessParts.push("Password");
  const access = accessParts.length > 0 ? accessParts.join(" · ") : "No sign-in";
  const intent = asRecord(s.filmIntent);
  const resolvedScope = asRecord(intent.resolvedScope ?? intent.scope);
  const selectedModules = textList(
    intent.selectedModules ?? resolvedScope.modules ?? intent.modules,
  );
  const selectedWorkflows = textList(
    intent.selectedWorkflows ?? resolvedScope.workflows ?? intent.workflows,
  );
  const selectedOutcomes = textList(
    intent.selectedOutcomes ?? resolvedScope.outcomes ?? intent.outcomes,
  );
  const coverage = coverageRows(intent.coverage ?? resolvedScope.coverage);
  const blockingAmbiguities = (
    Array.isArray(intent.ambiguities) ? intent.ambiguities : []
  ).filter((item) => {
    const row = asRecord(item);
    return row.blocking !== false && !["resolved", "dismissed"].includes(String(row.status ?? ""));
  });
  const scopeLabel =
    String(
      resolvedScope.label ??
        resolvedScope.title ??
        resolvedScope.mode ??
        intent.scopeMode ??
        s.modeLabel,
    ) || s.modeLabel;

  const facts = [
    {
      label: "Narration",
      value: s.narrationStyle === "teach" ? "Teach" : "Showcase",
    },
    { label: "Voice", value: s.accentLabel },
    { label: "Length", value: lengthLabel },
    { label: "Intro", value: lookLabel },
    {
      label: "Captions",
      value: s.subtitlesEnabled ? s.subtitleStyle : "Off",
    },
    { label: "Export", value: `${s.exportAspect} · ${s.exportResolution}` },
    { label: "Access", value: access },
  ];

  const chapters = (() => {
    const seen = new Set<string>();
    const rows: { title: string; compressed: boolean; count: number }[] = [];
    for (const beat of s.previewBeats) {
      const title = beatLabel(beat);
      if (seen.has(title)) {
        const hit = rows.find((r) => r.title === title);
        if (hit) hit.count += 1;
        continue;
      }
      seen.add(title);
      rows.push({
        title,
        compressed: Boolean(beat.compressed),
        count: 1,
      });
    }
    return rows;
  })();

  return (
    <div className="max-w-xl space-y-8">
      <div>
        <p className="text-[11px] font-medium tracking-[0.14em] text-faint uppercase">About to film</p>
        <h3 className="mt-2 text-[1.35rem] leading-snug tracking-[-0.025em] text-ink">
          {project}
        </h3>
        <p className="mt-1.5 break-all text-[13px] text-soft">{url}</p>
      </div>

      <div>
        <p className="text-[12px] font-medium text-faint">Brief</p>
        <p className="mt-2 text-[15px] leading-relaxed tracking-[-0.01em] text-ink">
          {brief
            ? brief.length > 220
              ? `${brief.slice(0, 217)}…`
              : brief
            : "We’ll follow your suggestions and learn the whole product first."}
        </p>
        {s.context.audience ? (
          <p className="mt-3 text-[13px] text-soft">For {s.context.audience}</p>
        ) : null}
      </div>

      {s.coverageWarning ? (
        <p className="rounded-[var(--radius)] border border-[color-mix(in_srgb,var(--record)_35%,var(--line))] bg-[color-mix(in_srgb,var(--record)_8%,transparent)] px-3 py-2.5 text-[13px] leading-relaxed text-ink">
          {s.coverageWarning}
        </p>
      ) : null}

      <p className="text-[12px] leading-relaxed text-faint">
        This job will re-explore the product from scratch (application maps are not reused yet).
      </p>

      {s.filmSummary ? (
        <p className="text-[13px] leading-relaxed text-soft">{s.filmSummary}</p>
      ) : null}

      <div className="space-y-4 rounded-[var(--radius)] border border-[var(--line)] bg-[var(--bg-elev)] p-4">
        <div>
          <p className="text-[11px] font-medium tracking-[0.1em] text-faint uppercase">
            Resolved scope
          </p>
          <p className="mt-1 text-[14px] font-medium text-ink">{scopeLabel}</p>
        </div>

        {[
          ["Modules", selectedModules],
          ["Workflows", selectedWorkflows],
          ["Outcomes", selectedOutcomes],
        ].map(([label, values]) =>
          (values as string[]).length ? (
            <div key={label as string}>
              <p className="text-[12px] font-medium text-faint">{label as string}</p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {(values as string[]).map((value) => (
                  <span
                    key={value}
                    className="rounded-full border border-[var(--line)] bg-[var(--bg)] px-2.5 py-1 text-[12px] text-ink"
                  >
                    {value}
                  </span>
                ))}
              </div>
            </div>
          ) : null,
        )}

        {coverage.length ? (
          <div>
            <p className="text-[12px] font-medium text-faint">Coverage</p>
            <ul className="mt-1.5 grid gap-1.5 sm:grid-cols-2">
              {coverage.map((row) => (
                <li
                  key={`${row.label}-${row.status}`}
                  className="flex items-center justify-between gap-2 rounded-[calc(var(--radius)-5px)] border border-[var(--line)] bg-[var(--bg)] px-2.5 py-1.5 text-[12px]"
                >
                  <span className="truncate text-ink">{row.label}</span>
                  <span className="shrink-0 capitalize text-faint">{row.status}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {blockingAmbiguities.length ? (
          <div className="rounded-[calc(var(--radius)-4px)] border border-[color-mix(in_srgb,var(--record)_35%,var(--line))] bg-[color-mix(in_srgb,var(--record)_7%,transparent)] p-3">
            <p className="text-[12px] font-medium text-ink">Blocking ambiguities</p>
            <ul className="mt-1.5 list-disc space-y-1 pl-4 text-[12px] leading-relaxed text-soft">
              {blockingAmbiguities.map((item, index) => (
                <li key={String(asRecord(item).id ?? index)}>{ambiguityText(item)}</li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="grid gap-3">
          <label className="text-[12px] font-medium text-faint">
            Focus
            <Input
              className="mt-1.5 bg-[var(--bg)] text-ink"
              value={s.mode === "feature" ? s.featureScope : s.goalText}
              onChange={(event) =>
                s.mode === "feature"
                  ? s.setFeatureScope(event.target.value)
                  : s.editBrief(event.target.value)
              }
            />
          </label>
          <label className="text-[12px] font-medium text-faint">
            Exclude
            <Textarea
              className="mt-1.5 min-h-16 bg-[var(--bg)] text-ink"
              value={s.context.whatToAvoid}
              onChange={(event) => s.setWhatToAvoid(event.target.value)}
              placeholder="Anything the director should leave out"
            />
          </label>
        </div>
      </div>

      <div>
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div>
            <p className="text-[12px] font-medium text-faint">Planned beats</p>
            <p className="mt-0.5 text-[12px] text-faint">
              Drop a redundant chapter before you spend a job.
            </p>
          </div>
          {s.previewLoading ? (
            <p className="text-[12px] text-faint">Drafting…</p>
          ) : null}
        </div>
        {s.previewError ? (
          <p className="mt-3 text-[13px] text-soft">{s.previewError}</p>
        ) : null}
        {chapters.length > 0 ? (
          <ul className="mt-3 space-y-1.5">
            {chapters.map((chapter) => {
              const dropped = s.droppedChapters.includes(chapter.title);
              return (
                <li
                  key={chapter.title}
                  className={cn(
                    "flex items-center justify-between gap-3 rounded-[calc(var(--radius)-4px)] border px-3 py-2",
                    dropped
                      ? "border-[var(--line)] opacity-45"
                      : "border-[var(--line)] bg-[var(--bg-elev)]",
                  )}
                >
                  <span className="min-w-0">
                    <span className="block truncate text-[13px] font-medium text-ink">
                      {chapter.title}
                    </span>
                    <span className="mt-0.5 block text-[11px] text-faint">
                      {chapter.compressed ? "Compressed arc" : "Full arc"}
                      {chapter.count > 1 ? ` · ${chapter.count} beats` : ""}
                    </span>
                  </span>
                  <button
                    type="button"
                    className="shrink-0 text-[12px] text-soft underline-offset-2 hover:text-ink hover:underline"
                    onClick={() => s.toggleDroppedChapter(chapter.title)}
                  >
                    {dropped ? "Keep" : "Drop"}
                  </button>
                </li>
              );
            })}
          </ul>
        ) : !s.previewLoading && !s.previewError ? (
          <p className="mt-3 text-[13px] text-soft">
            Beats will be planned when you generate (or draft a path earlier).
          </p>
        ) : null}
      </div>

      <ul className="flex flex-wrap gap-x-2 gap-y-2">
        {facts.map((fact) => (
          <li
            key={fact.label}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border border-[var(--line)]",
              "bg-[var(--bg-elev)] px-3 py-1.5 text-[12px]",
            )}
          >
            <span className="text-faint">{fact.label}</span>
            <span className="font-medium text-ink">{fact.value}</span>
          </li>
        ))}
      </ul>

      <p className="text-[13px] leading-relaxed text-soft">
        Nothing starts until you generate. Use Back if something still needs a tweak.
      </p>
    </div>
  );
}
