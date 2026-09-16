"use client";
import { useEffect, useState } from "react";
import { AppShell } from "../../components/studio/AppShell";
import { apiFetch } from "../../services/api";
import { useAuth } from "../../components/auth/AuthProvider";
type Ready = {
  caption_only: boolean;
  worker_mode: string;
  queue_ready: boolean;
  providers: Record<string, boolean>;
};
export default function Settings() {
  const { user } = useAuth();
  const [ready, setReady] = useState<Ready | null>(null),
    [theme, setTheme] = useState<"system" | "light" | "dark">(
      user?.theme_preference ?? "system",
    ),
    [saved, setSaved] = useState("");
  useEffect(() => {
    void apiFetch("/readiness")
      .then((r) => (r.ok ? r.json() : null))
      .then(setReady);
  }, []);
  async function updateTheme(next: "system" | "light" | "dark") {
    setTheme(next);
    const r = await apiFetch("/auth/preferences", {
      method: "PATCH",
      body: JSON.stringify({ theme_preference: next }),
    });
    setSaved(r.ok ? "Saved" : "Could not save preference");
  }
  return (
    <AppShell>
      <main className="content">
        <div className="pageIntro">
          <div>
            <p className="kicker">SETTINGS</p>
            <h1>Your studio, your preferences.</h1>
            <p>
              Provider credentials stay outside the browser. This page only
              exposes safe delivery status.
            </p>
          </div>
        </div>
        <section className="formLayout">
          <article className="formPanel">
            <p className="kicker">APPEARANCE</p>
            <h1>Interface preference</h1>
            <label>
              Theme
              <select
                value={theme}
                onChange={(e) =>
                  void updateTheme(
                    e.target.value as "system" | "light" | "dark",
                  )
                }
              >
                <option value="system">Use system setting</option>
                <option value="light">Light</option>
                <option value="dark">Dark</option>
              </select>
            </label>
            {saved && <p className="hint">{saved}</p>}
          </article>
          <aside className="videoPanel">
            <p className="kicker">DELIVERY STATUS</p>
            <div className="hint">
              Mode: {ready?.caption_only ? "Silent with captions" : "Narrated"}
            </div>
            <div className="hint">
              Worker: {ready?.worker_mode ?? "checking"}
            </div>
            <div className="hint">
              Browserbase:{" "}
              {ready?.providers.browserbase ? "configured" : "not in use"}
            </div>
            <div className="hint">
              Narration can be enabled later without changing the visual timing
              model.
            </div>
          </aside>
        </section>
      </main>
    </AppShell>
  );
}
