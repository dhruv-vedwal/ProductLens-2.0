import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** Full-bleed studio canvas — matches Create demo’s negative margin breakout. */
export function StudioPage({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "-mx-4 -my-6 flex min-h-[calc(100vh-3.25rem)] flex-col sm:-mx-8 sm:-my-8",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** Soft red/elev wash behind page headers (same family as Create demo rail). */
export function StudioAtmosphere({ className }: { className?: string }) {
  return (
    <div
      className={cn("pointer-events-none absolute inset-0 opacity-90", className)}
      aria-hidden
      style={{
        background:
          "radial-gradient(ellipse 80% 50% at 8% -10%, color-mix(in srgb, var(--record) 9%, transparent), transparent 55%), radial-gradient(ellipse 60% 40% at 100% 0%, color-mix(in srgb, var(--ink) 4%, transparent), transparent 50%), linear-gradient(180deg, color-mix(in srgb, var(--bg-elev) 65%, transparent), transparent 42%)",
      }}
    />
  );
}

export function StudioHeader({
  eyebrow,
  title,
  blurb,
  actions,
  className,
}: {
  eyebrow: string;
  title: ReactNode;
  blurb?: string;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cn("relative overflow-hidden border-b border-[var(--line)]", className)}>
      <StudioAtmosphere />
      <div className="relative flex flex-col gap-6 px-6 py-8 sm:px-10 lg:flex-row lg:items-end lg:justify-between lg:px-12 lg:py-10">
        <div className="max-w-2xl">
          <p className="text-[11px] font-medium tracking-[0.16em] text-faint uppercase">
            {eyebrow}
          </p>
          <h1 className="mt-3 text-[2rem] leading-[1.05] tracking-[-0.03em] text-ink sm:text-[2.25rem]">
            {title}
          </h1>
          {blurb ? (
            <p className="mt-3 max-w-xl text-[14px] leading-relaxed text-soft">{blurb}</p>
          ) : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-3">{actions}</div> : null}
      </div>
    </header>
  );
}

export function StudioBody({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex-1 px-6 py-8 sm:px-10 lg:px-12 lg:py-10", className)}>
      {children}
    </div>
  );
}

export function StudioPanel({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-[var(--radius)] border border-[var(--line)] bg-[var(--bg-elev)] p-5 sm:p-6",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function StudioSectionTitle({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-4 flex items-baseline justify-between gap-3">
      <div>
        <h2 className="text-[13px] font-medium tracking-wide text-ink">{title}</h2>
        {hint ? <p className="mt-0.5 text-[12px] text-faint">{hint}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function StudioKpi({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
}) {
  return (
    <div className="rounded-[var(--radius)] border border-[var(--line)] bg-[var(--bg-elev)] px-4 py-4 sm:px-5">
      <p className="text-[11px] font-medium tracking-[0.08em] text-faint uppercase">{label}</p>
      <p className="mt-2 text-[1.75rem] leading-none tracking-tight text-ink tabular-nums">
        {value}
      </p>
      {hint ? <p className="mt-2 text-[12px] text-faint">{hint}</p> : null}
    </div>
  );
}

export function MetricRow({
  items,
}: {
  items: { label: string; value: ReactNode; hint?: string }[];
}) {
  return (
    <div className="flex flex-wrap items-end gap-x-10 gap-y-4 border-b border-[var(--line)] pb-6">
      {items.map((item) => (
        <div key={item.label} className="min-w-[7rem]">
          <p className="text-[11px] font-medium tracking-[0.08em] text-faint uppercase">
            {item.label}
          </p>
          <p className="mt-1.5 text-[1.65rem] leading-none tracking-tight text-ink tabular-nums">
            {item.value}
          </p>
          {item.hint ? <p className="mt-1.5 text-[12px] text-faint">{item.hint}</p> : null}
        </div>
      ))}
    </div>
  );
}

export function StatusPill({
  status,
}: {
  status: string;
}) {
  const s = (status || "").toLowerCase();
  const variant =
    s === "succeeded" || s === "done" || s === "complete"
      ? "secondary"
      : s === "failed" || s === "cancelled"
        ? "destructive"
        : s === "running" || s === "queued"
          ? "default"
          : "outline";
  return (
    <Badge variant={variant} className="rounded px-2 py-0.5 text-[11px] tracking-wide uppercase">
      {status.replaceAll("_", " ")}
    </Badge>
  );
}

export const studioFieldClass =
  "h-11 rounded-md border-[var(--line)] bg-[var(--bg-elev)] px-3.5 text-sm shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] placeholder:text-faint focus-visible:border-ink/30";

export const studioTextareaClass =
  "min-h-[120px] resize-y rounded-md border-[var(--line)] bg-[var(--bg-elev)] px-3.5 py-3 text-sm leading-relaxed shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] placeholder:text-faint focus-visible:border-ink/30";
