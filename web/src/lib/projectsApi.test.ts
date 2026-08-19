// Unit tests for `projectsApi.ts` — the `/v1/projects` first-class CRUD
// client. Happy-path requests with a mocked `fetch`, plus error-path coverage
// that surfaces the server's structured `{error: {message}}` shape.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  addProjectRootToPrivateProfile,
  createProject,
  deleteProject,
  getProject,
  listProjects,
  moveProject,
  moveProjectFolder,
  renameProject,
  updateProjectConfig,
} from "./projectsApi";
import { setProfileUnlockToken } from "./profileUnlock";

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listProjects", () => {
  it("GETs /v1/projects and returns the data array", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ object: "list", data: [{ id: "p_1", name: "A" }] }),
    );
    const result = await listProjects();
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/projects");
    expect(result).toEqual([{ id: "p_1", name: "A" }]);
  });
});

describe("createProject", () => {
  it("POSTs the name and returns the project", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p_1", name: "New" }));
    const result = await createProject("New");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ name: "New" });
    expect(result.id).toBe("p_1");
  });

  it("surfaces the server error message on a duplicate name (409)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { message: "A project named 'New' already exists" } },
        {
          ok: false,
          status: 409,
        },
      ),
    );
    await expect(createProject("New")).rejects.toThrow("already exists");
  });

  it("includes config in the body when provided", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p_1", name: "New" }));
    await createProject("New", { host_id: "h1", agent_id: "ag_1" });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      name: "New",
      config: { host_id: "h1", agent_id: "ag_1" },
    });
  });
});

describe("getProject", () => {
  it("GETs /v1/projects/{id} and returns the project with config", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ id: "p_1", name: "A", config: { workspace: "/w" } }),
    );
    const result = await getProject("p_1");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/projects/p_1");
    expect(result.config).toEqual({ workspace: "/w" });
  });
});

describe("updateProjectConfig", () => {
  it("PATCHes only the config field (url-encoded id)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ id: "p a", name: "A", config: { agent_id: "ag_1" } }),
    );
    await updateProjectConfig("p a", { agent_id: "ag_1" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p%20a");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ config: { agent_id: "ag_1" } });
  });

  it("sends config:{} to clear stored defaults", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p_1", name: "A", config: {} }));
    await updateProjectConfig("p_1", {});
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ config: {} });
  });
});

describe("renameProject", () => {
  it("PATCHes /v1/projects/{id} with the new name (url-encoded id)", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p a", name: "Renamed" }));
    await renameProject("p a", "Renamed");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p%20a");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ name: "Renamed" });
  });

  it("can request atomic adoption of a differently named legacy folder", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p_1", name: "Renamed" }));
    await renameProject("p_1", "Renamed", "Legacy");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      name: "Renamed",
      adopt_legacy_name: "Legacy",
    });
  });
});

describe("moveProject", () => {
  it("PATCHes the destination profile with its scoped unlock token", async () => {
    setProfileUnlockToken("profile-private", "destination-token");
    fetchMock.mockResolvedValueOnce(
      mockResponse({ id: "p_1", name: "A", profile_id: "profile-private" }),
    );

    await moveProject("p_1", "profile-private");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1");
    expect(init.method).toBe("PATCH");
    expect(new Headers(init.headers).get("X-Omnigent-Destination-Profile-Unlock")).toBe(
      "destination-token",
    );
    expect(JSON.parse(init.body as string)).toEqual({ profile_id: "profile-private" });
    setProfileUnlockToken("profile-private", null);
  });
});

describe("moveProjectFolder", () => {
  it("POSTs the destination profile with its scoped unlock token", async () => {
    setProfileUnlockToken("profile-private", "destination-token");
    fetchMock.mockResolvedValueOnce(
      mockResponse({ id: "p_1", name: "A", profile_id: "profile-private" }),
    );

    await moveProjectFolder("p_1", "profile-private");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1/move-folder");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("X-Omnigent-Destination-Profile-Unlock")).toBe(
      "destination-token",
    );
    expect(JSON.parse(init.body as string)).toEqual({ profile_id: "profile-private" });
    setProfileUnlockToken("profile-private", null);
  });
});

describe("addProjectRootToPrivateProfile", () => {
  it("POSTs the project and destination unlock token", async () => {
    setProfileUnlockToken("profile-private", "destination-token");
    fetchMock.mockResolvedValueOnce(
      mockResponse({ id: "p_1", name: "A", profile_id: "profile-private" }),
    );

    await addProjectRootToPrivateProfile("p_1", "profile-private");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/profiles/profile-private/protected-roots/projects");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("X-Omnigent-Destination-Profile-Unlock")).toBe(
      "destination-token",
    );
    expect(JSON.parse(init.body as string)).toEqual({ project_id: "p_1" });
    setProfileUnlockToken("profile-private", null);
  });
});

describe("deleteProject", () => {
  it("DELETEs /v1/projects/{id}", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));
    await deleteProject("p_1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1");
    expect(init.method).toBe("DELETE");
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 404 }));
    await expect(deleteProject("missing")).rejects.toThrow();
  });
});
