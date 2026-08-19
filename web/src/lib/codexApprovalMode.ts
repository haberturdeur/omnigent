import type { Session } from "./types";

export type CodexApprovalMode = "default" | "read-only" | "full-access";
export type CodexApprovalState = CodexApprovalMode | "bypass";

export const CODEX_RUNTIME_APPROVAL_MODES: readonly {
  value: CodexApprovalMode;
  label: string;
  description: string;
}[] = [
  {
    value: "default",
    label: "Default",
    description: "Read, edit, and run in the workspace; ask before external access",
  },
  {
    value: "read-only",
    label: "Read only",
    description: "Ask before edits, commands, or network access",
  },
  {
    value: "full-access",
    label: "Full access",
    description: "Edit any file and access the internet without approval",
  },
];

export const CODEX_SESSION_APPROVAL_OPTIONS: readonly {
  value: CodexApprovalState;
  label: string;
  description: string;
  disabled?: boolean;
}[] = [
  ...CODEX_RUNTIME_APPROVAL_MODES,
  {
    value: "bypass",
    label: "Bypass approvals & sandbox",
    description: "Launch-only bypass; select another mode to restore safeguards",
    disabled: true,
  },
];

/** Resolve the standard Codex preset represented by persisted resume args. */
export function codexApprovalModeFromSession(
  session: Pick<Session, "terminalLaunchArgs" | "labels">,
): CodexApprovalState {
  if (session.labels?.["omnigent.codex_native.bypass_sandbox"] === "1") return "bypass";
  const args = session.terminalLaunchArgs ?? [];
  if (args.includes("--dangerously-bypass-approvals-and-sandbox")) return "full-access";

  let sandbox: string | null = null;
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if ((arg === "--sandbox" || arg === "-s") && args[i + 1]) {
      sandbox = args[i + 1];
      i += 1;
    } else if (arg.startsWith("--sandbox=") || arg.startsWith("-s=")) {
      sandbox = arg.slice(arg.indexOf("=") + 1);
    } else if ((arg === "-c" || arg === "--config") && args[i + 1]) {
      const assignment = args[i + 1];
      if (assignment.startsWith("default_permissions=") || assignment.startsWith("sandbox_mode=")) {
        sandbox = assignment.slice(assignment.indexOf("=") + 1).replaceAll(/["']/g, "");
      }
      i += 1;
    }
  }
  if (sandbox?.includes("danger-full-access")) return "full-access";
  if (sandbox?.includes("read-only")) return "read-only";
  return "default";
}
