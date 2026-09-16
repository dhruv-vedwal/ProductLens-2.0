"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/flows/auth/store";
import { AuthFrame } from "@/components/authFrame";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

const PERSONAS = ["Developer", "Founder", "Designer"] as const;

export default function SettlePage() {
  const router = useRouter();
  const token = useAuthStore((s) => s.token);
  const completeOnboarding = useAuthStore((s) => s.completeOnboarding);
  const [displayName, setDisplayName] = useState("");
  const [workspaceName, setWorkspaceName] = useState("My workspace");
  const [persona, setPersona] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!token) {
      router.push("/login");
      return;
    }
    setBusy(true);
    // Soft/local complete — 2.0 has no /auth/settle.
    void displayName;
    void workspaceName;
    void persona;
    completeOnboarding();
    router.push("/home");
  }

  return (
    <AuthFrame title="Settle in" subtitle="Optional details. No demo creation here.">
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="display">Display name</Label>
          <Input
            id="display"
            data-tour="settle-display-name"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="workspace">Workspace name</Label>
          <Input
            id="workspace"
            data-tour="settle-workspace-name"
            value={workspaceName}
            onChange={(e) => setWorkspaceName(e.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label>Role (optional)</Label>
          <div className="flex flex-wrap gap-2">
            {PERSONAS.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setPersona(persona === p ? "" : p)}
                className={cn(
                  "rounded-full border px-3 py-1.5 text-sm",
                  persona === p
                    ? "border-ink bg-ink text-[var(--bg)]"
                    : "border-[var(--line)] text-soft hover:text-ink",
                )}
              >
                {p}
              </button>
            ))}
          </div>
        </div>
        <Button type="submit" data-tour="settle-finish" size="lg" disabled={busy}>
          {busy ? "Saving…" : "Finish setup"}
        </Button>
      </form>
    </AuthFrame>
  );
}
