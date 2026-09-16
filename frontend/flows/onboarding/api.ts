/** Soft local onboarding — 2.0 has no /auth/settle or /auth/tour. */
export function settle() {
  return Promise.resolve(null);
}

export function tourEvent() {
  return Promise.resolve(null);
}
