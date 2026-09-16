"use client";

import { useEffect, useRef, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { downloadRunVideo, posterRunUrl, streamRunUrl } from "@/flows/app/createDemo/api";
import type { Run } from "@/flows/app/createDemo/types";
import { runTitle } from "@/flows/app/library/format";
import { useAuthStore } from "@/flows/auth/store";

export function DemoPlayer({
  run,
  open,
  onOpenChange,
}: {
  run: Run | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const token = useAuthStore((s) => s.token);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [src, setSrc] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !run || !token) {
      setSrc(null);
      return;
    }
    setSrc(streamRunUrl(token, run.id));
  }, [open, run, token]);

  if (!run) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[min(56rem,calc(100vw-2rem))] overflow-hidden p-0">
        <DialogHeader className="px-5 pt-5 pb-0">
          <DialogTitle>{runTitle(run)}</DialogTitle>
        </DialogHeader>
        <div className="bg-black">
          {src ? (
            <video
              ref={videoRef}
              className="aspect-video w-full"
              src={src}
              poster={token ? posterRunUrl(token, run.id) : undefined}
              controls
              autoPlay
              playsInline
            />
          ) : null}
        </div>
        <div className="flex justify-end gap-2 px-5 py-4">
          <Button
            type="button"
            variant="secondary"
            onClick={() =>
              token &&
              downloadRunVideo(
                token,
                run.id,
                `${runTitle(run).replace(/\s+/g, "-").slice(0, 48)}.mp4`,
              )
            }
          >
            Download
          </Button>
          <Button type="button" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
