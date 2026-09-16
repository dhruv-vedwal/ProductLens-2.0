"use client";

import { INTRO_TEMPLATES, type IntroTemplateId } from "@/flows/app/createDemo/introTemplates";
import { cn } from "@/lib/utils";

export function LookPreview({
  introTemplate,
  studioPolish,
  subtitlesEnabled,
  subtitleStyle,
  subtitlePosition,
  exportAspect,
}: {
  introTemplate: IntroTemplateId;
  studioPolish: boolean;
  subtitlesEnabled: boolean;
  subtitleStyle: string;
  subtitlePosition: string;
  exportAspect: string;
}) {
  const tpl = INTRO_TEMPLATES.find((t) => t.id === introTemplate) ?? INTRO_TEMPLATES[0];
  const aspect =
    exportAspect === "9:16" ? "aspect-[9/16] max-h-[28rem]" : exportAspect === "1:1" ? "aspect-square" : "aspect-video";

  return (
    <aside className="sticky top-[4.75rem] hidden self-start xl:block">
      <p className="mb-3 text-[11px] font-medium tracking-[0.12em] text-faint uppercase">Preview</p>
      <div
        className={cn("relative overflow-hidden rounded-[var(--radius)] border border-[var(--line)]", aspect)}
        style={{ background: studioPolish ? tpl.previewBg : "#0c0c0c" }}
      >
        {studioPolish ? (
          <div className="intro-mesh absolute inset-0" data-tone={tpl.dark ? "dark" : "light"} aria-hidden>
            <span className="intro-mesh__orb intro-mesh__orb--a" style={{ background: tpl.previewMesh[0] }} />
            <span className="intro-mesh__orb intro-mesh__orb--b" style={{ background: tpl.previewMesh[1] }} />
            <span className="intro-mesh__orb intro-mesh__orb--c" style={{ background: tpl.previewMesh[2] }} />
          </div>
        ) : null}

        <div className="relative z-[1] flex h-full flex-col px-[7%] pb-[9%] pt-[6%]">
          {studioPolish ? (
            <div
              className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[10px] bg-white shadow-[0_18px_40px_-12px_rgba(0,0,0,0.55)] ring-1 ring-black/10"
            >
              <div className="flex h-[18px] shrink-0 items-center gap-[5px] bg-[#e8e8e6] px-2">
                <span className="size-[7px] rounded-full bg-[#ff5f57]" />
                <span className="size-[7px] rounded-full bg-[#febc2e]" />
                <span className="size-[7px] rounded-full bg-[#28c840]" />
                <span className="ml-1 h-[9px] flex-1 rounded-full bg-white shadow-[inset_0_0_0_1px_rgba(0,0,0,0.06)]" />
              </div>
              <div className="min-h-0 flex-1 bg-gradient-to-br from-[#fafaf8] to-[#ecece8] p-2.5">
                <div className="flex h-full flex-col gap-1.5 rounded-[4px] bg-white/90 p-2 ring-1 ring-black/[0.04]">
                  <div className="flex items-center gap-1.5">
                    <span className="size-[6px] rounded-full" style={{ background: tpl.accent }} />
                    <span className="h-[5px] w-10 rounded-full bg-zinc-200" />
                    <span className="ml-auto h-[5px] w-6 rounded-full bg-zinc-100" />
                  </div>
                  <div className="h-[38%] rounded-[3px] bg-zinc-100" />
                  <div className="flex flex-1 gap-1.5">
                    <div className="flex-1 rounded-[3px] bg-zinc-50 ring-1 ring-zinc-100" />
                    <div className="w-[32%] rounded-[3px] bg-zinc-50 ring-1 ring-zinc-100" />
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="flex-1 rounded-md bg-zinc-900" />
          )}

          {subtitlesEnabled ? (
            <div
              className={cn(
                "pointer-events-none mx-auto mt-2 max-w-[88%] text-center text-[10px] leading-snug",
                subtitlePosition === "top" && "order-first mb-2 mt-0",
                subtitleStyle === "apple" && "rounded-full bg-black/70 px-3 py-1 text-white",
                subtitleStyle === "youtube" && "bg-black px-2 py-0.5 text-white",
                subtitleStyle === "minimal" && "rounded-md bg-black/55 px-2.5 py-1 text-white",
              )}
            >
              Here is where you create the form.
            </div>
          ) : null}
        </div>
      </div>
      <p className="mt-2 text-[11px] text-faint">
        {studioPolish ? `${tpl.label} · framed window` : "Full-bleed"} · {exportAspect}
      </p>
    </aside>
  );
}
