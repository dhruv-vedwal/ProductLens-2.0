"use client";

import { Textarea } from "@/components/ui/textarea";
import { studioTextareaClass } from "@/components/studioPage";
import type { CreateDemoState } from "@/flows/app/createDemo/useCreateDemoState";
import { cn } from "@/lib/utils";

const PLACEHOLDER =
  "Navigate to the login page and sign in. From the dashboard, create a new form titled Product Demo Request from scratch. Add a short text question for Company name, a dropdown for Company size, an email question, and a long text question. Preview the form and return to the editor.";

export function StoryStep({ s }: { s: CreateDemoState }) {
  return (
    <div className="space-y-8">
      <div data-tour="create-demo-context-whatItDoes" className="space-y-3">
        <div>
          <p className="text-[13px] font-medium text-ink">Task prompt</p>
          <p className="mt-0.5 text-[12px] text-faint">
            Ordered goals and the end state. This is the only flow instruction — we do not map the whole product first.
          </p>
        </div>
        <Textarea
          id="goalText"
          data-tour="create-demo-goal"
          className={cn(studioTextareaClass, "min-h-[180px]")}
          value={s.goalText}
          onChange={(e) => s.editBrief(e.target.value)}
          placeholder={PLACEHOLDER}
        />
      </div>

      <div>
        <p className="text-[12px] font-medium text-faint">Narration</p>
        <p className="mt-1 text-[12px] text-faint">How-to explains what is on screen. Showcase sells the outcome.</p>
        <div className="mt-2 flex flex-wrap gap-2">
          {(
            [
              ["teach", "How-to"],
              ["showcase", "Showcase"],
            ] as const
          ).map(([id, label]) => {
            const on = s.narrationStyle === id;
            return (
              <button
                key={id}
                type="button"
                aria-pressed={on}
                onClick={() => s.setNarrationStyle(id)}
                className={cn(
                  "rounded-full border px-3.5 py-2 text-[13px] font-medium transition-colors",
                  on
                    ? "border-ink bg-ink text-[var(--bg)]"
                    : "border-[var(--line)] bg-[var(--bg-elev)] text-ink hover:border-ink/35",
                )}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
