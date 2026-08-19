// Unlock bearers intentionally live only in this JavaScript realm. Reloading
// or closing the tab discards them and locks every private profile again.

const tokens = new Map<string, string>();
const listeners = new Set<() => void>();
let activeProfileId: string | null = null;

function publish(): void {
  for (const listener of listeners) listener();
}

export function setUnlockActiveProfile(id: string | null): void {
  if (activeProfileId === id) return;
  activeProfileId = id;
  publish();
}

export function getActiveProfileUnlockToken(): string | null {
  return activeProfileId ? (tokens.get(activeProfileId) ?? null) : null;
}

export function getProfileUnlockToken(profileId: string): string | null {
  return tokens.get(profileId) ?? null;
}

export function setProfileUnlockToken(profileId: string, token: string | null): void {
  if (token === null) tokens.delete(profileId);
  else tokens.set(profileId, token);
  publish();
}

export function subscribeProfileUnlock(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
