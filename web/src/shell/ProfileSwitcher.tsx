import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckIcon, LockKeyholeIcon, PlusIcon, Settings2Icon, UserRoundIcon } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useHosts } from "@/hooks/useHosts";
import { useAgents } from "@/hooks/useAgents";
import { ensureHostDirectory } from "@/hooks/useHostFilesystem";
import { getProfileUnlockToken, setProfileUnlockToken } from "@/lib/profileUnlock";
import { clearProfileNotificationActivity } from "@/lib/profileNotificationActivity";
import {
  hasPrivateProfileBiometrics,
  privateProfilePasscode,
  removePrivateProfileCredential,
} from "@/lib/nativeBridge";
import {
  configureProfileProtection,
  createProfile,
  deleteProfile,
  disableProfileProtection,
  lockProfile,
  unlockProfile,
  updateProfile,
  useActiveProfile,
} from "@/lib/profilesApi";
import { useNavigate } from "@/lib/routing";
import { clearAllConversationState } from "@/store/chatStore";

function ProfileHostSelect({
  value,
  onValueChange,
}: {
  value: string;
  onValueChange: (value: string) => void;
}) {
  const hosts = useHosts();
  return (
    <Select value={value} onValueChange={onValueChange}>
      <SelectTrigger className="w-full">
        <SelectValue placeholder="No default" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="__none__">No default</SelectItem>
        {(hosts.data ?? []).map((host) => (
          <SelectItem key={host.host_id} value={host.host_id}>
            {host.name}
            {host.status !== "online" ? " (offline)" : ""}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function ProfileSwitcher() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { profiles, activeProfile, setActiveProfileId } = useActiveProfile();
  const [createOpen, setCreateOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [name, setName] = useState("");
  const [isPrivate, setIsPrivate] = useState(false);
  const [createHostId, setCreateHostId] = useState("__none__");
  const [createRoot, setCreateRoot] = useState("");
  const [createAgentId, setCreateAgentId] = useState("__none__");
  const [createPasscode, setCreatePasscode] = useState("");
  const [settingsName, setSettingsName] = useState("");
  const [hostId, setHostId] = useState("__none__");
  const [workspace, setWorkspace] = useState("");
  const [useWorktree, setUseWorktree] = useState(false);
  const [baseBranch, setBaseBranch] = useState("");
  const [agentId, setAgentId] = useState("__none__");
  const [settingsPrivate, setSettingsPrivate] = useState(false);
  const [settingsPasscode, setSettingsPasscode] = useState("");
  const [unlockProfileId, setUnlockProfileId] = useState<string | null>(null);
  const [unlockPasscode, setUnlockPasscode] = useState("");
  const [biometricAttemptedFor, setBiometricAttemptedFor] = useState<string | null>(null);
  const [manuallyLockedProfileId, setManuallyLockedProfileId] = useState<string | null>(null);
  const [bridgeRetry, setBridgeRetry] = useState(0);
  const agents = useAgents({ enabled: createOpen || settingsOpen });
  const purgeProfileState = useCallback(() => {
    clearAllConversationState();
    queryClient.clear();
  }, [queryClient]);

  const switchProfile = useCallback(
    (id: string) => {
      const target = profiles.find((profile) => profile.id === id);
      if (target?.protection.lock && !getProfileUnlockToken(id)) {
        setManuallyLockedProfileId(null);
        setUnlockProfileId(id);
        return;
      }
      if (id === activeProfile?.id) return;
      if (activeProfile) clearProfileNotificationActivity(activeProfile.id);
      setActiveProfileId(id);
      purgeProfileState();
      navigate("/");
    },
    [activeProfile, navigate, profiles, purgeProfileState, setActiveProfileId],
  );

  useEffect(() => {
    if (
      activeProfile?.protection.lock &&
      !getProfileUnlockToken(activeProfile.id) &&
      manuallyLockedProfileId !== activeProfile.id &&
      !createOpen &&
      !settingsOpen
    ) {
      setUnlockProfileId(activeProfile.id);
    }
  }, [activeProfile, createOpen, manuallyLockedProfileId, settingsOpen]);

  useEffect(() => {
    if (!unlockProfileId || biometricAttemptedFor === unlockProfileId) return;
    if (!hasPrivateProfileBiometrics()) {
      const timer = window.setTimeout(() => setBridgeRetry((value) => value + 1), 250);
      return () => window.clearTimeout(timer);
    }
    setBiometricAttemptedFor(unlockProfileId);
    void privateProfilePasscode(unlockProfileId).then((token) => {
      if (!token) return;
      setProfileUnlockToken(unlockProfileId, token);
      setUnlockProfileId(null);
      setUnlockPasscode("");
      setBiometricAttemptedFor(null);
      setManuallyLockedProfileId(null);
      void queryClient.invalidateQueries();
      if (unlockProfileId !== activeProfile?.id) switchProfile(unlockProfileId);
      else navigate("/");
    });
    return undefined;
  }, [
    activeProfile?.id,
    biometricAttemptedFor,
    bridgeRetry,
    navigate,
    queryClient,
    switchProfile,
    unlockProfileId,
  ]);

  const createMutation = useMutation({
    mutationFn: async () => {
      let root = createRoot.trim();
      if (isPrivate && createHostId !== "__none__" && root) {
        root = await ensureHostDirectory(createHostId, root);
      }
      const config = {
        ...(createHostId !== "__none__" ? { host_id: createHostId } : {}),
        ...(root ? { workspace: root } : {}),
        ...(createAgentId !== "__none__" ? { agent_id: createAgentId } : {}),
      };
      const profile = await createProfile(name.trim(), {
        icon: isPrivate ? "lock" : "user",
        color: null,
        config,
      });
      if (isPrivate) {
        try {
          await configureProfileProtection(profile.id, createPasscode, [root]);
          await unlockProfile(profile.id, createPasscode);
          if (hasPrivateProfileBiometrics()) {
            const nativeToken = await privateProfilePasscode(profile.id, createPasscode);
            if (!nativeToken) throw new Error("Biometric credential setup was cancelled");
            setProfileUnlockToken(profile.id, nativeToken);
          }
        } catch (error) {
          await unlockProfile(profile.id, createPasscode).catch(() => undefined);
          try {
            await deleteProfile(profile.id);
          } catch {
            // The original protection error is the useful one to surface.
          }
          removePrivateProfileCredential(profile.id);
          throw error;
        }
        profile.protection = { lock: "passcode", notification_content: "generic" };
      }
      return profile;
    },
    onSuccess: (profile) => {
      queryClient.setQueryData(["profiles"], (old: unknown) =>
        Array.isArray(old) ? [...old, profile] : [profile],
      );
      setCreateOpen(false);
      setName("");
      setIsPrivate(false);
      setCreateHostId("__none__");
      setCreateRoot("");
      setCreateAgentId("__none__");
      setCreatePasscode("");
      switchProfile(profile.id);
    },
  });

  useEffect(() => {
    if (!settingsOpen || !activeProfile) return;
    setSettingsName(activeProfile.name);
    setHostId(activeProfile.config.host_id ?? "__none__");
    setWorkspace(activeProfile.config.workspace ?? "");
    setUseWorktree(activeProfile.config.use_worktree ?? false);
    setBaseBranch(activeProfile.config.base_branch ?? "");
    setAgentId(activeProfile.config.agent_id ?? "__none__");
    setSettingsPrivate(Boolean(activeProfile.protection?.lock));
    setSettingsPasscode("");
  }, [activeProfile, settingsOpen]);

  const settingsMutation = useMutation({
    mutationFn: async () => {
      if (!activeProfile) throw new Error("No active profile");
      const wasPrivate = Boolean(activeProfile.protection?.lock);
      const config = {
        ...(hostId !== "__none__" ? { host_id: hostId } : {}),
        ...(workspace.trim() ? { workspace: workspace.trim() } : {}),
        ...(useWorktree ? { use_worktree: true } : {}),
        ...(useWorktree && baseBranch.trim() ? { base_branch: baseBranch.trim() } : {}),
        ...(agentId !== "__none__" ? { agent_id: agentId } : {}),
      };
      const profile = await updateProfile(activeProfile.id, {
        name: settingsName.trim(),
        icon: settingsPrivate ? "lock" : "user",
        config,
      });
      try {
        if (settingsPrivate) {
          await configureProfileProtection(activeProfile.id, settingsPasscode || null, [
            workspace.trim(),
          ]);
        } else if (wasPrivate) {
          await disableProfileProtection(activeProfile.id);
          removePrivateProfileCredential(activeProfile.id);
        }
      } catch (error) {
        await updateProfile(activeProfile.id, {
          name: activeProfile.name,
          icon: activeProfile.icon,
          color: activeProfile.color,
          config: activeProfile.config,
        }).catch(() => undefined);
        throw error;
      }
      if (settingsPrivate && settingsPasscode) {
        try {
          const nativeToken = await privateProfilePasscode(activeProfile.id, settingsPasscode);
          if (nativeToken) setProfileUnlockToken(activeProfile.id, nativeToken);
          else await unlockProfile(activeProfile.id, settingsPasscode);
        } catch {
          setProfileUnlockToken(activeProfile.id, null);
        }
      }
      if (settingsPrivate) {
        profile.protection = { lock: "passcode", notification_content: "generic" };
      } else if (wasPrivate) {
        profile.protection = {};
      }
      return profile;
    },
    onSuccess: (profile) => {
      queryClient.setQueryData(["profiles"], (old: unknown) =>
        Array.isArray(old)
          ? old.map((candidate) =>
              typeof candidate === "object" &&
              candidate !== null &&
              "id" in candidate &&
              candidate.id === profile.id
                ? profile
                : candidate,
            )
          : [profile],
      );
      setSettingsOpen(false);
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (name.trim()) createMutation.mutate();
  };

  const unlockMutation = useMutation({
    mutationFn: async () => {
      if (!unlockProfileId) throw new Error("No profile selected");
      await unlockProfile(unlockProfileId, unlockPasscode);
      const nativeToken = await privateProfilePasscode(unlockProfileId, unlockPasscode);
      if (nativeToken) setProfileUnlockToken(unlockProfileId, nativeToken);
      return unlockProfileId;
    },
    onSuccess: (id) => {
      setUnlockProfileId(null);
      setUnlockPasscode("");
      setBiometricAttemptedFor(null);
      setManuallyLockedProfileId(null);
      if (id === activeProfile?.id) {
        void queryClient.invalidateQueries();
        navigate("/");
      } else {
        switchProfile(id);
      }
    },
  });

  if (!activeProfile) return <div className="mx-2 h-8 animate-pulse rounded-md bg-muted" />;
  const activePrivate = Boolean(activeProfile.protection?.lock);

  return (
    <>
      <div className="px-2 pb-1">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              className="h-8 w-full justify-start gap-2 px-2 font-normal"
              data-testid="profile-switcher"
            >
              {activePrivate ? (
                <LockKeyholeIcon className="size-3.5 text-muted-foreground" />
              ) : (
                <UserRoundIcon className="size-3.5 text-muted-foreground" />
              )}
              <span className="min-w-0 flex-1 truncate text-left">{activeProfile.name}</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-56">
            {profiles.map((profile) => {
              const privateProfile = Boolean(profile.protection?.lock);
              return (
                <DropdownMenuItem key={profile.id} onSelect={() => switchProfile(profile.id)}>
                  {privateProfile ? (
                    <LockKeyholeIcon className="size-3.5" />
                  ) : (
                    <UserRoundIcon className="size-3.5" />
                  )}
                  <span className="min-w-0 flex-1 truncate">{profile.name}</span>
                  {profile.id === activeProfile.id && <CheckIcon className="size-3.5" />}
                </DropdownMenuItem>
              );
            })}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => setSettingsOpen(true)}>
              <Settings2Icon className="size-3.5" />
              Profile settings
            </DropdownMenuItem>
            {activePrivate && getProfileUnlockToken(activeProfile.id) && (
              <DropdownMenuItem
                onSelect={() => {
                  clearProfileNotificationActivity(activeProfile.id);
                  setManuallyLockedProfileId(activeProfile.id);
                  void lockProfile(activeProfile.id).finally(() => {
                    purgeProfileState();
                    setBiometricAttemptedFor(null);
                    setUnlockProfileId(null);
                    navigate("/");
                  });
                }}
              >
                <LockKeyholeIcon className="size-3.5" />
                Lock profile
              </DropdownMenuItem>
            )}
            <DropdownMenuItem onSelect={() => setCreateOpen(true)}>
              <PlusIcon className="size-3.5" />
              New profile
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-hidden">
          <form className="flex min-h-0 flex-col" onSubmit={submit}>
            <DialogHeader>
              <DialogTitle>New profile</DialogTitle>
            </DialogHeader>
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto py-4 pr-1">
              <Input
                autoFocus
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Profile name"
                maxLength={100}
              />
              <label className="block space-y-1 text-sm">
                <span className="font-medium">Default host</span>
                <ProfileHostSelect value={createHostId} onValueChange={setCreateHostId} />
              </label>
              <label className="block space-y-1 text-sm">
                <span className="font-medium">Default working directory</span>
                <Input
                  value={createRoot}
                  onChange={(event) => setCreateRoot(event.target.value)}
                  placeholder="/path/to/project"
                  disabled={createHostId === "__none__"}
                />
                {isPrivate && (
                  <span className="text-xs text-muted-foreground">
                    Other profiles will see this directory masked by bubblewrap.
                  </span>
                )}
              </label>
              <label className="block space-y-1 text-sm">
                <span className="font-medium">Default agent</span>
                <Select value={createAgentId} onValueChange={setCreateAgentId}>
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="No default" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">No default</SelectItem>
                    {(agents.data ?? []).map((agent) => (
                      <SelectItem key={agent.id} value={agent.id}>
                        {agent.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={isPrivate}
                  onChange={(event) => setIsPrivate(event.target.checked)}
                  className="mt-0.5"
                />
                <span>
                  <span className="block font-medium">Private profile</span>
                  <span className="text-muted-foreground">
                    Hide notification contents and mark the profile for device-lock capable clients.
                  </span>
                </span>
              </label>
              {isPrivate && (
                <div className="space-y-3 rounded-md border p-3">
                  <label className="block space-y-1 text-sm">
                    <span className="font-medium">Passcode</span>
                    <Input
                      type="password"
                      value={createPasscode}
                      onChange={(event) => setCreatePasscode(event.target.value)}
                      autoComplete="new-password"
                    />
                  </label>
                </div>
              )}
              {createMutation.isError && (
                <p className="text-sm text-destructive">Could not create this profile.</p>
              )}
            </div>
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => setCreateOpen(false)}>
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  !name.trim() ||
                  createMutation.isPending ||
                  (isPrivate &&
                    (createHostId === "__none__" || !createRoot.trim() || !createPasscode))
                }
              >
                {createMutation.isPending ? "Creating…" : "Create"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-hidden">
          <form
            className="flex min-h-0 flex-col"
            onSubmit={(event) => {
              event.preventDefault();
              if (settingsName.trim()) settingsMutation.mutate();
            }}
          >
            <DialogHeader>
              <DialogTitle>Profile settings</DialogTitle>
            </DialogHeader>
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto py-4 pr-1">
              <label className="block space-y-1 text-sm">
                <span className="font-medium">Name</span>
                <Input
                  value={settingsName}
                  onChange={(event) => setSettingsName(event.target.value)}
                  maxLength={100}
                />
              </label>
              <label className="block space-y-1 text-sm">
                <span className="font-medium">Default host</span>
                <ProfileHostSelect value={hostId} onValueChange={setHostId} />
              </label>
              <label className="block space-y-1 text-sm">
                <span className="font-medium">Default working directory</span>
                <Input
                  value={workspace}
                  onChange={(event) => setWorkspace(event.target.value)}
                  placeholder="/path/to/project"
                  disabled={hostId === "__none__"}
                />
              </label>
              <label className="block space-y-1 text-sm">
                <span className="font-medium">Default agent</span>
                <Select value={agentId} onValueChange={setAgentId}>
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="No default" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">No default</SelectItem>
                    {(agents.data ?? []).map((agent) => (
                      <SelectItem key={agent.id} value={agent.id}>
                        {agent.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
              <label className="flex items-center justify-between gap-4 text-sm">
                <span>
                  <span className="block font-medium">Random worktree</span>
                  <span className="text-muted-foreground">Use a fresh worktree for new chats</span>
                </span>
                <Switch checked={useWorktree} onCheckedChange={setUseWorktree} />
              </label>
              {useWorktree && (
                <label className="block space-y-1 text-sm">
                  <span className="font-medium">Default base branch</span>
                  <Input
                    value={baseBranch}
                    onChange={(event) => setBaseBranch(event.target.value)}
                    placeholder="main"
                  />
                </label>
              )}
              <label className="flex items-start justify-between gap-4 text-sm">
                <span>
                  <span className="block font-medium">Private profile</span>
                  <span className="text-muted-foreground">
                    Hide notification contents and request a device lock in capable clients.
                  </span>
                </span>
                <Switch
                  aria-label="Private profile"
                  checked={settingsPrivate}
                  onCheckedChange={setSettingsPrivate}
                />
              </label>
              {settingsPrivate && (
                <label className="block space-y-1 text-sm">
                  <span className="font-medium">
                    {activePrivate ? "New passcode (optional)" : "Passcode"}
                  </span>
                  <Input
                    type="password"
                    value={settingsPasscode}
                    onChange={(event) => setSettingsPasscode(event.target.value)}
                    autoComplete="new-password"
                  />
                  <span className="text-xs text-muted-foreground">
                    The default working directory is the profile's protected root.
                  </span>
                </label>
              )}
              {settingsMutation.isError && (
                <p className="text-sm text-destructive">Could not save profile settings.</p>
              )}
            </div>
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => setSettingsOpen(false)}>
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  !settingsName.trim() ||
                  settingsMutation.isPending ||
                  (settingsPrivate && (!workspace.trim() || (!activePrivate && !settingsPasscode)))
                }
              >
                {settingsMutation.isPending ? "Saving…" : "Save"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={unlockProfileId !== null}
        onOpenChange={(open) => {
          if (!open) {
            setUnlockProfileId(null);
            setUnlockPasscode("");
            setBiometricAttemptedFor(null);
          }
        }}
      >
        <DialogContent>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (unlockPasscode) unlockMutation.mutate();
            }}
          >
            <DialogHeader>
              <DialogTitle>Unlock private profile</DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-4">
              <Input
                autoFocus
                type="password"
                value={unlockPasscode}
                onChange={(event) => setUnlockPasscode(event.target.value)}
                autoComplete="current-password"
                placeholder="Passcode"
              />
              {unlockMutation.isError && (
                <p className="text-sm text-destructive">The profile could not be unlocked.</p>
              )}
            </div>
            <DialogFooter>
              <Button type="submit" disabled={!unlockPasscode || unlockMutation.isPending}>
                {unlockMutation.isPending ? "Unlocking…" : "Unlock"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
