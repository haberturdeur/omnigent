import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "./identity";
import {
  addActiveProfileParam,
  createProfile,
  listProfiles,
  updateProfile,
  useActiveProfile,
} from "./profilesApi";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

const fetchMock = vi.mocked(authenticatedFetch);
const profile = {
  id: "profile-1",
  name: "Work",
  is_default: false,
  config: { host_id: "host-1" },
  protection: {},
  created_at: 1,
};

describe("profiles API", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    window.history.replaceState({}, "", "/");
  });

  it("lists owner profiles", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ object: "list", data: [profile] }), { status: 200 }),
    );

    await expect(listProfiles()).resolves.toEqual([profile]);
    expect(fetchMock).toHaveBeenCalledWith("/v1/profiles");
  });

  it("creates profiles with defaults and protection", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(profile), { status: 200 }));

    await createProfile("Work", {
      icon: "lock",
      color: null,
      config: { host_id: "host-1" },
      protection: { lock: "device", notification_content: "generic" },
    });

    const [, init] = fetchMock.mock.calls[0];
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/profiles");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toMatchObject({
      name: "Work",
      config: { host_id: "host-1" },
      protection: { lock: "device", notification_content: "generic" },
    });
  });

  it("replaces profile settings", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(profile), { status: 200 }));

    await updateProfile("profile/1", { name: "Client work", config: {} });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/profiles/profile%2F1");
    expect(init?.method).toBe("PATCH");
    expect(JSON.parse(String(init?.body))).toEqual({ name: "Client work", config: {} });
  });

  it("leaves URLs unscoped until an active profile resolves", () => {
    const params = new URLSearchParams({ archived: "false" });
    addActiveProfileParam(params);
    expect(params.toString()).toBe("archived=false");
  });

  it("selects the profile named in the page URL for a fresh tab", async () => {
    const otherProfile = { ...profile, id: "profile-2", name: "Personal" };
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ object: "list", data: [profile, otherProfile] }), {
        status: 200,
      }),
    );
    window.history.replaceState({}, "", "/?profile_id=profile-2");
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);

    const { result } = renderHook(() => useActiveProfile(), { wrapper });

    await waitFor(() => expect(result.current.activeProfile?.id).toBe("profile-2"));
  });
});
