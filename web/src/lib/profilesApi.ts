import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";

import { authenticatedFetch } from "./identity";
import {
  getProfileUnlockToken,
  setProfileUnlockToken,
  setUnlockActiveProfile,
} from "./profileUnlock";
import type { ProjectConfig } from "./projectsApi";

export interface ProfileProtection {
  lock?: "device" | "passcode";
  notification_content?: "generic" | "disabled";
}

export interface Profile {
  id: string;
  name: string;
  icon?: string | null;
  color?: string | null;
  is_default: boolean;
  config: ProjectConfig;
  protection: ProfileProtection;
  created_at: number;
  updated_at?: number | null;
}

const ACTIVE_PROFILE_KEY = "omnigent.activeProfileId";
const listeners = new Set<() => void>();

function readStoredProfileId(): string | null {
  try {
    return typeof globalThis.localStorage?.getItem === "function"
      ? globalThis.localStorage.getItem(ACTIVE_PROFILE_KEY)
      : null;
  } catch {
    return null;
  }
}

function storeProfileId(id: string): void {
  try {
    if (typeof globalThis.localStorage?.setItem === "function") {
      globalThis.localStorage.setItem(ACTIVE_PROFILE_KEY, id);
    }
  } catch {
    // Profile selection remains valid in memory when storage is unavailable.
  }
}

let resolvedActiveProfileId: string | null = readStoredProfileId();
let resolvedActiveProfile: Profile | null = null;
setUnlockActiveProfile(resolvedActiveProfileId);

export function getActiveProfileId(): string | null {
  return resolvedActiveProfileId;
}

export function getActiveProfile(): Profile | null {
  return resolvedActiveProfile;
}

function publishActiveProfile(id: string): void {
  resolvedActiveProfileId = id;
  setUnlockActiveProfile(id);
  storeProfileId(id);
  for (const listener of listeners) listener();
}

export interface ProfileProtectionStatus {
  profile_id: string;
  configured: boolean;
  unlocked: boolean;
  protected_roots: string[];
}

export async function configureProfileProtection(
  profileId: string,
  passcode: string | null,
  protectedRoots: string[],
): Promise<ProfileProtectionStatus> {
  const res = await authenticatedFetch(`/v1/profiles/${encodeURIComponent(profileId)}/protection`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passcode: passcode || null, protected_roots: protectedRoots }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  setProfileUnlockToken(profileId, null);
  return (await res.json()) as ProfileProtectionStatus;
}

export async function unlockProfile(profileId: string, passcode: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/profiles/${encodeURIComponent(profileId)}/unlock`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passcode }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { token: string };
  setProfileUnlockToken(profileId, body.token);
}

export async function lockProfile(profileId: string): Promise<void> {
  const token = getProfileUnlockToken(profileId);
  try {
    await authenticatedFetch(`/v1/profiles/${encodeURIComponent(profileId)}/unlock`, {
      method: "DELETE",
      headers: token ? { "X-Omnigent-Profile-Unlock": token } : undefined,
    });
  } finally {
    setProfileUnlockToken(profileId, null);
  }
}

export async function disableProfileProtection(profileId: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/profiles/${encodeURIComponent(profileId)}/protection`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  setProfileUnlockToken(profileId, null);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function addActiveProfileParam(params: URLSearchParams): void {
  const id = getActiveProfileId();
  if (id) params.set("profile_id", id);
}

export async function listProfiles(): Promise<Profile[]> {
  const res = await authenticatedFetch("/v1/profiles");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return ((await res.json()) as { data: Profile[] }).data;
}

export async function createProfile(
  name: string,
  options: Pick<Profile, "icon" | "color"> & {
    config?: ProjectConfig;
    protection?: ProfileProtection;
  },
): Promise<Profile> {
  const res = await authenticatedFetch("/v1/profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, ...options }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Profile;
}

export async function updateProfile(
  id: string,
  changes: Partial<Pick<Profile, "name" | "icon" | "color" | "config" | "protection">>,
): Promise<Profile> {
  const res = await authenticatedFetch(`/v1/profiles/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Profile;
}

export async function deleteProfile(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/profiles/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
}

export function useProfiles() {
  return useQuery({ queryKey: ["profiles"], queryFn: listProfiles, staleTime: 30_000 });
}

export function useActiveProfile() {
  const profilesQuery = useProfiles();
  const [, rerender] = useState(0);
  useEffect(() => {
    const listener = () => rerender((value) => value + 1);
    return subscribe(listener);
  }, []);

  const activeProfile = useMemo(() => {
    const profiles = profilesQuery.data ?? [];
    const selected = profiles.find((profile) => profile.id === resolvedActiveProfileId);
    const fallback = selected ?? profiles.find((profile) => profile.is_default) ?? profiles[0];
    if (fallback && fallback.id !== resolvedActiveProfileId) {
      resolvedActiveProfileId = fallback.id;
    }
    return fallback ?? null;
  }, [profilesQuery.data]);

  useEffect(() => {
    if (activeProfile) {
      resolvedActiveProfile = activeProfile;
      resolvedActiveProfileId = activeProfile.id;
      setUnlockActiveProfile(activeProfile.id);
      storeProfileId(activeProfile.id);
      for (const listener of listeners) listener();
    }
  }, [activeProfile]);

  const setActiveProfileId = useCallback(
    (id: string) => {
      resolvedActiveProfile = profilesQuery.data?.find((profile) => profile.id === id) ?? null;
      publishActiveProfile(id);
    },
    [profilesQuery.data],
  );
  return {
    ...profilesQuery,
    profiles: profilesQuery.data ?? [],
    activeProfile,
    activeProfileId: activeProfile?.id ?? null,
    setActiveProfileId,
  };
}

export function useCurrentProfile(): Profile | null {
  const [, rerender] = useState(0);
  useEffect(() => subscribe(() => rerender((value) => value + 1)), []);
  return resolvedActiveProfile;
}
