import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { TooltipProvider } from "@/components/ui/tooltip";

import { SidebarHeaderActions } from "./SidebarHeaderActions";

const { openServerSettingsSpy } = vi.hoisted(() => ({ openServerSettingsSpy: vi.fn() }));

vi.mock("@/lib/nativeBridge", () => ({
  hasNativeServerSettings: () => true,
  openNativeServerSettings: openServerSettingsSpy,
}));

afterEach(cleanup);

describe("SidebarHeaderActions shortcut hints", () => {
  it.each([
    {
      control: "Search",
      tooltip: "Search",
      keys: [MOD_KEY, "K"],
      aria: MOD_KEY === "⌘" ? "Meta+K" : "Control+K",
    },
    {
      control: "Settings",
      tooltip: "Settings",
      keys: [MOD_KEY, ALT_KEY, ","],
      aria: MOD_KEY === "⌘" ? "Meta+Alt+," : "Control+Alt+,",
    },
    {
      control: "Close sidebar",
      tooltip: "Collapse sidebar",
      keys: [MOD_KEY, ALT_KEY, "["],
      aria: MOD_KEY === "⌘" ? "Meta+Alt+[" : "Control+Alt+[",
    },
  ])("shows the $control shortcut in its tooltip", async ({ control, tooltip, keys, aria }) => {
    render(
      <MemoryRouter>
        <TooltipProvider delayDuration={0}>
          <SidebarHeaderActions expanded onToggle={vi.fn()} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    const trigger = screen.getByLabelText(control);
    expect(trigger).toHaveAttribute("aria-keyshortcuts", aria);
    fireEvent.focus(trigger);
    const content = await screen.findByRole("tooltip");

    expect(content).toHaveTextContent(tooltip);
    expect(
      Array.from(content.querySelectorAll('[data-slot="kbd"]'), (key) => key.textContent),
    ).toEqual(keys);
  });
});

describe("SidebarHeaderActions on Android", () => {
  it("opens server settings directly from the sidebar header", () => {
    render(
      <MemoryRouter>
        <TooltipProvider>
          <SidebarHeaderActions expanded onToggle={() => undefined} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Server and app settings" }));

    expect(openServerSettingsSpy).toHaveBeenCalledOnce();
  });
});
