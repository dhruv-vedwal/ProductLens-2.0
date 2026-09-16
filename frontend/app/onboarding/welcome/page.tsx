"use client";

import { useRouter } from "next/navigation";
import { AuthFrame } from "@/components/authFrame";
import { Button } from "@/components/ui/button";

export default function WelcomePage() {
  const router = useRouter();

  return (
    <AuthFrame
      title={
        <>
          Welcome to <em className="em">ProductLens</em>
        </>
      }
      subtitle="Understand the product once. Generate demos when you are ready — never forced at signup."
    >
      <Button
        type="button"
        data-tour="welcome-continue"
        size="lg"
        onClick={() => router.push("/onboarding/settle")}
      >
        Continue
      </Button>
    </AuthFrame>
  );
}
