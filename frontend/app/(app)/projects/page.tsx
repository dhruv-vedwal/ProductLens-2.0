"use client";

import { FormEvent, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { createProject, listProjects, renameProject } from "@/flows/app/createDemo/api";
import type { ProjectSummary } from "@/flows/app/createDemo/types";
import {
  StudioBody,
  StudioHeader,
  StudioPage,
  StudioPanel,
  StudioSectionTitle,
  studioFieldClass,
} from "@/components/studioPage";
import { useAuthStore } from "@/flows/auth/store";
import { toast } from "@/components/ui/toaster";
import { formatWhen } from "@/flows/app/library/format";

export default function ProjectsPage() {
  const token = useAuthStore((s) => s.token);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [editName, setEditName] = useState("");

  async function refresh() {
    if (!token) return;
    try {
      setProjects(await listProjects(token));
    } catch {
      setProjects([]);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    if (!token || !name.trim()) return;
    setBusy(true);
    try {
      await createProject(token, name.trim());
      setName("");
      await refresh();
      toast("Project created");
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not create project", {
        tone: "destructive",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <StudioPage>
      <StudioHeader
        eyebrow="Workspace"
        title={
          <>
            <em className="em">Projects</em>
          </>
        }
        blurb="Studio workspaces that group generation runs."
      />
      <StudioBody className="grid gap-6 lg:grid-cols-2">
        <StudioPanel>
          <StudioSectionTitle title="Your projects" />
          {projects.length === 0 ? (
            <p className="text-sm text-faint">No projects yet.</p>
          ) : (
            <ul className="divide-y divide-[var(--line)]">
              {projects.map((project) => (
                <li key={project.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                  {editing === project.id ? (
                    <form
                      className="flex flex-1 flex-wrap gap-2"
                      onSubmit={async (e) => {
                        e.preventDefault();
                        if (!token) return;
                        try {
                          await renameProject(token, project.id, editName.trim());
                          setEditing(null);
                          await refresh();
                          toast("Renamed");
                        } catch (err) {
                          toast(err instanceof Error ? err.message : "Rename failed", {
                            tone: "destructive",
                          });
                        }
                      }}
                    >
                      <Input
                        className={studioFieldClass}
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                      />
                      <Button type="submit" size="sm">
                        Save
                      </Button>
                      <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(null)}>
                        Cancel
                      </Button>
                    </form>
                  ) : (
                    <>
                      <div>
                        <p className="text-sm font-medium text-ink">{project.name}</p>
                        <p className="mt-0.5 text-[11px] text-faint">
                          {formatWhen(project.created_at as string | undefined)}
                          {typeof project.request_count === "number"
                            ? ` · ${project.request_count} requests`
                            : ""}
                        </p>
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setEditing(project.id);
                          setEditName(project.name);
                        }}
                      >
                        Rename
                      </Button>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
        </StudioPanel>

        <StudioPanel>
          <StudioSectionTitle title="New project" />
          <form onSubmit={onCreate} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="project-name">Name</Label>
              <Input
                id="project-name"
                className={studioFieldClass}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Acme sales demos"
                required
              />
            </div>
            <Button type="submit" disabled={busy}>
              {busy ? "Creating…" : "Create project"}
            </Button>
          </form>
        </StudioPanel>
      </StudioBody>
    </StudioPage>
  );
}
