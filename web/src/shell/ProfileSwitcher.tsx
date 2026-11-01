import { useHosts } from "@/hooks/useHosts";
import { useAgents } from "@/hooks/useAgents";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useProfiles, saveProfile, deleteProfile } from "@/lib/profilesApi";
import { profileRoute, selectProfile } from "@/lib/activeProfile";

export function ProfileSwitcher() {
  const { data: profiles, activeProfile, error } = useProfiles();
  const client = useQueryClient();
  const { data: hosts } = useHosts();
  const { data: agents } = useAgents();
  const [editing, setEditing] = useState<"new" | "edit" | null>(null);
  const [name, setName] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [host, setHost] = useState("");
  const [agent, setAgent] = useState("");
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  function open(mode: "new" | "edit") {
    const current = mode === "edit" ? activeProfile : undefined;
    setName(current?.name ?? "");
    setWorkspace(current?.config.workspace ?? "");
    setHost(current?.config.host_id ?? "");
    setAgent(current?.config.agent_id ?? "");
    setFailure("");
    setEditing(mode);
  }
  async function save() {
    setBusy(true);
    setFailure("");
    try {
      const profile = await saveProfile(editing === "edit" ? activeProfile!.id : null, {
        name: name.trim(),
        config: {
          ...(editing === "edit" ? activeProfile?.config : {}),
          workspace,
          host_id: host,
          agent_id: agent,
        },
      });
      await client.invalidateQueries({ queryKey: ["profiles"] });
      selectProfile(profile.id);
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="px-3 py-2">
      <div className="flex items-center gap-1">
        <select
          aria-label="Profile"
          value={activeProfile?.id ?? ""}
          onChange={(e) => selectProfile(e.target.value)}
          className="min-w-0 flex-1 rounded border bg-background p-1 text-sm"
        >
          {(profiles ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <Button variant="ghost" size="sm" onClick={() => open("new")} aria-label="Create profile">
          +
        </Button>
        <Button variant="ghost" size="sm" disabled={!activeProfile} onClick={() => open("edit")}>
          Edit
        </Button>
      </div>
      {activeProfile && (
        <a
          className="text-xs text-muted-foreground"
          href={profileRoute(activeProfile.id)}
          target="_blank"
          rel="noopener noreferrer"
        >
          Open profile in new tab
        </a>
      )}
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error.message}
        </p>
      )}
      <Dialog
        open={editing !== null}
        onOpenChange={(isOpen) => {
          if (!isOpen) setEditing(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing === "new" ? "Create profile" : "Edit profile"}</DialogTitle>
          </DialogHeader>
          <label>
            Name
            <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={100} />
          </label>
          <label>
            Default working directory
            <Input value={workspace} onChange={(e) => setWorkspace(e.target.value)} />
          </label>
          <label>
            Default host
            <select
              aria-label="Default host"
              value={host}
              onChange={(e) => setHost(e.target.value)}
              className="w-full rounded border bg-background p-2"
            >
              <option value="">Use current default</option>
              {(hosts ?? []).map((h) => (
                <option key={h.host_id} value={h.host_id}>
                  {h.name || h.host_id}
                </option>
              ))}
            </select>
          </label>
          <label>
            Default agent
            <select
              aria-label="Default agent"
              value={agent}
              onChange={(e) => setAgent(e.target.value)}
              className="w-full rounded border bg-background p-2"
            >
              <option value="">Use current default</option>
              {(agents ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </label>
          <p className="text-sm text-muted-foreground">
            Projects can override these defaults. Profiles organize sessions and do not restrict
            filesystem access.
          </p>
          {failure && (
            <p role="alert" className="text-sm text-destructive">
              {failure}
            </p>
          )}
          <Button disabled={busy || !name.trim()} onClick={() => void save()}>
            Save
          </Button>
          {editing === "edit" && activeProfile && !activeProfile.is_default && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await deleteProfile(activeProfile.id);
                  selectProfile("default");
                } catch (e) {
                  setFailure(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              Delete empty profile
            </Button>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
