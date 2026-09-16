"use client";

import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useThemeStore, type ThemePreference } from "@/utilities/themeStore";

function SunIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="1.75" />
      <path
        d="M12 2v2.5M12 19.5V22M4.5 12H2M22 12h-2.5M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M20 14.5A7.5 7.5 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function SystemIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="3" y="4" width="18" height="13" rx="2" stroke="currentColor" strokeWidth="1.75" />
      <path d="M8 20h8M12 17v3" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  );
}

const CYCLE: ThemePreference[] = ["light", "dark", "system"];

const META: Record<
  ThemePreference,
  { label: string; nextHint: string; icon: ReactNode }
> = {
  light: { label: "Light", nextHint: "Switch to dark", icon: <SunIcon /> },
  dark: { label: "Dark", nextHint: "Switch to system", icon: <MoonIcon /> },
  system: { label: "System", nextHint: "Switch to light", icon: <SystemIcon /> },
};

function nextTheme(current: ThemePreference): ThemePreference {
  const i = CYCLE.indexOf(current);
  return CYCLE[(i + 1) % CYCLE.length];
}

export function ThemeToggle({
  labeled = false,
  onCommitted,
}: {
  labeled?: boolean;
  onCommitted?: (value: ThemePreference) => void;
}) {
  const preference = useThemeStore((s) => s.preference);
  const setPreference = useThemeStore((s) => s.setPreference);

  function commit(value: ThemePreference) {
    setPreference(value);
    onCommitted?.(value);
  }

  if (!labeled) {
    const meta = META[preference];
    return (
      <Button
        type="button"
        variant="ghost"
        size="icon"
        data-tour="theme-toggle"
        title={`${meta.label} · ${meta.nextHint}`}
        aria-label={`Theme: ${meta.label}. ${meta.nextHint}`}
          onClick={() => commit(nextTheme(preference))}
      >
        {meta.icon}
      </Button>
    );
  }

  return (
    <div
      className="inline-flex items-center gap-1 rounded-md border border-[var(--line)] bg-elev p-1"
      role="group"
      aria-label="Color theme"
    >
      {CYCLE.map((value) => {
        const active = preference === value;
        return (
          <Button
            key={value}
            type="button"
            variant={active ? "default" : "ghost"}
            size="sm"
            data-tour={`theme-${value}`}
            aria-pressed={active}
            onClick={() => commit(value)}
            className={cn(!active && "text-muted-foreground")}
          >
            {META[value].label}
          </Button>
        );
      })}
    </div>
  );
}
