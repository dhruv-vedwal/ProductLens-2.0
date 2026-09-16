"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ThemePreference = "light" | "dark" | "system";

type ThemeState = {
  preference: ThemePreference;
  resolved: "light" | "dark";
  setPreference: (value: ThemePreference) => void;
  hydrateResolved: () => void;
};

function resolvePreference(preference: ThemePreference): "light" | "dark" {
  if (preference !== "system") return preference;
  if (typeof window === "undefined") return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyDomTheme(resolved: "light" | "dark") {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.setAttribute("data-theme", resolved);
  root.classList.toggle("dark", resolved === "dark");
}

export const useThemeStore = create<ThemeState>()(
  persist(
    (set, get) => ({
      preference: "system",
      resolved: "light",
      setPreference: (value) => {
        const resolved = resolvePreference(value);
        applyDomTheme(resolved);
        set({ preference: value, resolved });
      },
      hydrateResolved: () => {
        const resolved = resolvePreference(get().preference);
        applyDomTheme(resolved);
        set({ resolved });
      },
    }),
    {
      name: "productlens_theme",
      partialize: (state) => ({ preference: state.preference }),
      onRehydrateStorage: () => (state) => {
        state?.hydrateResolved();
      },
    },
  ),
);

if (typeof window !== "undefined") {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const { preference, hydrateResolved } = useThemeStore.getState();
    if (preference === "system") hydrateResolved();
  });
}
