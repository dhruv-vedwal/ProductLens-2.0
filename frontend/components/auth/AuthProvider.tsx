"use client";

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  apiFetch,
  productLensApi,
  saveSession,
  savedSession,
  type ApiUser,
  type AuthSession,
} from "../../services/api";

type AuthState = {
  user: ApiUser | null;
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (name: string, email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
};
const AuthContext = createContext<AuthState | null>(null);

async function readSession(path: string, body: object): Promise<AuthSession> {
  const response = await fetch(`${productLensApi}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok)
    throw new Error(payload.detail ?? "We could not sign you in.");
  saveSession(payload);
  return payload;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<ApiUser | null>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const session = savedSession();
    if (!session) {
      setReady(true);
      return;
    }
    apiFetch("/auth/me", {}, session.access_token)
      .then(async (response) => {
        if (!response.ok) throw new Error();
        setUser(await response.json());
      })
      .catch(() => saveSession(null))
      .finally(() => setReady(true));
  }, []);
  const value = useMemo<AuthState>(
    () => ({
      user,
      ready,
      async login(email, password) {
        const session = await readSession("/auth/login", { email, password });
        setUser(session.user);
      },
      async signup(display_name, email, password) {
        const session = await readSession("/auth/signup", {
          display_name,
          email,
          password,
        });
        setUser(session.user);
      },
      async logout() {
        await apiFetch("/auth/logout", { method: "POST" }).catch(
          () => undefined,
        );
        saveSession(null);
        setUser(null);
      },
    }),
    [user, ready],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("AuthProvider is required");
  return value;
}
