import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "./identity";
import { addActiveProfileParam, createProfile, listProfiles, updateProfile } from "./profilesApi";

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
});
