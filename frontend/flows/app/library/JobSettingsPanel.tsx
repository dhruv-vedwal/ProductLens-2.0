"use client";

/** Settings panel from 1.0 — not used for 2.0 runs (presentation lives in create wizard). */
export function JobSettingsPanel({
  settings,
}: {
  settings?: Record<string, unknown> | null;
  staticOpen?: boolean;
}) {
  if (!settings) return null;
  return (
    <pre className="max-h-40 overflow-auto rounded-lg bg-[var(--bg)] p-3 text-[11px] text-faint">
      {JSON.stringify(settings, null, 2)}
    </pre>
  );
}

export function settingsSummaryLine(settings?: Record<string, unknown> | null) {
  if (!settings) return "";
  const bits = [settings.audience, settings.objective, settings.mode].filter(Boolean);
  return bits.map(String).join(" · ");
}
