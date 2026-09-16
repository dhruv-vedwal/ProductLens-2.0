"use client";

import { useEffect } from "react";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useAuthStore } from "@/flows/auth/store";
import { useThemeStore } from "@/utilities/themeStore";

export function Providers({ children }: { children: React.ReactNode }) {
  const bootstrap = useAuthStore((s) => s.bootstrap);
  const hydrateTheme = useThemeStore((s) => s.hydrateResolved);

  useEffect(() => {
    hydrateTheme();
    void bootstrap();
  }, [bootstrap, hydrateTheme]);

  return (
    <TooltipProvider>
      {children}
      <Toaster />
    </TooltipProvider>
  );
}
