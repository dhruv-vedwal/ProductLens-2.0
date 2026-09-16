import Link from "next/link";
import { Button } from "@/components/ui/button";
import {
  StudioBody,
  StudioHeader,
  StudioPage,
  StudioPanel,
  StudioSectionTitle,
} from "@/components/studioPage";

const LOCAL_FEATURES = [
  "Unlimited local jobs on your machine",
  "Story, feature, path, or URL-only recording",
  "Narration, captions, and Demo Studio intros",
  "Library with live progress and resume",
];

const PRO_FEATURES = [
  "Hosted workers and share links",
  "Team workspaces and brand kits",
  "Clear spend caps when billing ships",
];

const FAQ = [
  {
    q: "Is ProductLens free right now?",
    a: "Yes for local/self-host. You bring API keys for Gemini/Sarvam when you want real AI — stubs work offline.",
  },
  {
    q: "When does Pro ship?",
    a: "With Phase 5–6 platform work: hosted jobs, billing, and team features. Pricing here stays honest until then.",
  },
  {
    q: "What do I pay for today?",
    a: "Only your own LLM/TTS provider usage if you enable them. ProductLens itself does not meter local runs.",
  },
];

export default function PricingPage() {
  return (
    <StudioPage>
      <StudioHeader
        eyebrow="Plans"
        title={<em className="em">Pricing</em>}
        blurb="Transparent while we build. Local is free. Pro is named early so the roadmap stays clear — no dark patterns."
        actions={
          <Button asChild size="lg">
            <Link href="/createDemo">Start creating</Link>
          </Button>
        }
      />

      <StudioBody className="space-y-10">
        <div className="grid gap-4 lg:grid-cols-2">
          <StudioPanel className="relative overflow-hidden border-ink">
            <p className="text-[11px] font-medium tracking-[0.12em] text-faint uppercase">
              Available now
            </p>
            <h2 className="mt-2 text-2xl tracking-tight text-ink">Local / Free</h2>
            <p className="mt-2 text-sm leading-relaxed text-soft">
              Run ProductLens on your machine through Phase 1–3. Ideal for solo builders dogfooding
              demos against any URL.
            </p>
            <p className="mt-5 text-3xl tracking-tight text-ink">
              $0<span className="text-base font-normal text-faint"> / forever local</span>
            </p>
            <ul className="mt-6 space-y-2.5 border-t border-[var(--line)] pt-5">
              {LOCAL_FEATURES.map((f) => (
                <li key={f} className="flex gap-3 text-sm text-soft">
                  <span className="mt-2 size-1.5 shrink-0 rounded-full bg-ink" aria-hidden />
                  {f}
                </li>
              ))}
            </ul>
            <Button asChild className="mt-8" size="lg">
              <Link href="/createDemo">Open Create demo</Link>
            </Button>
          </StudioPanel>

          <StudioPanel className="relative opacity-80">
            <p className="text-[11px] font-medium tracking-[0.12em] text-faint uppercase">
              Coming later
            </p>
            <h2 className="mt-2 text-2xl tracking-tight text-ink">Pro</h2>
            <p className="mt-2 text-sm leading-relaxed text-soft">
              Hosted pipeline, teams, and shareable cloud assets — once Phase 5–6 platform work
              lands.
            </p>
            <p className="mt-5 text-3xl tracking-tight text-ink">
              TBD<span className="text-base font-normal text-faint"> · announced with billing</span>
            </p>
            <ul className="mt-6 space-y-2.5 border-t border-[var(--line)] pt-5">
              {PRO_FEATURES.map((f) => (
                <li key={f} className="flex gap-3 text-sm text-soft">
                  <span className="mt-2 size-1.5 shrink-0 rounded-full bg-faint" aria-hidden />
                  {f}
                </li>
              ))}
            </ul>
            <Button asChild variant="secondary" className="mt-8" size="lg">
              <Link href="/whatsNew">See roadmap</Link>
            </Button>
          </StudioPanel>
        </div>

        <div>
          <StudioSectionTitle title="Straight answers" hint="No surprise meters on local" />
          <div className="grid gap-3 md:grid-cols-3">
            {FAQ.map((item) => (
              <StudioPanel key={item.q}>
                <h3 className="text-sm font-medium text-ink">{item.q}</h3>
                <p className="mt-2 text-[13px] leading-relaxed text-soft">{item.a}</p>
              </StudioPanel>
            ))}
          </div>
        </div>
      </StudioBody>
    </StudioPage>
  );
}
