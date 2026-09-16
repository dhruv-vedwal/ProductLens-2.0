"use client";

import { create } from "zustand";
import * as authApi from "@/flows/auth/api";
import {
  clearOnboarding,
  markOnboardingComplete,
} from "@/flows/auth/api";
import type { AuthSession, User } from "@/flows/auth/types";
import { SESSION_KEY } from "@/flows/auth/types";

type AuthState = {
  token: string | null;
  user: User | null;
  loading: boolean;
  setToken: (token: string | null) => void;
  setSession: (session: AuthSession) => void;
  refreshUser: () => Promise<User | null>;
  logout: () => Promise<void>;
  bootstrap: () => Promise<void>;
  completeOnboarding: () => void;
};

function readSession(): AuthSession | null {
  if (typeof window === "undefined") return null;
  try {
    return JSON.parse(window.localStorage.getItem(SESSION_KEY) ?? "null") as AuthSession | null;
  } catch {
    return null;
  }
}

function writeSession(session: AuthSession | null) {
  if (typeof window === "undefined") return;
  if (session) window.localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  else window.localStorage.removeItem(SESSION_KEY);
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: null,
  user: null,
  loading: true,
  setToken: (token) => {
    const existing = readSession();
    if (token && existing) {
      writeSession({ ...existing, access_token: token });
    } else if (!token) {
      writeSession(null);
    }
    set({ token });
  },
  setSession: (session) => {
    writeSession(session);
    set({ token: session.access_token });
  },
  logout: async () => {
    const token = get().token;
    if (token) {
      try {
        await authApi.logout(token);
      } catch {
        /* ignore */
      }
    }
    writeSession(null);
    clearOnboarding();
    set({ token: null, user: null });
  },
  refreshUser: async () => {
    const token = get().token;
    if (!token) {
      set({ user: null });
      return null;
    }
    try {
      const user = await authApi.fetchMe(token);
      set({ user });
      return user;
    } catch {
      writeSession(null);
      set({ token: null, user: null });
      return null;
    }
  },
  bootstrap: async () => {
    const session = readSession();
    if (!session?.access_token) {
      set({ loading: false, token: null, user: null });
      return;
    }
    set({ token: session.access_token });
    try {
      const user = await authApi.fetchMe(session.access_token);
      set({ user, loading: false });
    } catch {
      writeSession(null);
      set({ token: null, user: null, loading: false });
    }
  },
  completeOnboarding: () => {
    markOnboardingComplete();
    const user = get().user;
    if (user) {
      set({
        user: { ...user, onboardingCompletedAt: new Date().toISOString() },
      });
    }
  },
}));
