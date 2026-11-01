import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "./identity";
import { activeProfileId, selectProfile } from "./activeProfile";
import type { ProjectConfig } from "./projectsApi";

export interface Profile {
  id: string;
  name: string;
  icon: string | null;
  color: string | null;
  is_default: boolean;
  config: ProjectConfig;
}

async function readResponse(response: Response) {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error?.message ?? `Profile request failed (${response.status})`);
  }
  return response.json();
}

export async function listProfiles(): Promise<Profile[]> {
  return (await readResponse(await authenticatedFetch("/v1/profiles"))).data;
}

export async function saveProfile(
  id: string | null,
  input: { name: string; config: ProjectConfig },
): Promise<Profile> {
  return readResponse(
    await authenticatedFetch(id ? `/v1/profiles/${encodeURIComponent(id)}` : "/v1/profiles", {
      method: id ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function deleteProfile(id: string): Promise<void> {
  await readResponse(
    await authenticatedFetch(`/v1/profiles/${encodeURIComponent(id)}`, { method: "DELETE" }),
  );
}

export function useProfiles() {
  const query = useQuery({ queryKey: ["profiles"], queryFn: listProfiles, staleTime: 30_000 });
  const selected = activeProfileId();
  useEffect(() => {
    if (query.data && selected !== "default" && !query.data.some((p) => p.id === selected)) {
      const fallback = query.data.find((p) => p.is_default);
      if (fallback) selectProfile(fallback.id);
    }
  }, [query.data, selected]);
  return {
    ...query,
    activeProfile:
      query.data?.find((p) => p.id === selected) ?? query.data?.find((p) => p.is_default),
  };
}
