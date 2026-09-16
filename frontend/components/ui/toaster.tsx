"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

type ToastItem = {
  id: number;
  title: string;
  description?: string;
  tone?: "default" | "destructive";
};

let nextId = 1;
const listeners = new Set<(items: ToastItem[]) => void>();
let items: ToastItem[] = [];

function emit() {
  listeners.forEach((fn) => fn(items));
}

function dismissToast(id: number) {
  items = items.filter((t) => t.id !== id);
  emit();
}

export function toast(title: string, opts?: { description?: string; tone?: "default" | "destructive" }) {
  const id = nextId++;
  items = [...items, { id, title, description: opts?.description, tone: opts?.tone }];
  emit();
  window.setTimeout(() => {
    dismissToast(id);
  }, 4200);
}

export function Toaster() {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  useEffect(() => {
    listeners.add(setToasts);
    setToasts(items);
    return () => {
      listeners.delete(setToasts);
    };
  }, []);

  if (!toasts.length) return null;

  return (
    <div className="pointer-events-none fixed right-4 bottom-4 z-[80] flex w-[min(22rem,calc(100vw-2rem))] flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            "pointer-events-auto flex items-start gap-3 rounded-[var(--radius)] border px-4 py-3 text-sm",
            t.tone === "destructive"
              ? "border-[color-mix(in_srgb,var(--record)_35%,var(--line))] bg-[var(--bg-elev)] text-[var(--record)]"
              : "border-[var(--line)] bg-[var(--bg-elev)] text-ink",
          )}
        >
          <div className="min-w-0 flex-1">
            <p className="font-medium">{t.title}</p>
            {t.description ? <p className="mt-0.5 text-[12px] text-soft">{t.description}</p> : null}
          </div>
          <button
            type="button"
            aria-label="Dismiss"
            className="mt-0.5 shrink-0 rounded-md p-0.5 text-faint transition-colors hover:bg-[var(--bg)] hover:text-ink"
            onClick={() => dismissToast(t.id)}
          >
            <X className="size-3.5" strokeWidth={1.75} />
          </button>
        </div>
      ))}
    </div>
  );
}
