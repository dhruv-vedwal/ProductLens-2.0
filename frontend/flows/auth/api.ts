import { http } from "@/utilities/http";
import type { ApiUser, TokenResponse, User } from "@/flows/auth/types";
import { mapApiUser, ONBOARDING_KEY } from "@/flows/auth/types";

function readOnboarding(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(ONBOARDING_KEY);
  } catch {
    return null;
  }
}

export function markOnboardingComplete() {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(ONBOARDING_KEY, new Date().toISOString());
}

export function clearOnboarding() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(ONBOARDING_KEY);
}

export function signup(email: string, password: string, displayName?: string | null) {
  return http<TokenResponse>("/auth/signup", {
    method: "POST",
    body: JSON.stringify({
      email,
      password,
      display_name: displayName?.trim() || null,
    }),
  });
}

export function login(email: string, password: string) {
  return http<TokenResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export async function fetchMe(token: string): Promise<User> {
  const api = await http<ApiUser>("/auth/me", { token });
  return mapApiUser(api, readOnboarding());
}

export function logout(token: string) {
  return http<void>("/auth/logout", { method: "POST", token });
}

export function updateThemePreference(
  token: string,
  themePreference: "system" | "light" | "dark",
) {
  return http<ApiUser>("/auth/preferences", {
    method: "PATCH",
    token,
    body: JSON.stringify({ theme_preference: themePreference }),
  }).then((api) => mapApiUser(api, readOnboarding()));
}
