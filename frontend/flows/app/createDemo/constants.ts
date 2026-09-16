import type { DemoMode } from "@/flows/app/createDemo/types";

export const STEPS = ["Product", "Task", "Look"] as const;

export const FALLBACK_OPTIONS = [
  { id: "fallback-us", label: "Clear · US", language: "en", accent: "us" },
  { id: "fallback-uk", label: "Soft · UK", language: "en", accent: "uk" },
  { id: "fallback-indian", label: "Warm · Indian", language: "en", accent: "indian" },
  { id: "fallback-neutral", label: "Neutral", language: "en", accent: "neutral" },
];

export const ACCENT_LABELS: Record<string, string> = {
  us: "US",
  uk: "UK",
  indian: "Indian",
  neutral: "Neutral",
};

export const MODES: { value: DemoMode; label: string; hint: string }[] = [
  {
    value: "goal",
    label: "Task prompt",
    hint: "Describe the walkthrough the agent should film",
  },
  {
    value: "autonomous",
    label: "Task prompt",
    hint: "Describe the walkthrough the agent should film",
  },
  {
    value: "feature",
    label: "Task prompt",
    hint: "Describe the walkthrough the agent should film",
  },
  {
    value: "guided",
    label: "Task prompt",
    hint: "Describe the walkthrough the agent should film",
  },
];

export const STEP_META = [
  { title: "The product", blurb: "Start URL and optional sign-in. That is the whole setup." },
  {
    title: "The task",
    blurb: "One prompt: where to start, the ordered goals, and when it is done.",
  },
  { title: "The look", blurb: "Voice, intro, captions, and export size." },
] as const;