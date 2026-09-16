"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { useAuthStore } from "@/flows/auth/store";

const STEPS = [
  {
    title: "Home",
    body: "A quiet overview of demos and live runs.",
    selector: '[data-tour="nav-home"]',
  },
  {
    title: "Create demo",
    body: "URL, objective, look — generate when you are ready.",
    selector: '[data-tour="nav-createDemo"]',
  },
  {
    title: "My demos",
    body: "Watch finished films and open a run if one fails.",
    selector: '[data-tour="nav-myDemos"]',
  },
];

const TOUR_KEY = "productlens.tour.dismissed";

type Hole = { top: number; left: number; width: number; height: number };

export function TourPrompt() {
  const user = useAuthStore((s) => s.user);
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<number | null>(null);
  const [hole, setHole] = useState<Hole | null>(null);

  useEffect(() => {
    if (!user?.onboardingCompletedAt) return;
    if (typeof window === "undefined") return;
    if (window.localStorage.getItem(TOUR_KEY)) return;
    setOpen(true);
  }, [user]);

  useEffect(() => {
    if (step === null) {
      setHole(null);
      return;
    }
    function place() {
      const current = STEPS[step ?? 0];
      const el = document.querySelector(current.selector) as HTMLElement | null;
      if (!el) {
        setHole(null);
        return;
      }
      const r = el.getBoundingClientRect();
      setHole({
        top: r.top - 6,
        left: r.left - 6,
        width: r.width + 12,
        height: r.height + 12,
      });
    }
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [step]);

  if (!open && step === null) return null;

  function dismiss() {
    window.localStorage.setItem(TOUR_KEY, "1");
    setOpen(false);
    setStep(null);
  }

  if (open && step === null) {
    return (
      <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/30 p-6 sm:items-center">
        <div className="w-full max-w-md rounded-[var(--radius)] border border-[var(--line)] bg-elev p-6">
          <h2 className="text-xl tracking-tight">
            Want a quick tour of <em className="em">ProductLens</em>?
          </h2>
          <p className="mt-2 text-sm text-muted-foreground">
            Three spotlights on the real UI. Skip anytime.
          </p>
          <div className="mt-6 flex gap-3">
            <Button
              type="button"
              onClick={() => {
                setOpen(false);
                setStep(0);
              }}
            >
              Yes, show me
            </Button>
            <Button type="button" variant="ghost" data-tour="tour-dismiss" onClick={dismiss}>
              No thanks
            </Button>
          </div>
        </div>
      </div>
    );
  }

  if (step === null) return null;
  const current = STEPS[step];

  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/45" />
      {hole ? (
        <div
          className="pointer-events-none absolute rounded-md ring-2 ring-[var(--bg)]"
          style={{
            top: hole.top,
            left: hole.left,
            width: hole.width,
            height: hole.height,
            boxShadow: "0 0 0 9999px rgba(0,0,0,0.45)",
          }}
        />
      ) : null}
      <div className="absolute bottom-8 left-1/2 w-[min(420px,90vw)] -translate-x-1/2 rounded-[var(--radius)] border border-[var(--line)] bg-elev p-5">
        <p className="text-xs tracking-wide text-faint uppercase">
          Tour {step + 1} / {STEPS.length}
        </p>
        <h3 className="mt-1 text-lg">{current.title}</h3>
        <p className="mt-2 text-sm text-muted-foreground">{current.body}</p>
        <div className="mt-4 flex justify-between">
          <Button type="button" variant="ghost" onClick={dismiss}>
            Skip
          </Button>
          <Button
            type="button"
            onClick={() => {
              if (step >= STEPS.length - 1) dismiss();
              else setStep(step + 1);
            }}
          >
            {step >= STEPS.length - 1 ? "Done" : "Next"}
          </Button>
        </div>
      </div>
    </div>
  );
}
