"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { motion, useReducedMotion } from "framer-motion";
import { Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { ThemeToggle } from "@/components/themeToggle";
import { Wordmark } from "@/components/wordmark";
import { INTRO_TEMPLATES } from "@/flows/app/createDemo/introTemplates";
import { IntroTemplateCard } from "@/flows/app/createDemo/IntroTemplateCard";
import { cn } from "@/lib/utils";

const HERO_SRC = "/demos/productlens-product-walkthrough.mp4";

const pains = [
  { title: "README isn't a demo", visual: "A repo is not a story" },
  { title: "Loom nights", visual: "An evening gone to edits" },
  { title: "Stale screenshots", visual: "Every release, start over" },
  { title: "Clicks aren't a product", visual: "Recorders don't understand" },
  { title: "Need it shareable", visual: "Minutes, not a film set" },
];

const how = [
  { n: "01", title: "Connect app", body: "Point at a URL. Sign in once if you need to." },
  { n: "02", title: "Build knowledge", body: "A map of routes, flows, and what matters." },
  { n: "03", title: "Generate assets", body: "A narrated walkthrough — and more from the same map." },
];

const outputs = ["Demo video", "Screenshots", "Docs", "Release notes", "Showcase pack"];

function fade(reduce: boolean | null, delay = 0) {
  return {
    initial: reduce ? false : { opacity: 0, y: 16 },
    whileInView: { opacity: 1, y: 0 },
    viewport: { once: true, margin: "-40px" },
    transition: { delay, duration: 0.5, ease: [0.22, 1, 0.36, 1] as const },
  };
}

function HeroFrame({ reduce }: { reduce: boolean | null }) {
  const [failed, setFailed] = useState(false);

  return (
    <motion.div
      id="product"
      className="overflow-hidden rounded-[18px] border border-[var(--line)] bg-elev shadow-device"
      initial={reduce ? false : { opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
    >
      <div className="flex items-center gap-1.5 border-b border-[var(--line)] px-3.5 py-3">
        <span className="h-2 w-2 rounded-full bg-[var(--line)]" />
        <span className="h-2 w-2 rounded-full bg-[var(--line)]" />
        <span className="h-2 w-2 rounded-full bg-[var(--line)]" />
        <span className="ml-2 truncate text-[11px] text-faint">productlens.app</span>
      </div>
      <div className="relative aspect-video bg-black">
        {!failed ? (
          <video
            className="h-full w-full object-cover"
            src={HERO_SRC}
            muted
            loop
            playsInline
            autoPlay={!reduce}
            preload="metadata"
            onError={() => setFailed(true)}
          />
        ) : (
          <div className="flex h-full flex-col justify-end bg-[linear-gradient(160deg,#111,#1c1c1c)] p-6">
            <p className="text-xs tracking-wide text-white/50 uppercase">Demo Studio</p>
            <p className="mt-2 text-lg text-white">
              Your product deserves a <em className="em">demo</em>
            </p>
          </div>
        )}
      </div>
    </motion.div>
  );
}

function MapFragment() {
  return (
    <svg viewBox="0 0 640 220" className="h-48 w-full text-ink" aria-hidden>
      <rect x="24" y="28" width="120" height="36" rx="8" fill="currentColor" opacity="0.9" />
      <text x="40" y="51" fill="var(--bg)" fontSize="12">
        App
      </text>
      {[
        [200, 20, "Auth"],
        [200, 84, "Home"],
        [200, 148, "Create"],
        [360, 52, "Plan"],
        [360, 116, "Record"],
        [500, 84, "Demo"],
      ].map(([x, y, label]) => (
        <g key={String(label)}>
          <rect
            x={Number(x)}
            y={Number(y)}
            width="100"
            height="36"
            rx="8"
            fill="var(--bg-elev)"
            stroke="var(--line)"
          />
          <text x={Number(x) + 16} y={Number(y) + 23} fill="currentColor" fontSize="12">
            {label}
          </text>
        </g>
      ))}
      <path
        d="M144 46 H200 M250 56 H360 M250 102 H360 M250 166 H250 310 V134 H360 M460 70 H500 84"
        stroke="currentColor"
        strokeOpacity="0.25"
        fill="none"
      />
      <circle cx="508" cy="102" r="4" fill="var(--record)" />
    </svg>
  );
}

export function LandingPage() {
  const reduce = useReducedMotion();
  const [scrolled, setScrolled] = useState(false);
  const [menu, setMenu] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const links = [
    { href: "#product", label: "Product" },
    { href: "#how", label: "How it works" },
    { href: "#gallery", label: "Made with" },
    { href: "#pricing", label: "Pricing" },
  ];

  return (
    <div className="min-h-screen">
      <nav
        className={cn(
          "sticky top-0 z-20 flex items-center justify-between bg-[color-mix(in_srgb,var(--bg)_85%,transparent)] px-6 py-5 backdrop-blur-md md:px-10",
          scrolled ? "border-b border-[var(--line)]" : "border-b border-transparent",
        )}
      >
        <Wordmark />
        <div className="hidden items-center gap-6 text-sm text-soft md:flex">
          {links.map((l) => (
            <a key={l.href} href={l.href} className="hover:text-ink">
              {l.label}
            </a>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <Button asChild variant="ghost" size="lg" className="hidden sm:inline-flex">
            <Link href="/login">Log in</Link>
          </Button>
          <Button asChild size="lg" className="hidden sm:inline-flex">
            <Link href="/signup" data-tour="landing-cta-start">
              Start free
            </Link>
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="md:hidden"
            aria-label="Open menu"
            onClick={() => setMenu(true)}
          >
            <Menu className="size-4" />
          </Button>
        </div>
      </nav>

      <Sheet open={menu} onOpenChange={setMenu}>
        <SheetContent side="right" className="px-6 py-8">
          <SheetHeader>
            <SheetTitle>ProductLens</SheetTitle>
          </SheetHeader>
          <div className="mt-6 flex flex-col gap-4 text-sm">
            {links.map((l) => (
              <a key={l.href} href={l.href} onClick={() => setMenu(false)}>
                {l.label}
              </a>
            ))}
            <Button asChild>
              <Link href="/signup">Start free</Link>
            </Button>
            <Button asChild variant="ghost">
              <Link href="/login">Log in</Link>
            </Button>
          </div>
        </SheetContent>
      </Sheet>

      <section className="relative overflow-hidden border-b border-[var(--line)]">
        <div
          className="pointer-events-none absolute inset-0 opacity-70"
          style={{
            background:
              "radial-gradient(ellipse 80% 50% at 70% 20%, color-mix(in srgb, var(--record) 8%, transparent), transparent 55%)",
          }}
        />
        <div className="relative mx-auto grid max-w-6xl gap-12 px-6 pb-16 pt-16 md:grid-cols-[1.05fr_0.95fr] md:items-end md:px-10 md:pt-24">
          <div>
            <p className="mb-4 text-sm font-medium tracking-tight text-ink">ProductLens</p>
            <h1 className="max-w-[12ch] text-[clamp(2.6rem,6vw,4.5rem)] font-medium leading-[0.98] tracking-[-0.04em]">
              Your product deserves a real <em className="em">demo</em>
            </h1>
            <p className="mt-6 max-w-[38ch] text-lg leading-relaxed text-soft">
              Understand it once. Generate a shareable walkthrough — without a film night.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Button asChild size="lg">
                <Link href="/signup" data-tour="landing-cta-hero">
                  Start free
                </Link>
              </Button>
              <Button asChild variant="ghost" size="lg">
                <a href="#how">See how it works</a>
              </Button>
            </div>
          </div>
          <HeroFrame reduce={reduce} />
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-6 py-20 md:px-10">
        <h2 className="text-2xl tracking-tight md:text-3xl">
          You&apos;ve felt this <em className="em">before</em>
        </h2>
        <div className="mt-10 grid gap-6 sm:grid-cols-2 lg:grid-cols-5">
          {pains.map((p, i) => (
            <motion.div
              key={p.title}
              className="border-t border-[var(--line)] pt-4"
              {...fade(reduce, i * 0.05)}
            >
              <div className="mb-4 h-1 w-8 bg-ink/20" />
              <p className="text-sm font-medium">{p.title}</p>
              <p className="mt-3 text-xs text-faint">{p.visual}</p>
            </motion.div>
          ))}
        </div>
      </section>

      <section className="border-y border-[var(--line)] bg-elev py-20">
        <div className="mx-auto grid max-w-6xl gap-10 px-6 md:grid-cols-2 md:px-10">
          <div className="rounded-[var(--radius)] border border-[var(--line)] bg-[var(--bg)] p-6">
            <p className="text-xs tracking-wide text-faint uppercase">Record screen</p>
            <p className="mt-3 text-xl text-soft">Clicks in. Video out. No understanding.</p>
          </div>
          <div className="rounded-[var(--radius)] border border-ink bg-ink p-6 text-[var(--bg)]">
            <p className="text-xs tracking-wide text-[var(--bg)]/60 uppercase">The shift</p>
            <p className="mt-3 text-xl">
              Understand product → <em className="em">generate</em> anything
            </p>
          </div>
        </div>
      </section>

      <section id="how" className="mx-auto max-w-6xl px-6 py-20 md:px-10">
        <h2 className="text-2xl tracking-tight md:text-3xl">How it works</h2>
        <p className="mt-2 text-soft">Three scenes. No essay.</p>
        <div className="mt-12 grid gap-8 md:grid-cols-3">
          {how.map((step, i) => (
            <motion.div
              key={step.title}
              className="overflow-hidden rounded-[var(--radius)] border border-[var(--line)] bg-[var(--bg)]"
              {...fade(reduce, i * 0.08)}
            >
              <div className="intro-mesh relative h-28 overflow-hidden" data-tone="light">
                <span className="intro-mesh__orb intro-mesh__orb--a bg-[#e4e0dc]" />
                <span className="intro-mesh__orb intro-mesh__orb--b bg-[#d8d4d0]" />
                <div className="absolute inset-x-6 bottom-4 rounded-md border border-[var(--line)] bg-white px-3 py-2 text-[11px] text-soft">
                  {step.n} · {step.title}
                </div>
              </div>
              <div className="p-5">
                <h3 className="text-lg">{step.title}</h3>
                <p className="mt-2 text-sm text-soft">{step.body}</p>
              </div>
            </motion.div>
          ))}
        </div>
      </section>

      <section className="border-y border-[var(--line)] py-20">
        <div className="mx-auto max-w-6xl px-6 md:px-10">
          <h2 className="text-2xl tracking-tight md:text-3xl">
            Application <em className="em">Intelligence</em>
          </h2>
          <p className="mt-2 max-w-xl text-soft">
            A reusable map of your product — the understanding behind every asset.
          </p>
          <div className="mt-10 overflow-hidden rounded-[var(--radius)] border border-[var(--line)] bg-elev px-2 py-4">
            <MapFragment />
          </div>
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-6 py-20 md:px-10">
        <h2 className="text-2xl tracking-tight md:text-3xl">Demo Studio</h2>
        <p className="mt-2 text-soft">Frame, cursor, zoom, captions, voice — table stakes, calmly.</p>
        <div className="mt-10 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {INTRO_TEMPLATES.slice(0, 4).map((tpl) => (
            <IntroTemplateCard key={tpl.id} template={tpl} selected={tpl.id === "mist_soft"} onSelect={() => undefined} />
          ))}
        </div>
        <Button asChild variant="ghost" size="lg" className="mt-8">
          <Link href="/signup">Create demos</Link>
        </Button>
      </section>

      <section id="gallery" className="border-y border-[var(--line)] bg-elev py-20">
        <div className="mx-auto max-w-6xl px-6 md:px-10">
          <h2 className="text-2xl tracking-tight md:text-3xl">
            Made with <em className="em">ProductLens</em>
          </h2>
          <p className="mt-2 text-soft">Real demos we generate — starting with ProductLens itself.</p>
          <div className="mt-10 grid gap-6 md:grid-cols-2">
            <figure className="overflow-hidden rounded-[var(--radius)] border border-[var(--line)] bg-[var(--bg)]">
              <video
                className="aspect-video w-full bg-black object-cover"
                src={HERO_SRC}
                muted
                loop
                playsInline
                autoPlay={!reduce}
                preload="metadata"
              />
              <figcaption className="flex items-baseline justify-between gap-3 px-4 py-3">
                <span className="text-sm text-ink">ProductLens · product walkthrough</span>
                <span className="text-xs text-faint">Signup → Create · ~2 min</span>
              </figcaption>
            </figure>
            <p className="self-end text-sm text-faint">More clips soon.</p>
          </div>
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-6 py-20 md:px-10">
        <h2 className="text-2xl tracking-tight md:text-3xl">Create with context</h2>
        <p className="mt-2 text-soft">Product brief → voice → subtitles → generate. AI shouldn&apos;t guess.</p>
        <div className="mt-8 flex flex-wrap gap-3 text-sm text-soft">
          {["Story", "Product", "Look", "Generate"].map((chip) => (
            <span key={chip} className="rounded-full border border-[var(--line)] px-3 py-1.5">
              {chip}
            </span>
          ))}
        </div>
      </section>

      <section id="outputs" className="border-y border-[var(--line)] bg-elev py-20">
        <div className="mx-auto max-w-6xl px-6 md:px-10">
          <h2 className="text-2xl tracking-tight md:text-3xl">More than video</h2>
          <p className="mt-2 text-soft">One map. Many outputs.</p>
          <div className="mt-10 flex flex-wrap gap-4">
            {outputs.map((o) => (
              <div
                key={o}
                className="min-w-[140px] flex-1 border-t border-[var(--line)] pt-4 text-sm font-medium"
              >
                {o}
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-6 py-16 md:px-10">
        <h2 className="text-2xl tracking-tight">Who it&apos;s for</h2>
        <div className="mt-8 flex flex-wrap gap-3">
          {["Builders", "Founders", "Product teams", "Sales"].map((p, i) => (
            <span
              key={p}
              className={`rounded-full border border-[var(--line)] px-4 py-2 text-sm ${
                i === 0 ? "bg-ink text-[var(--bg)]" : "text-soft"
              }`}
            >
              {p}
            </span>
          ))}
        </div>
      </section>

      <section className="border-y border-[var(--line)] py-16">
        <div className="mx-auto grid max-w-6xl gap-8 px-6 md:grid-cols-3 md:px-10">
          {[
            { t: "Settle in", d: "Name and workspace. No forced first demo." },
            { t: "Optional tour", d: "A few spotlights on the real UI." },
            { t: "Create when ready", d: "Story, product, look — then generate." },
          ].map((s) => (
            <div key={s.t}>
              <p className="text-sm font-medium text-ink">{s.t}</p>
              <p className="mt-2 text-sm text-soft">{s.d}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-6 py-16 md:px-10">
        <h2 className="text-2xl tracking-tight">What&apos;s coming</h2>
        <p className="mt-4 text-sm text-soft">
          Brand kits · audience packs · release automation — named, not oversold.
        </p>
      </section>

      <section id="pricing" className="border-y border-[var(--line)] bg-elev py-16">
        <div className="mx-auto max-w-6xl px-6 md:px-10">
          <h2 className="text-2xl tracking-tight">Pricing</h2>
          <p className="mt-2 text-soft">Local free while we build. Pro soon.</p>
          <Button asChild variant="ghost" size="lg" className="mt-6">
            <Link href="/pricing">See plans</Link>
          </Button>
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-6 py-24 text-center md:px-10">
        <h2 className="text-3xl tracking-tight md:text-4xl">
          Show the product. Keep your <em className="em">evening</em>.
        </h2>
        <div className="mt-8 flex justify-center gap-3">
          <Button asChild size="lg">
            <Link href="/signup">Start free</Link>
          </Button>
          <Button asChild variant="ghost" size="lg">
            <Link href="/login">Log in</Link>
          </Button>
        </div>
      </section>

      <footer className="border-t border-[var(--line)] px-6 py-10 text-sm text-faint md:px-10">
        <div className="mx-auto flex max-w-6xl flex-wrap justify-between gap-4">
          <Wordmark />
          <div className="flex gap-4">
            <Link href="/#product" className="hover:text-ink">
              Product
            </Link>
            <Link href="/pricing" className="hover:text-ink">
              Pricing
            </Link>
            <Link href="/login" className="hover:text-ink">
              Log in
            </Link>
          </div>
        </div>
      </footer>
    </div>
  );
}
