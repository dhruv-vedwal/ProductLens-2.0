/** Public API origin. Keep browser requests configurable without duplicating it per page. */
export const productLensApi =
  process.env.NEXT_PUBLIC_PRODUCTLENS_API ?? "http://localhost:8000";

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

const storageKey = "productlens.session";
export function savedSession(): AuthSession | null {
  if (typeof window === "undefined") return null;
  try {
    return JSON.parse(
      window.localStorage.getItem(storageKey) ?? "null",
    ) as AuthSession | null;
  } catch {
    return null;
  }
}
export function saveSession(session: AuthSession | null) {
  if (typeof window === "undefined") return;
  if (session) window.localStorage.setItem(storageKey, JSON.stringify(session));
  else window.localStorage.removeItem(storageKey);
}
export async function apiFetch(
  path: string,
  init: RequestInit = {},
  token = savedSession()?.access_token,
) {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json");
  return fetch(`${productLensApi}${path}`, { ...init, headers });
}
