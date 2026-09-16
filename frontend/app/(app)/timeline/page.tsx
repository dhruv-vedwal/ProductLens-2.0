"use client";

import { Suspense } from "react";
import TimelineInner from "./TimelineInner";

export default function TimelinePage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-faint">Loading timeline…</p>}>
      <TimelineInner />
    </Suspense>
  );
}
