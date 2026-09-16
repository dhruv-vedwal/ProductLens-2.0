"use client";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export function HoverText({
  text,
  className,
  lines = 1,
}: {
  text: string;
  className?: string;
  lines?: 1 | 2;
}) {
  const value = text.trim();
  if (!value) return null;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "block min-w-0",
            lines === 2 ? "line-clamp-2 whitespace-normal" : "truncate",
            className,
          )}
        >
          {value}
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-sm whitespace-pre-wrap break-words">
        {value}
      </TooltipContent>
    </Tooltip>
  );
}
