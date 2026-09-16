import Link from "next/link";
import { Button } from "@/components/ui/button";
import {
  StudioBody,
  StudioHeader,
  StudioPage,
  StudioPanel,
  StudioSectionTitle,
} from "@/components/studioPage";

type Update = {
  date: string;
  phase: string;
  title: string;
  why: string;
  items: string[];
};

const UPDATES: Update[] = [
  {
    date: "2026-08",
    phase: "Studio",
    title: "A calmer product UI",
    why: "The app now matches the films it makes — library playback, a shorter create flow, and a landing that shows the product first.",
    items: [
      "My demos is a watchable library, not a file list",
      "Create demo is Story → Product → Look → Generate",
      "Landing hero plays a real walkthrough",
      "Home, Timeline, Settings, and the tour recrafted in place",
    ],
  },
  {
    date: "2026-08",
    phase: "Phase 4",
    title: "Product intelligence",
    why: "Understand the app first — then plan, zoom, and narrate from a real Application Map.",
    items: [
      "Thorough analyzer → versioned Application Map + screenshots (reuse unless you force re-crawl)",
      "Mode 1 (URL-first) on Create demo — primary journey from discovery",
      "Planner reads DB maps; ProductLens dogfood map stays as fallback",
      "Sparse cinematic zoom + visible cursor + 60fps default; subtitle position/size burn-in",
      "DOM/a11y then VisionProvider selector fallback",
      "Timeline: regenerate one scene (TTS + mux) from My demos",
      "Settings: list / delete / merge / wipe product maps",
      "Optional TutorialHint npm package — demos work without it",
    ],
  },
  {
    date: "2026-08",
    phase: "Phase 3",
    title: "Production polish & Demo Studio",
    why: "Demos look like finished product films — not raw screen captures.",
    items: [
      "Eight intro templates; cinematic studio open (mesh → staggered copy → browser chrome + URL → zoom → full-bleed)",
      "Cursor paths, click highlights, and click-zoom motion (on by default)",
      "Credentials prefer login over inventing signup emails; Create demo wizard map for dogfood",
      "Narration/picture sync hardened (no blind lead-trim; AAC head-pad for the first word)",
      "SSE run progress with poll fallback; retry or resume failed runs from a chosen stage",
      "Export aspect / resolution options and optional R2 upload",
      "Target-app browser sessions in Settings for OAuth-gated products",
      "Create demo UI: sticky Demo Studio rail, segmented controls, intro template cards",
    ],
  },
  {
    date: "2026-08",
    phase: "Phase 2",
    title: "Solo-dev AI demos",
    why: "Describe an outcome — AI plans, narrates, and records the walkthrough.",
    items: [
      "Create demo modes: Goal, Feature, and Guided step lists",
      "AI Planner + Narrator via LanguageModelProvider (stub offline / Gemini when keyed)",
      "TTS via SpeechProvider (stub / Sarvam Bulbul) with per-step holds for A/V sync",
      "Captions as SRT/VTT burned in when FFmpeg is available",
      "Voice picker from GET /voices",
      "1080p Playwright recording with browser zoom viewport control",
    ],
  },
  {
    date: "2026-08",
    phase: "Phase 1",
    title: "App shell & first guided loop",
    why: "A calm Editorial Mono workspace where demos are never forced at signup.",
    items: [
      "Marketing landing with full product story structure",
      "Email/password auth plus welcome / settle onboarding",
      "App shell: Home, Create demo, My demos, What's New, Pricing, Settings",
      "Tenant-scoped FastAPI schema and Playwright guided runner",
      "Golden ProductLens journey for local dogfood testing",
    ],
  },
];

const ROADMAP = [
  {
    phase: "Phase 5",
    title: "Multi-audience & business",
    body: "Audience-aware demos, brand kits, and workspace features for teams shipping product stories.",
  },
  {
    phase: "Phase 6",
    title: "Platform maturity",
    body: "Scenarios, multi-agent, interactive/live, marketplace, and enterprise product features.",
  },
  {
    phase: "Phase 7–8",
    title: "Commercial & GitHub",
    body: "Billing/admin (7), then GitHub commit-range before/after demos (8).",
  },
];

export default function WhatsNewPage() {
  return (
    <StudioPage>
      <StudioHeader
        eyebrow="Changelog"
        title={
          <>
            What&apos;s <em className="em">new</em>
          </>
        }
        blurb="Shipped ProductLens capability, newest first — plus what comes after Phase 4."
        actions={
          <Button asChild size="lg">
            <Link href="/createDemo">Try Create demo</Link>
          </Button>
        }
      />

      <StudioBody className="space-y-10">
        <div className="relative space-y-0 border-l border-[var(--line)] pl-6">
          {UPDATES.map((u) => (
            <article key={u.title} className="relative pb-10 last:pb-0">
              <span className="absolute top-1.5 -left-[1.55rem] size-2 rounded-full bg-[var(--record)]" />
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="text-[11px] font-medium tracking-[0.1em] text-faint uppercase">
                  {u.phase}
                </span>
                <span className="text-[12px] text-faint">{u.date}</span>
              </div>
              <h2 className="mt-2 text-xl tracking-tight text-ink">{u.title}</h2>
              <p className="mt-2 max-w-2xl text-sm leading-relaxed text-soft">{u.why}</p>
              <details className="mt-3">
                <summary className="cursor-pointer text-[13px] text-soft hover:text-ink">
                  What shipped
                </summary>
                <ul className="mt-3 space-y-2">
                  {u.items.map((item) => (
                    <li key={item} className="text-sm leading-relaxed text-soft">
                      {item}
                    </li>
                  ))}
                </ul>
              </details>
            </article>
          ))}
        </div>

        <div>
          <StudioSectionTitle
            title="On the roadmap"
            hint="Not built yet — named so nothing disappears from the harness"
          />
          <div className="grid gap-3 md:grid-cols-3">
            {ROADMAP.map((r) => (
              <StudioPanel key={r.phase}>
                <p className="text-[11px] font-medium tracking-[0.1em] text-faint uppercase">
                  {r.phase}
                </p>
                <h3 className="mt-2 text-sm font-medium text-ink">{r.title}</h3>
                <p className="mt-2 text-[13px] leading-relaxed text-soft">{r.body}</p>
              </StudioPanel>
            ))}
          </div>
        </div>
      </StudioBody>
    </StudioPage>
  );
}
