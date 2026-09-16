/** Client-facing user shape (mapped from 2.0 snake_case API). */
export type User = {
  id: string;
  email: string;
  displayName: string | null;
  themePreference: "system" | "light" | "dark";
  /** Soft/local onboarding flag — 2.0 has no /auth/settle. */
  onboardingCompletedAt: string | null;
};

export type ApiUser = {
  id: string;
  email: string;
  display_name: string | null;
  theme_preference: "system" | "light" | "dark";
};

export type AuthSession = {
  access_token: string;
  token_type: "bearer";
  expires_at: string;
  user: ApiUser;
};

export type TokenResponse = AuthSession;

export const SESSION_KEY = "productlens.session";
export const ONBOARDING_KEY = "productlens.onboarding";

export function mapApiUser(api: ApiUser, onboardingCompletedAt: string | null = null): User {
  return {
    id: api.id,
    email: api.email,
    displayName: api.display_name,
    themePreference: api.theme_preference || "system",
    onboardingCompletedAt,
  };
}
