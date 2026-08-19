import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProfileSwitcher } from "./ProfileSwitcher";

const createProfileMock = vi.hoisted(() => vi.fn());
const configureProtectionMock = vi.hoisted(() => vi.fn());
const deleteProfileMock = vi.hoisted(() => vi.fn());
const unlockProfileMock = vi.hoisted(() => vi.fn());
const ensureHostDirectoryMock = vi.hoisted(() => vi.fn());
const hasBiometricsMock = vi.hoisted(() => vi.fn());
const privatePasscodeMock = vi.hoisted(() => vi.fn());
const removeCredentialMock = vi.hoisted(() => vi.fn());
const updateProfileMock = vi.hoisted(() => vi.fn());
const disableProtectionMock = vi.hoisted(() => vi.fn());
const lockProfileMock = vi.hoisted(() => vi.fn());
const getUnlockTokenMock = vi.hoisted(() => vi.fn());
const clearNotificationActivityMock = vi.hoisted(() => vi.fn());
const setActiveProfileIdMock = vi.hoisted(() => vi.fn());
const profileState = vi.hoisted(() => ({
  profiles: [] as Record<string, unknown>[],
  activeProfile: null as Record<string, unknown> | null,
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [{ host_id: "host-1", name: "This machine", status: "online" }],
  }),
}));
vi.mock("@/lib/profilesApi", () => ({
  useActiveProfile: () => ({
    profiles: profileState.profiles,
    activeProfile: profileState.activeProfile,
    setActiveProfileId: setActiveProfileIdMock,
  }),
  createProfile: createProfileMock,
  configureProfileProtection: configureProtectionMock,
  deleteProfile: deleteProfileMock,
  disableProfileProtection: disableProtectionMock,
  lockProfile: lockProfileMock,
  unlockProfile: unlockProfileMock,
  updateProfile: updateProfileMock,
}));
vi.mock("@/lib/nativeBridge", () => ({
  hasPrivateProfileBiometrics: hasBiometricsMock,
  isIOSShell: () => false,
  privateProfilePasscode: privatePasscodeMock,
  removePrivateProfileCredential: removeCredentialMock,
}));
vi.mock("@/hooks/useHostFilesystem", () => ({
  ensureHostDirectory: ensureHostDirectoryMock,
}));
vi.mock("@/lib/profileUnlock", () => ({
  getProfileUnlockToken: getUnlockTokenMock,
  setProfileUnlockToken: vi.fn(),
}));
vi.mock("@/lib/profileNotificationActivity", () => ({
  clearProfileNotificationActivity: clearNotificationActivityMock,
}));
vi.mock("@/lib/routing", () => ({ useNavigate: () => vi.fn() }));
vi.mock("@/store/chatStore", () => ({ clearAllConversationState: vi.fn() }));

describe("ProfileSwitcher creation defaults", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hasBiometricsMock.mockReturnValue(false);
    ensureHostDirectoryMock.mockImplementation(async (_hostId: string, path: string) => path);
    configureProtectionMock.mockResolvedValue(undefined);
    unlockProfileMock.mockResolvedValue(undefined);
    deleteProfileMock.mockResolvedValue(undefined);
    disableProtectionMock.mockResolvedValue(undefined);
    lockProfileMock.mockResolvedValue(undefined);
    getUnlockTokenMock.mockReturnValue(null);
    const defaultProfile = {
      id: "profile-1",
      name: "Default",
      is_default: true,
      config: {},
      protection: {},
      created_at: 1,
    };
    profileState.profiles = [defaultProfile];
    profileState.activeProfile = defaultProfile;
  });

  it("offers a default host and working directory for every new profile", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProfileSwitcher />
      </QueryClientProvider>,
    );

    fireEvent.pointerDown(screen.getByTestId("profile-switcher"), { button: 0 });
    fireEvent.click(screen.getByText("New profile"));

    expect(screen.getByText("Default host")).toBeInTheDocument();
    expect(screen.getByText("Default working directory")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveClass("max-h-[calc(100dvh-2rem)]");
  });

  it("stores the selected host and working directory on a public profile", async () => {
    createProfileMock.mockResolvedValue({
      id: "profile-2",
      name: "Workshop",
      is_default: false,
      config: { host_id: "host-1", workspace: "/srv/workshop" },
      protection: {},
      created_at: 2,
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProfileSwitcher />
      </QueryClientProvider>,
    );

    fireEvent.pointerDown(screen.getByTestId("profile-switcher"), { button: 0 });
    fireEvent.click(screen.getByText("New profile"));
    fireEvent.change(screen.getByPlaceholderText("Profile name"), {
      target: { value: "Workshop" },
    });
    const hostSelect = screen.getAllByRole("combobox")[0];
    hostSelect.focus();
    fireEvent.keyDown(hostSelect, { key: "ArrowDown", code: "ArrowDown" });
    const hostOption = screen.getByRole("option", { name: "This machine" });
    fireEvent.pointerMove(hostOption, { pointerType: "mouse" });
    fireEvent.pointerDown(hostOption, { button: 0, pointerType: "mouse" });
    fireEvent.pointerUp(hostOption, { button: 0, pointerType: "mouse" });
    fireEvent.change(screen.getByPlaceholderText("/path/to/project"), {
      target: { value: "/srv/workshop" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(createProfileMock).toHaveBeenCalledWith("Workshop", {
        icon: "user",
        color: null,
        config: { host_id: "host-1", workspace: "/srv/workshop" },
      }),
    );
  });

  it("creates a missing private root on the selected host before the profile", async () => {
    createProfileMock.mockResolvedValue({
      id: "profile-private",
      name: "Private",
      is_default: false,
      config: { host_id: "host-1", workspace: "/srv/private" },
      protection: {},
      created_at: 2,
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProfileSwitcher />
      </QueryClientProvider>,
    );

    fireEvent.pointerDown(screen.getByTestId("profile-switcher"), { button: 0 });
    fireEvent.click(screen.getByText("New profile"));
    fireEvent.change(screen.getByPlaceholderText("Profile name"), {
      target: { value: "Private" },
    });
    const hostSelect = screen.getAllByRole("combobox")[0];
    hostSelect.focus();
    fireEvent.keyDown(hostSelect, { key: "ArrowDown", code: "ArrowDown" });
    const hostOption = screen.getByRole("option", { name: "This machine" });
    fireEvent.pointerMove(hostOption, { pointerType: "mouse" });
    fireEvent.pointerDown(hostOption, { button: 0, pointerType: "mouse" });
    fireEvent.pointerUp(hostOption, { button: 0, pointerType: "mouse" });
    fireEvent.change(screen.getByPlaceholderText("/path/to/project"), {
      target: { value: "/srv/private" },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.change(screen.getByLabelText("Passcode"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(ensureHostDirectoryMock).toHaveBeenCalledWith("host-1", "/srv/private"),
    );
    expect(ensureHostDirectoryMock.mock.invocationCallOrder[0]).toBeLessThan(
      createProfileMock.mock.invocationCallOrder[0],
    );
  });

  it("deletes a newly protected profile when biometric setup is cancelled", async () => {
    hasBiometricsMock.mockReturnValue(true);
    privatePasscodeMock.mockResolvedValue(null);
    createProfileMock.mockResolvedValue({
      id: "profile-private",
      name: "Private",
      is_default: false,
      config: { host_id: "host-1", workspace: "/srv/private" },
      protection: {},
      created_at: 2,
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProfileSwitcher />
      </QueryClientProvider>,
    );

    fireEvent.pointerDown(screen.getByTestId("profile-switcher"), { button: 0 });
    fireEvent.click(screen.getByText("New profile"));
    fireEvent.change(screen.getByPlaceholderText("Profile name"), {
      target: { value: "Private" },
    });
    const hostSelect = screen.getAllByRole("combobox")[0];
    hostSelect.focus();
    fireEvent.keyDown(hostSelect, { key: "ArrowDown", code: "ArrowDown" });
    const hostOption = screen.getByRole("option", { name: "This machine" });
    fireEvent.pointerMove(hostOption, { pointerType: "mouse" });
    fireEvent.pointerDown(hostOption, { button: 0, pointerType: "mouse" });
    fireEvent.pointerUp(hostOption, { button: 0, pointerType: "mouse" });
    fireEvent.change(screen.getByPlaceholderText("/path/to/project"), {
      target: { value: "/srv/private" },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.change(screen.getByLabelText("Passcode"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(deleteProfileMock).toHaveBeenCalledWith("profile-private"));
    expect(removeCredentialMock).toHaveBeenCalledWith("profile-private");
    expect(screen.getByText("Could not create this profile.")).toBeInTheDocument();
  });

  it("keeps an explicitly locked profile locked until the user selects it", async () => {
    const privateProfile = {
      id: "profile-private",
      name: "Private",
      is_default: true,
      config: { host_id: "host-1", workspace: "/srv/private" },
      protection: { lock: "passcode", notification_content: "generic" },
      created_at: 1,
    };
    profileState.profiles = [privateProfile];
    profileState.activeProfile = privateProfile;
    getUnlockTokenMock.mockReturnValue("unlocked-token");

    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProfileSwitcher />
      </QueryClientProvider>,
    );
    fireEvent.pointerDown(screen.getByTestId("profile-switcher"), { button: 0 });
    fireEvent.click(screen.getByText("Lock profile"));

    await waitFor(() => expect(lockProfileMock).toHaveBeenCalledWith("profile-private"));
    expect(clearNotificationActivityMock).toHaveBeenCalledWith("profile-private");
    expect(privatePasscodeMock).not.toHaveBeenCalled();
    expect(screen.queryByText("Unlock private profile")).not.toBeInTheDocument();
  });

  it("rolls profile metadata back when protection configuration fails", async () => {
    const privateProfile = {
      id: "profile-1",
      name: "Default",
      icon: "lock",
      is_default: true,
      config: { host_id: "host-1", workspace: "/srv/private" },
      protection: { lock: "passcode", notification_content: "generic" },
      created_at: 1,
    };
    profileState.profiles = [privateProfile];
    profileState.activeProfile = privateProfile;
    getUnlockTokenMock.mockReturnValue("unlocked-token");
    updateProfileMock
      .mockResolvedValueOnce({
        ...profileState.activeProfile,
        name: "Renamed",
        icon: "lock",
        config: privateProfile.config,
      })
      .mockResolvedValueOnce(profileState.activeProfile);
    configureProtectionMock.mockRejectedValueOnce(new Error("protection failed"));

    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProfileSwitcher />
      </QueryClientProvider>,
    );
    fireEvent.pointerDown(screen.getByTestId("profile-switcher"), { button: 0 });
    fireEvent.click(screen.getByText("Profile settings"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Renamed" } });
    const hostSelect = screen.getAllByRole("combobox")[0];
    hostSelect.focus();
    fireEvent.keyDown(hostSelect, { key: "ArrowDown", code: "ArrowDown" });
    const hostOption = screen.getByRole("option", { name: "This machine" });
    fireEvent.pointerDown(hostOption, { button: 0, pointerType: "mouse" });
    fireEvent.pointerUp(hostOption, { button: 0, pointerType: "mouse" });
    fireEvent.change(screen.getByPlaceholderText("/path/to/project"), {
      target: { value: "/srv/private" },
    });
    if (!screen.queryByText("New passcode (optional)")) {
      fireEvent.click(screen.getByRole("switch", { name: "Private profile" }));
    }
    fireEvent.change(await screen.findByLabelText(/New passcode/), {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(updateProfileMock).toHaveBeenCalledTimes(2));
    expect(updateProfileMock).toHaveBeenLastCalledWith("profile-1", {
      name: "Default",
      icon: "lock",
      color: undefined,
      config: privateProfile.config,
    });
    expect(screen.getByText("Could not save profile settings.")).toBeInTheDocument();
  });

  it("treats biometric credential storage failure as non-fatal after protection saves", async () => {
    const privateProfile = {
      id: "profile-1",
      name: "Default",
      icon: "lock",
      is_default: true,
      config: { host_id: "host-1", workspace: "/srv/private" },
      protection: { lock: "passcode", notification_content: "generic" },
      created_at: 1,
    };
    profileState.profiles = [privateProfile];
    profileState.activeProfile = privateProfile;
    getUnlockTokenMock.mockReturnValue("unlocked-token");
    updateProfileMock.mockResolvedValue({
      ...privateProfile,
      name: "Default",
      icon: "lock",
      config: privateProfile.config,
    });
    privatePasscodeMock.mockRejectedValueOnce(new Error("cancelled"));

    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProfileSwitcher />
      </QueryClientProvider>,
    );
    fireEvent.pointerDown(screen.getByTestId("profile-switcher"), { button: 0 });
    fireEvent.click(screen.getByText("Profile settings"));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveClass("max-h-[calc(100dvh-2rem)]");
    expect(dialog.querySelector(".overflow-y-auto")).not.toBeNull();
    const hostSelect = screen.getAllByRole("combobox")[0];
    hostSelect.focus();
    fireEvent.keyDown(hostSelect, { key: "ArrowDown", code: "ArrowDown" });
    const hostOption = screen.getByRole("option", { name: "This machine" });
    fireEvent.pointerDown(hostOption, { button: 0, pointerType: "mouse" });
    fireEvent.pointerUp(hostOption, { button: 0, pointerType: "mouse" });
    fireEvent.change(screen.getByPlaceholderText("/path/to/project"), {
      target: { value: "/srv/private" },
    });
    if (!screen.queryByText("New passcode (optional)")) {
      fireEvent.click(screen.getByRole("switch", { name: "Private profile" }));
    }
    fireEvent.change(await screen.findByLabelText(/New passcode/), {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(configureProtectionMock).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.queryByText("Profile settings")).not.toBeInTheDocument());
    expect(updateProfileMock).toHaveBeenCalledOnce();
  });
});
