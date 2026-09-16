"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { MoreHorizontal, Pencil, Play, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { deleteRun, posterRunUrl, streamRunUrl } from "@/flows/app/createDemo/api";
import type { Run } from "@/flows/app/createDemo/types";
import { formatWhen, runTitle } from "@/flows/app/library/format";
import { useAuthStore } from "@/flows/auth/store";
import { cn } from "@/lib/utils";

export function DemoCard({
  run,
  compact,
  onPlay,
  onDeleted,
}: {
  run: Run;
  compact?: boolean;
  onPlay: (run: Run) => void;
  onDeleted?: (run: Run) => void;
}) {
  const token = useAuthStore((s) => s.token);
  const router = useRouter();
  const [posterFailed, setPosterFailed] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    setPosterFailed(false);
  }, [run.id]);

  async function onDelete() {
    if (!token) return;
    setDeleting(true);
    try {
      await deleteRun(token, run.id);
      setConfirmOpen(false);
      onDeleted?.(run);
    } finally {
      setDeleting(false);
    }
  }

  const poster = token ? posterRunUrl(token, run.id) : null;
  const title = runTitle(run);

  return (
    <article
      className={cn(
        "group overflow-hidden rounded-[var(--radius)] border border-[var(--line)] bg-[var(--bg-elev)]",
        compact && "min-w-[16rem] max-w-[18rem] shrink-0",
      )}
    >
      <div className="relative aspect-video overflow-hidden bg-[var(--bg)]">
        <button
          type="button"
          onClick={() => onPlay(run)}
          className="absolute inset-0 z-[1]"
          aria-label={`Play ${title}`}
        />
        {poster && !posterFailed ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={poster}
            alt=""
            className="h-full w-full object-cover transition duration-500 group-hover:scale-[1.02]"
            onError={() => setPosterFailed(true)}
          />
        ) : (
          <div className="flex h-full items-center justify-center text-[12px] text-faint">
            No poster yet
          </div>
        )}
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center opacity-0 transition group-hover:opacity-100">
          <span className="flex size-11 items-center justify-center rounded-full bg-ink/90 text-[var(--bg)]">
            <Play className="size-4 fill-current" />
          </span>
        </div>
      </div>
      <div className="flex items-start justify-between gap-2 p-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-ink">{title}</p>
          <p className="mt-0.5 text-[11px] text-faint">
            {formatWhen(run.created_at || run.updated_at)}
            {run.status ? ` · ${String(run.status).toLowerCase()}` : ""}
          </p>
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="ghost" size="icon" className="size-8 shrink-0">
              <MoreHorizontal className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={() => onPlay(run)}>
              <Play className="size-3.5" /> Play
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => router.push(`/timeline?run=${run.id}`)}>
              <Pencil className="size-3.5" /> Open run
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setConfirmOpen(true)}>
              <Trash2 className="size-3.5" /> Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete this run?</DialogTitle>
            <DialogDescription>
              Removes the run and its artifacts from your workspace.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button type="button" variant="destructive" disabled={deleting} onClick={() => void onDelete()}>
              {deleting ? "Deleting…" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </article>
  );
}
