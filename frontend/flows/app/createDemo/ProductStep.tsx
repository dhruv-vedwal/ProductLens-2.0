"use client";

import { useEffect, useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/studioControls";
import { studioFieldClass } from "@/components/studioPage";
import type { CreateDemoState } from "@/flows/app/createDemo/useCreateDemoState";
import { cn } from "@/lib/utils";

function OptionalReveal({
  open,
  onOpenChange,
  title,
  summary,
  children,
  tour,
}: {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  title: string;
  summary: string;
  children: ReactNode;
  tour?: string;
}) {
  return (
    <div
      data-tour={tour}
      className="border-t border-[var(--line)] pt-5"
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => onOpenChange(!open)}
        className="flex w-full items-start justify-between gap-4 text-left"
      >
        <span className="min-w-0">
          <span className="block text-[13px] font-medium text-ink">{title}</span>
          <span className="mt-1 block text-[12px] leading-relaxed text-faint">{summary}</span>
        </span>
        <span
          className={cn(
            "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border border-[var(--line)] text-soft transition-transform",
            open && "rotate-180",
          )}
        >
          <ChevronDown className="size-3.5" strokeWidth={1.75} />
        </span>
      </button>
      {open ? <div className="mt-4 space-y-4">{children}</div> : null}
    </div>
  );
}

export function ProductStep({ s }: { s: CreateDemoState }) {
  const hasCreds = Boolean(s.loginUsername.trim() || s.loginPassword.trim());
  const hasSession = Boolean(s.browserSessionId);
  const isConnected = s.connectionBadge.tone === "ok";

  const [showConnection, setShowConnection] = useState(
    () => isConnected || hasSession || Boolean(s.connectMsg),
  );
  const [showLogin, setShowLogin] = useState(() => hasCreds);

  useEffect(() => {
    if (isConnected || hasSession || s.connectMsg) setShowConnection(true);
  }, [isConnected, hasSession, s.connectMsg]);

  useEffect(() => {
    if (hasCreds) setShowLogin(true);
  }, [hasCreds]);

  return (
    <div className="space-y-8">
      <div className="grid gap-5 sm:grid-cols-2">
        <Field label="Project name" htmlFor="projectName" hint="Shown in My demos">
          <Input
            id="projectName"
            data-tour="create-demo-project-name"
            className={studioFieldClass}
            value={s.projectName}
            onChange={(e) => s.setProjectName(e.target.value)}
            placeholder="Demo"
          />
        </Field>
        <Field label="Target URL" htmlFor="baseUrl" hint="Required">
          <Input
            id="baseUrl"
            data-tour="create-demo-base-url"
            className={studioFieldClass}
            value={s.baseUrl}
            onChange={(e) => s.setBaseUrl(e.target.value)}
            onBlur={() => void s.ensureProject()}
            placeholder="https://"
            required
          />
        </Field>
      </div>

      <div className="space-y-1">
        <p className="text-[13px] font-medium text-ink">Access</p>
        <p className="text-[12px] leading-relaxed text-faint">
          Optional. Use for saved logins or captcha-heavy apps — public pages can skip this.
          Captchas are solved automatically when Browserbase is configured.
        </p>
      </div>

      <div className="space-y-5">
        <OptionalReveal
          tour="create-demo-product-connection"
          open={showConnection}
          onOpenChange={setShowConnection}
          title="Browser session"
          summary={
            isConnected
              ? s.connectionBadge.label
              : hasSession
                ? "Using a saved login"
                : "Optional — skip for public sites"
          }
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="max-w-md text-[12px] leading-relaxed text-soft">
              We fill email and password automatically. Captchas are handled for you. The session
              can be reused on later demos so you skip this step.
            </p>
            <span
              className={cn(
                "rounded px-2 py-0.5 text-[11px] font-medium tracking-wide",
                s.connectionBadge.tone === "ok" &&
                  "bg-[color-mix(in_srgb,var(--ok)_16%,transparent)] text-[var(--ok)]",
                (s.connectionBadge.tone as string) === "warn" &&
                  "bg-amber-500/15 text-amber-800 dark:text-amber-200",
                (s.connectionBadge.tone as string) === "bad" &&
                  "bg-[color-mix(in_srgb,var(--record)_14%,transparent)] text-[var(--record)]",
                s.connectionBadge.tone === "muted" && "bg-[var(--bg)] text-faint",
              )}
            >
              {s.connectionBadge.label}
            </span>
          </div>
          {typeof s.matchedProject?.lastConnectError === "string" ? (
            <p className="text-[12px] text-[var(--record)]">{s.matchedProject.lastConnectError}</p>
          ) : null}
          {s.connectMsg ? <p className="text-[12px] text-soft">{s.connectMsg}</p> : null}
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={s.connectBusy || !s.token || !s.baseUrl.trim()}
            onClick={() => void s.onConnect()}
          >
            {s.connectBusy
              ? "Connecting…"
              : s.browserSessionId
                ? "Connect now"
                : "Connect"}
          </Button>
          <Field label="Or use a saved credential" htmlFor="browserSessionId" hint="Optional">
            <select
              id="browserSessionId"
              className={cn(studioFieldClass, "w-full")}
              value={s.browserSessionId}
              onChange={(e) => s.setBrowserSessionId(e.target.value)}
            >
              <option value="">None</option>
              {s.browserSessions.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.label}
                </option>
              ))}
            </select>
            <p className="mt-2 text-[12px] leading-relaxed text-faint">
              Save credentials in Settings, or enter username/password above.
            </p>
          </Field>
        </OptionalReveal>

        <OptionalReveal
          tour="create-demo-product-login"
          open={showLogin}
          onOpenChange={setShowLogin}
          title="Password login"
          summary={
            hasCreds
              ? "Credentials ready for this demo"
              : "Username and password — only if the product has a simple login form"
          }
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Username" htmlFor="loginUsername">
              <Input
                id="loginUsername"
                className={studioFieldClass}
                value={s.loginUsername}
                onChange={(e) => s.setLoginUsername(e.target.value)}
                placeholder="Optional"
                autoComplete="username"
              />
            </Field>
            <Field label="Password" htmlFor="loginPassword">
              <Input
                id="loginPassword"
                type="password"
                className={studioFieldClass}
                value={s.loginPassword}
                onChange={(e) => s.setLoginPassword(e.target.value)}
                placeholder="Optional"
                autoComplete="current-password"
              />
            </Field>
          </div>
        </OptionalReveal>
      </div>
    </div>
  );
}
