import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ProfileUnlockModule from "./profileUnlock";

async function freshModule(): Promise<typeof ProfileUnlockModule> {
  vi.resetModules();
  return import("./profileUnlock");
}

describe("profileUnlock", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps unlock bearers scoped to their profile", async () => {
    const unlock = await freshModule();

    unlock.setProfileUnlockToken("profile-a", "token-a");
    unlock.setProfileUnlockToken("profile-b", "token-b");
    unlock.setUnlockActiveProfile("profile-a");

    expect(unlock.getProfileUnlockToken("profile-a")).toBe("token-a");
    expect(unlock.getProfileUnlockToken("profile-b")).toBe("token-b");
    expect(unlock.getActiveProfileUnlockToken()).toBe("token-a");

    unlock.setUnlockActiveProfile("profile-b");
    expect(unlock.getActiveProfileUnlockToken()).toBe("token-b");
  });

  it("removes a bearer without affecting another profile", async () => {
    const unlock = await freshModule();
    unlock.setProfileUnlockToken("profile-a", "token-a");
    unlock.setProfileUnlockToken("profile-b", "token-b");

    unlock.setProfileUnlockToken("profile-a", null);

    expect(unlock.getProfileUnlockToken("profile-a")).toBeNull();
    expect(unlock.getProfileUnlockToken("profile-b")).toBe("token-b");
  });

  it("publishes active-profile and bearer changes", async () => {
    const unlock = await freshModule();
    const listener = vi.fn();
    const unsubscribe = unlock.subscribeProfileUnlock(listener);

    unlock.setUnlockActiveProfile("profile-a");
    unlock.setProfileUnlockToken("profile-a", "token-a");
    unlock.setProfileUnlockToken("profile-a", null);
    unsubscribe();
    unlock.setUnlockActiveProfile("profile-b");

    expect(listener).toHaveBeenCalledTimes(3);
  });
});
