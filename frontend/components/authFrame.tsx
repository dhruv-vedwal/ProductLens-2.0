"use client";

import type { ReactNode } from "react";
import { ThemeToggle } from "@/components/themeToggle";
import { Wordmark } from "@/components/wordmark";

function StudioStill() {
  return (
    <div className="relative hidden min-h-screen overflow-hidden bg-[var(--bg)] lg:block">
      <div className="intro-mesh absolute inset-0 dark:hidden" data-tone="light" aria-hidden>
        <span className="intro-mesh__orb intro-mesh__orb--a bg-[#e4ddd4]" />
        <span className="intro-mesh__orb intro-mesh__orb--b bg-[#d8cfc6]" />
        <span className="intro-mesh__orb intro-mesh__orb--c bg-[#ece6de]" />
        <span className="intro-mesh__sheen" />
      </div>
      <div className="intro-mesh absolute inset-0 hidden dark:block" data-tone="dark" aria-hidden>
        <span className="intro-mesh__orb intro-mesh__orb--a bg-[#2a2420]" />
        <span className="intro-mesh__orb intro-mesh__orb--b bg-[#1c1c1c]" />
        <span className="intro-mesh__orb intro-mesh__orb--c bg-[#3a302c]" />
      </div>

      <div className="relative flex h-full flex-col justify-between px-12 py-12 xl:px-16 xl:py-14">
        <Wordmark href="/" className="text-[15px]" />

        <div className="max-w-md">
          <p className="text-[11px] font-medium tracking-[0.16em] text-faint uppercase">
            Demo Studio
          </p>
          <h2 className="mt-4 text-[2.35rem] leading-[1.05] tracking-[-0.035em] text-ink xl:text-[2.75rem]">
            Your product deserves a real <em className="em">demo</em>
          </h2>
          <p className="mt-4 max-w-[34ch] text-[15px] leading-relaxed text-soft">
            Understand it once. Generate a shareable walkthrough — without a film night.
          </p>
        </div>

        <div className="relative mt-10 max-w-lg overflow-hidden rounded-[18px] border border-[var(--line)] bg-[var(--bg-elev)]">
          <div className="flex items-center gap-1.5 border-b border-[var(--line)] px-3.5 py-2.5">
            <span className="size-2 rounded-full bg-[var(--line)]" />
            <span className="size-2 rounded-full bg-[var(--line)]" />
            <span className="size-2 rounded-full bg-[var(--line)]" />
            <span className="ml-2 h-4 flex-1 rounded-sm bg-[var(--bg)]" />
          </div>
          <div className="grid grid-cols-[0.9fr_1.1fr] gap-4 p-5">
            <div className="space-y-2.5 pt-1">
              <div className="h-1.5 w-10 rounded-full bg-[var(--record)]/70" />
              <div className="h-2 w-4/5 rounded-full bg-[var(--line)]" />
              <div className="h-2 w-3/5 rounded-full bg-[var(--line)]" />
              <div className="mt-4 h-7 w-20 rounded-md bg-ink" />
            </div>
            <div className="space-y-2 rounded-md border border-[var(--line)] bg-[var(--bg)] p-3">
              <div className="h-2 w-2/3 rounded-full bg-[var(--line)]" />
              <div className="h-16 rounded-md bg-[var(--line)]/70" />
              <div className="flex gap-2">
                <div className="h-2 flex-1 rounded-full bg-[var(--line)]" />
                <div className="h-2 w-8 rounded-full bg-ink/80" />
              </div>
            </div>
          </div>
        </div>

        <p className="mt-auto pt-10 text-[12px] text-faint">
          No forced demo at signup. Settle in, then create when you are ready.
        </p>
      </div>
    </div>
  );
}

export function AuthFrame({
  title,
  subtitle,
  children,
  footer,
}: {
  title: ReactNode;
  subtitle?: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="grid min-h-screen bg-[var(--bg)] lg:grid-cols-[minmax(0,1fr)_minmax(26rem,32rem)]">
      <StudioStill />

      <div className="flex flex-col border-[var(--line)] bg-[var(--bg-elev)] lg:border-l">
        <div className="flex items-center justify-between px-6 pt-6 sm:px-10 lg:px-12">
          <Wordmark href="/" className="text-[15px] lg:hidden" />
          <div className="ml-auto">
            <ThemeToggle />
          </div>
        </div>

        <div className="flex flex-1 flex-col justify-center px-6 py-10 sm:px-10 lg:px-12">
          <div className="mx-auto w-full max-w-[22rem]">
            <h1 className="text-[1.85rem] leading-tight tracking-[-0.03em] text-ink">{title}</h1>
            {subtitle ? (
              <p className="mt-2 text-sm leading-relaxed text-soft">{subtitle}</p>
            ) : null}
            <div className="mt-8">{children}</div>
            {footer ? <div className="mt-8 text-sm text-soft">{footer}</div> : null}
          </div>
        </div>
      </div>
    </div>
  );
}
