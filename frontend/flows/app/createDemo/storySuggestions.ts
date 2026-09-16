export type StoryChip = { id: string; label: string; blurb: string };

export type StoryPicks = {
  audience: string[];
  show: string[];
  tone: string[];
};

export const AUDIENCE_CHIPS: StoryChip[] = [
  { id: "founders", label: "Founders", blurb: "people deciding whether this is the right product" },
  { id: "customers", label: "Customers", blurb: "people who already care about the problem" },
  { id: "investors", label: "Investors", blurb: "people judging traction and clarity" },
  { id: "new-users", label: "New users", blurb: "someone opening the product for the first time" },
  { id: "team", label: "Your team", blurb: "teammates who need to see how it actually works" },
];

export const SHOW_CHIPS: StoryChip[] = [
  { id: "full-walkthrough", label: "Full walkthrough", blurb: "the primary journey across the product" },
  { id: "signup-to-value", label: "Signup to value", blurb: "from first arrival through the moment it clicks" },
  { id: "core-workflow", label: "Core workflow", blurb: "the job people come back to do" },
  { id: "whats-powerful", label: "What’s powerful", blurb: "the capability that makes this product different" },
];

export const TONE_CHIPS: StoryChip[] = [
  { id: "calm", label: "Calm", blurb: "calm" },
  { id: "confident", label: "Confident", blurb: "confident" },
  { id: "technical", label: "Technical", blurb: "precise and technical" },
  { id: "not-salesy", label: "Not salesy", blurb: "not salesy" },
];

export const DEFAULT_STORY_PICKS: StoryPicks = {
  audience: ["customers"],
  show: ["full-walkthrough"],
  tone: ["calm", "not-salesy"],
};

export function chipLabels(chips: StoryChip[], ids: string[]): string[] {
  return ids
    .map((id) => chips.find((c) => c.id === id)?.label)
    .filter((label): label is string => Boolean(label));
}

export function chipBlurbs(chips: StoryChip[], ids: string[]): string[] {
  return ids
    .map((id) => chips.find((c) => c.id === id)?.blurb)
    .filter((blurb): blurb is string => Boolean(blurb));
}

export function firstSentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  const match = trimmed.match(/^[^.!?]+[.!?]?/);
  return (match?.[0] || trimmed).trim();
}

export function composeDemoBrief(picks: StoryPicks, avoid?: string): string {
  const who = joinPhrase(chipBlurbs(AUDIENCE_CHIPS, picks.audience), "the people who will use this product");
  const what = joinPhrase(chipBlurbs(SHOW_CHIPS, picks.show), "the primary product journey");
  const feel = joinPhrase(chipBlurbs(TONE_CHIPS, picks.tone), "calm and clear");
  const skip = avoid?.trim()
    ? ` Stay off ${avoid.trim().replace(/\.$/, "")}.`
    : " Skip settings, billing, and dead ends unless they are the point.";
  return (
    `Understand the entire product in depth before filming. ` +
    `Create a demo for ${who} that shows ${what}. ` +
    `Keep the narration ${feel}.${skip}`
  );
}

export function applyPicksToContext(
  picks: StoryPicks,
  current: Record<string, string>,
  brief: string,
): Record<string, string> {
  return {
    ...current,
    audience: chipLabels(AUDIENCE_CHIPS, picks.audience).join(", "),
    whatMatters: chipLabels(SHOW_CHIPS, picks.show).join(", "),
    toneNotes: chipLabels(TONE_CHIPS, picks.tone).join(", "),
    whatItDoes: firstSentence(brief) || current.whatItDoes || "",
  };
}

function joinPhrase(parts: string[], fallback: string): string {
  if (!parts.length) return fallback;
  if (parts.length === 1) return parts[0];
  if (parts.length === 2) return `${parts[0]} and ${parts[1]}`;
  return `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;
}
