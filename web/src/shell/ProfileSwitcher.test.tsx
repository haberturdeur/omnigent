import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProfileSwitcher } from "./ProfileSwitcher";

const mocks = vi.hoisted(() => ({ save: vi.fn(), select: vi.fn(), remove: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: () => ({ data: [] }) }));
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => ({ data: [] }) }));
vi.mock("@/lib/activeProfile", () => ({
  selectProfile: mocks.select,
  profileRoute: (id: string) => `/?profile_id=${id}`,
}));
vi.mock("@/lib/profilesApi", () => ({
  useProfiles: () => ({
    data: [{ id: "personal", name: "Personal", config: {}, is_default: true }],
    activeProfile: { id: "personal", name: "Personal", config: {}, is_default: true },
  }),
  saveProfile: mocks.save,
  deleteProfile: mocks.remove,
}));
function show() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ProfileSwitcher />
    </QueryClientProvider>,
  );
}
describe("ordinary profiles", () => {
  beforeEach(() => vi.clearAllMocks());
  it("creates and selects a profile without any lock controls", async () => {
    mocks.save.mockResolvedValue({ id: "work" });
    show();
    fireEvent.click(screen.getByRole("button", { name: "Create profile" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Work" } });
    fireEvent.change(screen.getByLabelText("Default working directory"), {
      target: { value: "/work" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.select).toHaveBeenCalledWith("work"));
    expect(mocks.save).toHaveBeenCalledWith(
      null,
      expect.objectContaining({
        name: "Work",
        config: expect.objectContaining({ workspace: "/work" }),
      }),
    );
    expect(screen.queryByLabelText(/passcode/i)).not.toBeInTheDocument();
  });
  it("keeps the form open and displays failed saves", async () => {
    mocks.save.mockRejectedValue(new Error("A profile named Work already exists"));
    show();
    fireEvent.click(screen.getByRole("button", { name: "Create profile" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Work" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
    expect(mocks.select).not.toHaveBeenCalled();
  });
  it("offers a separate-tab link and protects the default profile from deletion", () => {
    show();
    expect(screen.getByRole("link", { name: "Open profile in new tab" })).toHaveAttribute(
      "target",
      "_blank",
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.queryByRole("button", { name: "Delete empty profile" })).not.toBeInTheDocument();
  });
});
