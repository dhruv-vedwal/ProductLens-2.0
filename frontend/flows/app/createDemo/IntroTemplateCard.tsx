"use client";

import { cn } from "@/lib/utils";
import type { IntroTemplateOption } from "@/flows/app/createDemo/introTemplates";

type Props = {
  template: IntroTemplateOption;
  selected: boolean;
  onSelect: () => void;
};

export function IntroTemplateCard({ template, selected, onSelect }: Props) {
  const tpl = template;

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "group overflow-hidden rounded-lg border text-left transition-all duration-200",
        selected
          ? "border-ink shadow-[0_0_0_1px_var(--ink)]"
          : "border-[var(--line)] hover:border-ink/30 hover:shadow-[0_8px_24px_-12px_rgba(0,0,0,0.35)]",
      )}
    >
      <div
        className="intro-mesh relative aspect-[16/10] w-full overflow-hidden"
        data-tone={tpl.dark ? "dark" : "light"}
        style={{ background: tpl.previewBg }}
        aria-hidden
      >
        <span
          className="intro-mesh__orb intro-mesh__orb--a"
          style={{ background: tpl.previewMesh[0] }}
        />
        <span
          className="intro-mesh__orb intro-mesh__orb--b"
          style={{ background: tpl.previewMesh[1] }}
        />
        <span
          className="intro-mesh__orb intro-mesh__orb--c"
          style={{ background: tpl.previewMesh[2] }}
        />
        <span className="intro-mesh__sheen" />

        {/* Mini split composition */}
        <div className="absolute inset-0 z-[1] flex items-center px-[7%] py-[10%]">
          <div className="w-[44%] pr-1.5">
            <p
              className="font-sans text-[6px] font-medium tracking-[0.2em] uppercase"
              style={{ color: tpl.accent }}
            >
              ProductLens
            </p>
            <p
              className={cn(
                "mt-1.5 font-sans text-[12px] font-medium leading-[1.05] tracking-[-0.035em]",
                tpl.dark ? "text-white" : "text-zinc-900",
              )}
            >
              Your product
              <br />
              deserves a{" "}
              <em className="em text-[13px] tracking-[-0.02em]">demo</em>
            </p>
            <p
              className={cn(
                "mt-2 font-sans text-[6px] font-medium tracking-[0.04em]",
                tpl.dark ? "text-white/40" : "text-zinc-500",
              )}
            >
              Split open · framed walkthrough
            </p>
          </div>

          <div
            className={cn(
              "relative ml-auto w-[52%] overflow-hidden bg-white shadow-[0_10px_28px_-8px_rgba(0,0,0,0.45)]",
              tpl.frameStyle === "rounded" && "rounded-[6px]",
              tpl.frameStyle === "minimal" &&
                "rounded-[4px] ring-1 ring-black/10",
              tpl.frameStyle === "browser" && "rounded-[4px]",
            )}
          >
            <div className="flex h-[9px] items-center gap-[3px] bg-[#e8e8e6] px-1.5">
              <span className="size-[4px] rounded-full bg-[#ff5f57]" />
              <span className="size-[4px] rounded-full bg-[#febc2e]" />
              <span className="size-[4px] rounded-full bg-[#28c840]" />
              <span className="ml-1 h-[3px] flex-1 rounded-sm bg-white/90" />
            </div>
            <div className="space-y-1 bg-gradient-to-br from-[#fafaf8] to-[#ecece8] px-1.5 py-1.5">
              <div className="flex items-center gap-1">
                <span
                  className="size-[3px] rounded-full"
                  style={{ background: tpl.accent }}
                />
                <span className="h-[2px] w-6 rounded-full bg-zinc-300" />
              </div>
              <div className="h-5 rounded-[2px] bg-white/80 ring-1 ring-black/[0.04]" />
              <div className="flex gap-1">
                <div className="h-2 flex-1 rounded-[2px] bg-zinc-200/80" />
                <div className="h-2 w-4 rounded-[2px] bg-zinc-900/80" />
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="flex items-start justify-between gap-2 border-t border-[var(--line)] bg-[var(--bg-elev)] px-3.5 py-3">
        <div className="min-w-0">
          <p className="font-sans text-[13px] font-medium tracking-[-0.015em] text-ink">
            {tpl.label}
          </p>
          <p className="mt-0.5 font-sans text-[11px] leading-snug text-faint">{tpl.hint}</p>
        </div>
        <span
          className={cn(
            "mt-0.5 size-2 shrink-0 rounded-full transition-opacity",
            selected ? "opacity-100" : "opacity-0 group-hover:opacity-40",
          )}
          style={{ background: selected ? "var(--ink)" : tpl.accent }}
        />
      </div>
    </button>
  );
}
