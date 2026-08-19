import { describe, expect, it } from "vitest";

import { codexApprovalModeFromSession } from "./codexApprovalMode";

describe("codexApprovalModeFromSession", () => {
  it("maps standard sandbox presets", () => {
    expect(
      codexApprovalModeFromSession({
        terminalLaunchArgs: ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
      }),
    ).toBe("read-only");
    expect(
      codexApprovalModeFromSession({
        terminalLaunchArgs: ["--sandbox=danger-full-access", "--ask-for-approval=never"],
      }),
    ).toBe("full-access");
  });

  it("treats omitted permission args as the default preset", () => {
    expect(codexApprovalModeFromSession({ terminalLaunchArgs: ["--model", "gpt-5.4"] })).toBe(
      "default",
    );
  });

  it("recognizes persisted Codex permission profiles", () => {
    expect(
      codexApprovalModeFromSession({
        terminalLaunchArgs: ["-c", 'default_permissions=":danger-full-access"'],
      }),
    ).toBe("full-access");
  });

  it("reports a launch-only bypass distinctly from ordinary full access", () => {
    expect(
      codexApprovalModeFromSession({
        labels: { "omnigent.codex_native.bypass_sandbox": "1" },
        terminalLaunchArgs: ["--sandbox", "danger-full-access"],
      }),
    ).toBe("bypass");
  });
});
