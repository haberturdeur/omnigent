import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { activeProfileId, profileRoute } from "./activeProfile";

describe("per-tab profile selection", () => {
  beforeEach(() => {
    sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });
  afterEach(() => {
    sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });
  it("starts in the default profile", () => {
    expect(activeProfileId()).toBe("default");
  });
  it("keeps an explicitly opened profile after navigation removes the query", () => {
    window.history.replaceState({}, "", "/?profile_id=work");
    expect(activeProfileId()).toBe("work");
    window.history.replaceState({}, "", "/c/session");
    expect(activeProfileId()).toBe("work");
  });
  it("lets a newly opened link override the tab's prior selection", () => {
    window.history.replaceState({}, "", "/?profile_id=work");
    activeProfileId();
    window.history.replaceState({}, "", "/?profile_id=personal");
    expect(activeProfileId()).toBe("personal");
    expect(profileRoute("personal")).toContain("profile_id=personal");
  });
});
