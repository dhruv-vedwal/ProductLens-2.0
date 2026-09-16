"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { AppShell, AppShellSkeleton } from "@/components/appShell";
import { TourPrompt } from "@/flows/onboarding/components/tourPrompt";
import { useAuthStore } from "@/flows/auth/store";

export default function AuthenticatedLayout({ children }: { children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user);
  const loading = useAuthStore((s) => s.loading);
  const router = useRouter();

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!user.onboardingCompletedAt) {
      router.replace("/onboarding/welcome");
    }
  }, [user, loading, router]);

  if (loading || !user || !user.onboardingCompletedAt) {
    return <AppShellSkeleton />;
  }

  return (
    <AppShell>
      {children}
      <TourPrompt />
    </AppShell>
  );
}
