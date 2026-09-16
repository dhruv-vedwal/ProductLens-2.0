"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { GuidedStep } from "@/flows/app/createDemo/types";
import { studioFieldClass, studioTextareaClass } from "@/components/studioPage";
import { cn } from "@/lib/utils";

const ACTIONS = ["goto", "click", "type", "wait", "scroll", "press"];

export function GuidedStepList({
  steps,
  onChange,
}: {
  steps: GuidedStep[];
  onChange: (next: GuidedStep[]) => void;
}) {
  function update(i: number, patch: Partial<GuidedStep>) {
    onChange(steps.map((s, idx) => (idx === i ? { ...s, ...patch } : s)));
  }

  return (
    <div className="space-y-3">
      {steps.map((step, i) => (
        <div
          key={`${i}-${step.action}`}
          className="rounded-[var(--radius)] border border-[var(--line)] bg-[var(--bg-elev)] p-4"
        >
          <div className="flex items-center justify-between gap-3">
            <p className="text-[12px] font-medium text-faint">Beat {i + 1}</p>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => onChange(steps.filter((_, idx) => idx !== i))}
            >
              Remove
            </Button>
          </div>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="space-y-1.5">
              <span className="text-[12px] text-soft">Action</span>
              <select
                className={cn(studioFieldClass, "w-full")}
                value={step.action}
                onChange={(e) => update(i, { action: e.target.value })}
              >
                {ACTIONS.includes(step.action) ? null : (
                  <option value={step.action}>{step.action}</option>
                )}
                {ACTIONS.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1.5">
              <span className="text-[12px] text-soft">Target / URL</span>
              <Input
                className={studioFieldClass}
                value={step.url || step.selector || ""}
                onChange={(e) =>
                  update(i, step.action === "goto" ? { url: e.target.value } : { selector: e.target.value })
                }
                placeholder={step.action === "goto" ? "https://" : "button, field, or note"}
              />
            </label>
          </div>
          <label className="mt-3 block space-y-1.5">
            <span className="text-[12px] text-soft">Narration</span>
            <Textarea
              className={cn(studioTextareaClass, "min-h-[72px]")}
              value={step.narration || step.note || ""}
              onChange={(e) => update(i, { narration: e.target.value })}
              placeholder="What should be said on this beat"
            />
          </label>
        </div>
      ))}
      <Button
        type="button"
        variant="secondary"
        onClick={() => onChange([...steps, { action: "click", selector: "", narration: "" }])}
      >
        Add beat
      </Button>
    </div>
  );
}
