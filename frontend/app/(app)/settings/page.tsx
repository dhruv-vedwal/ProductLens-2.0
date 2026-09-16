"use client";

import { FormEvent, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  createCredential,
  deleteCredential,
  fetchReadiness,
  listCredentials,
} from "@/flows/app/createDemo/api";
import type { CredentialItem, Readiness } from "@/flows/app/createDemo/types";
import {
  StudioBody,
  StudioHeader,
  StudioPage,
  StudioPanel,
  StudioSectionTitle,
  studioFieldClass,
} from "@/components/studioPage";
import { updateThemePreference } from "@/flows/auth/api";
import { useAuthStore } from "@/flows/auth/store";
import { ThemeToggle } from "@/components/themeToggle";
import { toast } from "@/components/ui/toaster";
import type { ThemePreference } from "@/utilities/themeStore";

export default function SettingsPage() {
  const user = useAuthStore((s) => s.user);
  const token = useAuthStore((s) => s.token);
  const refreshUser = useAuthStore((s) => s.refreshUser);
  const [credentials, setCredentials] = useState<CredentialItem[]>([]);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  async function refreshCredentials() {
    if (!token) return;
    try {
      setCredentials(await listCredentials(token));
    } catch {
      setCredentials([]);
    }
  }

  useEffect(() => {
    void refreshCredentials();
    fetchReadiness()
      .then(setReadiness)
      .catch(() => setReadiness(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  async function onThemeCommitted(value: ThemePreference) {
    if (!token) return;
    try {
      await updateThemePreference(token, value);
      await refreshUser();
      toast("Appearance saved");
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not save", { tone: "destructive" });
    }
  }

  async function onCreateCredential(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    try {
      await createCredential(token, {
        name: name.trim(),
        username: username.trim(),
        password,
      });
      setName("");
      setUsername("");
      setPassword("");
      await refreshCredentials();
      toast("Credential saved");
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not save credential", {
        tone: "destructive",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <StudioPage>
      <StudioHeader
        eyebrow="Account"
        title={<em className="em">Settings</em>}
        blurb="Profile, appearance, product credentials, and delivery readiness."
      />

      <StudioBody>
        <Tabs defaultValue="account">
          <TabsList>
            <TabsTrigger value="account">Account</TabsTrigger>
            <TabsTrigger value="appearance">Appearance</TabsTrigger>
            <TabsTrigger value="credentials">Credentials</TabsTrigger>
            <TabsTrigger value="readiness">Readiness</TabsTrigger>
          </TabsList>

          <TabsContent value="account">
            <div className="grid gap-6 lg:grid-cols-2">
              <StudioPanel>
                <StudioSectionTitle title="Profile" />
                <dl className="space-y-4">
                  <div>
                    <dt className="text-[11px] font-medium tracking-[0.08em] text-faint uppercase">
                      Display name
                    </dt>
                    <dd className="mt-1 text-sm text-ink">{user?.displayName || "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-[11px] font-medium tracking-[0.08em] text-faint uppercase">
                      Email
                    </dt>
                    <dd className="mt-1 text-sm text-ink">{user?.email}</dd>
                  </div>
                </dl>
              </StudioPanel>
              <StudioPanel>
                <StudioSectionTitle title="Session" />
                <p className="text-sm leading-relaxed text-soft">
                  Access tokens live in local storage under{" "}
                  <code className="text-[12px]">productlens.session</code>. Provider API keys
                  never enter the browser.
                </p>
              </StudioPanel>
            </div>
          </TabsContent>

          <TabsContent value="appearance">
            <StudioPanel>
              <StudioSectionTitle
                title="Appearance"
                hint="Saves to your account when you pick one."
              />
              <ThemeToggle labeled onCommitted={onThemeCommitted} />
            </StudioPanel>
          </TabsContent>

          <TabsContent value="credentials">
            <div className="grid gap-6 lg:grid-cols-2">
              <StudioPanel>
                <StudioSectionTitle
                  title="Vault references"
                  hint="Opaque secret:// refs — passwords are never listed back"
                />
                {credentials.length === 0 ? (
                  <p className="text-sm text-faint">No credentials yet.</p>
                ) : (
                  <ul className="divide-y divide-[var(--line)]">
                    {credentials.map((row) => (
                      <li
                        key={row.id || row.reference}
                        className="flex flex-wrap items-center justify-between gap-3 py-3"
                      >
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-ink">{row.name}</p>
                          <p className="mt-0.5 truncate text-[11px] text-faint">
                            {row.reference}
                            {row.source ? ` · ${row.source}` : ""}
                            {row.available === false ? " · unavailable" : ""}
                          </p>
                        </div>
                        {row.id ? (
                          <Button
                            type="button"
                            variant="ghost"
                            onClick={async () => {
                              if (!token || !row.id) return;
                              try {
                                await deleteCredential(token, row.id);
                                await refreshCredentials();
                                toast("Credential deleted");
                              } catch (err) {
                                toast(
                                  err instanceof Error ? err.message : "Delete failed",
                                  { tone: "destructive" },
                                );
                              }
                            }}
                          >
                            Delete
                          </Button>
                        ) : (
                          <span className="text-[11px] text-faint">env</span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </StudioPanel>

              <StudioPanel>
                <StudioSectionTitle title="Add credential" />
                <form onSubmit={onCreateCredential} className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="cred-name">Name</Label>
                    <Input
                      id="cred-name"
                      className={studioFieldClass}
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="acme-staging"
                      required
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="cred-user">Username</Label>
                    <Input
                      id="cred-user"
                      className={studioFieldClass}
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      required
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="cred-pass">Password</Label>
                    <Input
                      id="cred-pass"
                      type="password"
                      className={studioFieldClass}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      required
                    />
                  </div>
                  <Button type="submit" disabled={busy}>
                    {busy ? "Saving…" : "Save credential"}
                  </Button>
                </form>
              </StudioPanel>
            </div>
          </TabsContent>

          <TabsContent value="readiness">
            <StudioPanel>
              <StudioSectionTitle title="Delivery readiness" hint="GET /readiness" />
              {!readiness ? (
                <p className="text-sm text-faint">Checking…</p>
              ) : (
                <dl className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <dt className="text-[11px] tracking-[0.08em] text-faint uppercase">
                      Live generation
                    </dt>
                    <dd className="mt-1 text-sm text-ink">
                      {readiness.live_generation_ready ? "Ready" : "Not ready"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[11px] tracking-[0.08em] text-faint uppercase">
                      Narration
                    </dt>
                    <dd className="mt-1 text-sm text-ink">
                      {readiness.caption_only
                        ? "Caption-only"
                        : readiness.narration_ready
                          ? "Ready"
                          : "Not ready"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[11px] tracking-[0.08em] text-faint uppercase">
                      Cloud browser
                    </dt>
                    <dd className="mt-1 text-sm text-ink">
                      {readiness.cloud_browser_ready ? "Ready" : "Local / not configured"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[11px] tracking-[0.08em] text-faint uppercase">Worker</dt>
                    <dd className="mt-1 text-sm text-ink">
                      {readiness.worker_mode || "—"}
                      {readiness.queue_ready === false ? " · queue not ready" : ""}
                    </dd>
                  </div>
                  {readiness.providers ? (
                    <div className="sm:col-span-2">
                      <dt className="text-[11px] tracking-[0.08em] text-faint uppercase">
                        Providers
                      </dt>
                      <dd className="mt-2 flex flex-wrap gap-2">
                        {Object.entries(readiness.providers).map(([key, ok]) => (
                          <span
                            key={key}
                            className="rounded-full border border-[var(--line)] px-2.5 py-1 text-[11px] text-soft"
                          >
                            {key}: {ok ? "ok" : "off"}
                          </span>
                        ))}
                      </dd>
                    </div>
                  ) : null}
                </dl>
              )}
            </StudioPanel>
          </TabsContent>
        </Tabs>
      </StudioBody>
    </StudioPage>
  );
}
