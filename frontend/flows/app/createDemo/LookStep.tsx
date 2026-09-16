"use client";

import { Field, Segmented, ToggleRow } from "@/components/studioControls";
import { studioFieldClass } from "@/components/studioPage";
import { Input } from "@/components/ui/input";
import { ACCENT_LABELS } from "@/flows/app/createDemo/constants";
import { INTRO_TEMPLATES } from "@/flows/app/createDemo/introTemplates";
import { IntroTemplateCard } from "@/flows/app/createDemo/IntroTemplateCard";
import { LookPreview } from "@/flows/app/createDemo/LookPreview";
import type { CreateDemoState } from "@/flows/app/createDemo/useCreateDemoState";
import { cn } from "@/lib/utils";

export function LookStep({ s }: { s: CreateDemoState }) {
  return (
    <div className="grid gap-10 xl:grid-cols-[minmax(0,1fr)_18rem]">
      <div className="space-y-8">
        <section className="space-y-4">
          <h3 className="text-[13px] font-medium tracking-wide text-faint uppercase">Narration</h3>
          <ToggleRow
            checked={s.includeAudio}
            onChange={s.setIncludeAudio}
            title="Narration audio"
            description="Spoken voiceover via TTS. Turn off for a silent walkthrough video."
          />
          <div
            className={cn(
              "grid gap-5 sm:grid-cols-2",
              !s.includeAudio && "pointer-events-none opacity-40",
            )}
          >
            <Field label="Language" htmlFor="language">
              <select
                id="language"
                className={cn(studioFieldClass, "w-full")}
                value={s.language}
                onChange={(e) => s.setLanguage(e.target.value)}
                disabled={!s.includeAudio}
              >
                <option value="en">English</option>
              </select>
            </Field>
            <Field label="Accent" htmlFor="accent">
              <select
                id="accent"
                className={cn(studioFieldClass, "w-full")}
                value={s.accent}
                disabled={!s.includeAudio || s.voicesLoading}
                onChange={(e) => s.setAccent(e.target.value)}
              >
                {s.accents.map((a) => (
                  <option key={a} value={a}>
                    {ACCENT_LABELS[a] || a}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Target length" htmlFor="targetDurationMinutes">
              <Segmented
                value={String(s.targetDurationMinutes)}
                onChange={(v) => s.setTargetDurationMinutes(Number(v))}
                options={[
                  { value: "0.5", label: "30s" },
                  { value: "1", label: "1m" },
                  { value: "2", label: "2m" },
                  { value: "3", label: "3m" },
                  { value: "5", label: "5m" },
                ]}
              />
            </Field>
            <Field label="Speech pace" htmlFor="pace" hint="1.0 = natural">
              <Input
                id="pace"
                type="number"
                min={0.5}
                max={1.5}
                step={0.1}
                className={studioFieldClass}
                value={s.pace}
                disabled={!s.includeAudio}
                onChange={(e) => s.setPace(Number(e.target.value))}
              />
            </Field>
          </div>
        </section>

        <section className="space-y-4">
          <h3 className="text-[13px] font-medium tracking-wide text-faint uppercase">Intro</h3>
          <ToggleRow
            checked={s.studioPolish}
            onChange={s.setStudioPolish}
            title="Studio intro"
            description="Branded open and close cards around the walkthrough"
          />
          {s.studioPolish ? (
            <div className="grid gap-3.5 sm:grid-cols-2">
              {INTRO_TEMPLATES.map((tpl) => (
                <IntroTemplateCard
                  key={tpl.id}
                  template={tpl}
                  selected={s.introTemplate === tpl.id}
                  onSelect={() => s.setIntroTemplate(tpl.id)}
                />
              ))}
            </div>
          ) : null}
          <p className="text-[12px] leading-relaxed text-faint">
            Each job re-explores the live product — application maps are not reused until mapping
            quality is stable.
          </p>
        </section>

        <section className="space-y-4">
          <h3 className="text-[13px] font-medium tracking-wide text-faint uppercase">Captions</h3>
          <ToggleRow
            checked={s.subtitlesEnabled}
            onChange={s.setSubtitlesEnabled}
            title="Burned-in captions"
            description="Stay readable without covering the UI"
          />
          <div className={cn("space-y-5", !s.subtitlesEnabled && "pointer-events-none opacity-40")}>
            <Field label="Style">
              <Segmented
                value={s.subtitleStyle}
                onChange={s.setSubtitleStyle}
                disabled={!s.subtitlesEnabled}
                options={[
                  { value: "minimal", label: "Minimal" },
                  { value: "apple", label: "Apple" },
                  { value: "youtube", label: "YouTube" },
                ]}
              />
            </Field>
            <div className="grid gap-5 sm:grid-cols-2">
              <Field label="Position">
                <Segmented
                  value={s.subtitlePosition}
                  onChange={s.setSubtitlePosition}
                  disabled={!s.subtitlesEnabled}
                  options={[
                    { value: "bottom", label: "Bottom" },
                    { value: "top", label: "Top" },
                  ]}
                />
              </Field>
              <Field label="Size">
                <Segmented
                  value={s.subtitleFontSize}
                  onChange={s.setSubtitleFontSize}
                  disabled={!s.subtitlesEnabled}
                  options={[
                    { value: "sm", label: "Small" },
                    { value: "md", label: "Medium" },
                    { value: "lg", label: "Large" },
                  ]}
                />
              </Field>
            </div>
          </div>
        </section>

        <button
          type="button"
          className="text-[13px] text-soft underline-offset-4 hover:text-ink hover:underline"
          onClick={() => s.setShowLookAdvanced(!s.showLookAdvanced)}
        >
          {s.showLookAdvanced ? "Hide motion & export" : "Advanced: motion & export"}
        </button>

        {s.showLookAdvanced ? (
          <section className="space-y-4">
            <div className="grid gap-5 sm:grid-cols-2">
              <Field label="Cursor path" htmlFor="cursorStyle">
                <select
                  id="cursorStyle"
                  className={cn(studioFieldClass, "w-full")}
                  value={s.cursorStyle}
                  onChange={(e) => s.setCursorStyle(e.target.value)}
                >
                  <option value="default">Smooth</option>
                  <option value="large">Longer arc</option>
                  <option value="spotlight">Spotlight pace</option>
                </select>
              </Field>
              <Field label="Click highlight" htmlFor="highlightStyle">
                <select
                  id="highlightStyle"
                  className={cn(studioFieldClass, "w-full")}
                  value={s.highlightStyle}
                  onChange={(e) => s.setHighlightStyle(e.target.value)}
                >
                  <option value="none">None</option>
                  <option value="pulse">Pulse</option>
                  <option value="border">Border</option>
                  <option value="spotlight">Spotlight</option>
                </select>
              </Field>
              <Field label="Export aspect">
                <Segmented
                  value={s.exportAspect}
                  onChange={s.setExportAspect}
                  options={[
                    { value: "16:9", label: "16:9" },
                    { value: "9:16", label: "9:16" },
                    { value: "1:1", label: "1:1" },
                  ]}
                />
              </Field>
              <Field label="Resolution">
                <Segmented
                  value={s.exportResolution}
                  onChange={s.setExportResolution}
                  options={[
                    { value: "720", label: "720p" },
                    { value: "1080", label: "1080p" },
                    { value: "4k", label: "4K" },
                  ]}
                />
              </Field>
              <Field label="Browser zoom" htmlFor="browserZoomPercent" className="sm:col-span-2">
                <Segmented
                  value={String(s.browserZoomPercent)}
                  onChange={(v) => s.setBrowserZoomPercent(Number(v))}
                  options={[
                    { value: "75", label: "75%" },
                    { value: "90", label: "90%" },
                    { value: "100", label: "100%" },
                    { value: "110", label: "110%" },
                    { value: "125", label: "125%" },
                  ]}
                />
              </Field>
            </div>
            <ToggleRow
              checked={s.clickZoom}
              onChange={s.setClickZoom}
              title="Focus zoom on clicks"
              description="During recording, zoom toward the control you click — not a separate studio motion pass over the whole film"
            />
          </section>
        ) : null}
      </div>

      <LookPreview
        introTemplate={s.introTemplate}
        studioPolish={s.studioPolish}
        subtitlesEnabled={s.subtitlesEnabled}
        subtitleStyle={s.subtitleStyle}
        subtitlePosition={s.subtitlePosition}
        exportAspect={s.exportAspect}
      />
    </div>
  );
}
