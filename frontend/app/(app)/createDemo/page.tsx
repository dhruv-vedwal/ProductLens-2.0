"use client";

import { Button } from "@/components/ui/button";
import { STEPS, STEP_META } from "@/flows/app/createDemo/constants";
import { LookStep } from "@/flows/app/createDemo/LookStep";
import { ProductStep } from "@/flows/app/createDemo/ProductStep";
import { StoryStep } from "@/flows/app/createDemo/StoryStep";
import { useCreateDemoState } from "@/flows/app/createDemo/useCreateDemoState";
import { cn } from "@/lib/utils";

export default function CreateDemoPage() {
  const s = useCreateDemoState();

  return (
    <div className="-mx-4 -my-6 flex min-h-[calc(100vh-3.25rem)] flex-col sm:-mx-8 sm:-my-8 lg:flex-row">
      <aside className="relative flex w-full shrink-0 flex-col border-b border-[var(--line)] lg:sticky lg:top-0 lg:h-screen lg:w-[19.5rem] lg:self-start lg:overflow-y-auto lg:border-b-0 lg:border-r xl:w-[21rem]">
        <div
          className="pointer-events-none absolute inset-0 opacity-80"
          aria-hidden
          style={{
            background:
              "radial-gradient(ellipse 90% 55% at 10% 0%, color-mix(in srgb, var(--record) 10%, transparent), transparent 55%)",
          }}
        />
        <div className="relative flex flex-1 flex-col px-7 py-8 lg:px-8 lg:py-10">
          <p className="text-[11px] font-medium tracking-[0.16em] text-faint uppercase">
            Demo studio
          </p>
          <h1 className="mt-3 max-w-[10ch] text-[2rem] leading-[1.05] tracking-[-0.03em] text-ink">
            Create <em className="em">demo</em>
          </h1>
          <p className="mt-3 max-w-[18rem] text-[13px] leading-relaxed text-soft">
            Paste a URL, optional credentials, and one task prompt. We film it, then edit.
          </p>

          <nav aria-label="Wizard steps" className="mt-10 flex-1">
            <ol className="hidden space-y-1 lg:block">
              {STEPS.map((label, i) => {
                const done = i < s.stepIndex;
                const active = i === s.stepIndex;
                return (
                  <li key={label}>
                    <button
                      type="button"
                      data-tour={`create-demo-step-${i}`}
                      onClick={() => s.setStepIndex(i)}
                      className={cn(
                        "group flex w-full items-center gap-3 rounded-[calc(var(--radius)-4px)] px-2.5 py-2.5 text-left transition-colors",
                        active && "bg-[var(--bg-elev)]",
                        !active && "hover:bg-[var(--bg-elev)]/60",
                      )}
                    >
                      <span
                        className={cn(
                          "flex size-7 shrink-0 items-center justify-center rounded-full text-[11px] font-medium tabular-nums",
                          active && "bg-ink text-[var(--bg)]",
                          done && !active && "bg-ink/10 text-ink",
                          !done && !active && "border border-[var(--line)] text-faint",
                        )}
                      >
                        {done && !active ? "✓" : i + 1}
                      </span>
                      <span className="min-w-0">
                        <span
                          className={cn(
                            "block text-sm font-medium",
                            active ? "text-ink" : done ? "text-soft" : "text-faint",
                          )}
                        >
                          {label}
                        </span>
                        {active ? (
                          <span className="mt-0.5 block truncate text-[11px] text-faint">
                            {STEP_META[i].blurb}
                          </span>
                        ) : null}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ol>
          </nav>

          <div className="mt-6 lg:hidden">
            <div className="flex gap-1.5 overflow-x-auto pb-1">
              {STEPS.map((label, i) => {
                const active = i === s.stepIndex;
                const done = i < s.stepIndex;
                return (
                  <button
                    key={label}
                    type="button"
                    onClick={() => s.setStepIndex(i)}
                    className={cn(
                      "shrink-0 rounded-full px-3 py-1.5 text-[12px] font-medium",
                      active && "bg-ink text-[var(--bg)]",
                      done && !active && "bg-[var(--line)] text-ink",
                      !done && !active && "border border-[var(--line)] text-faint",
                    )}
                  >
                    {i + 1}. {label}
                  </button>
                );
              })}
            </div>
          </div>

          <p className="mt-auto hidden pt-8 text-[11px] text-faint lg:block">
            Step {s.stepIndex + 1} of {STEPS.length}
          </p>
        </div>
      </aside>

      <div className="relative flex min-w-0 flex-1 flex-col">
        <form
          onSubmit={(e) => {
            e.preventDefault();
          }}
          className="flex min-h-0 flex-1 flex-col"
        >
          <div className="flex-1 px-6 py-8 sm:px-10 lg:px-12 lg:py-10">
            <div className="mb-8 max-w-2xl">
              <p className="text-[11px] font-medium tracking-[0.12em] text-faint uppercase lg:hidden">
                Step {s.stepIndex + 1} · {STEPS[s.stepIndex]}
              </p>
              <h2 className="text-[1.65rem] tracking-[-0.02em] text-ink">
                {STEP_META[s.stepIndex].title}
              </h2>
              <p className="mt-1.5 text-sm leading-relaxed text-soft">
                {STEP_META[s.stepIndex].blurb}
              </p>
            </div>

            <div className={s.stepIndex === 2 ? "max-w-none" : "max-w-2xl"}>
              {s.stepIndex === 0 ? <ProductStep s={s} /> : null}
              {s.stepIndex === 1 ? <StoryStep s={s} /> : null}
              {s.stepIndex === 2 ? <LookStep s={s} /> : null}
            </div>
          </div>

          <div className="sticky bottom-0 z-10 border-t border-[var(--line)] bg-[color-mix(in_srgb,var(--bg)_92%,transparent)] px-6 py-4 backdrop-blur-md sm:px-10 lg:px-12">
            {s.error ? <p className="mb-3 text-sm text-destructive">{s.error}</p> : null}
            <div className="flex max-w-2xl items-center justify-between gap-4">
              <div>
                {s.stepIndex > 0 ? (
                  <Button type="button" variant="ghost" onClick={() => s.setStepIndex(s.stepIndex - 1)}>
                    Back
                  </Button>
                ) : (
                  <span className="text-[12px] text-faint">
                    {s.stepIndex + 1} / {STEPS.length}
                  </span>
                )}
              </div>
              <div>
                {s.stepIndex < STEPS.length - 1 ? (
                  <Button
                    key="continue"
                    type="button"
                    size="lg"
                    data-tour="create-demo-continue"
                    onClick={() => void s.goNext()}
                  >
                    Continue
                  </Button>
                ) : (
                  <Button
                    key="generate"
                    type="button"
                    size="lg"
                    data-tour="create-demo-generate"
                    disabled={s.busy}
                    onClick={() => void s.onGenerate()}
                  >
                    {s.busy ? "Starting…" : "Generate demo"}
                  </Button>
                )}
              </div>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
