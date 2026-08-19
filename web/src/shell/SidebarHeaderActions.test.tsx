import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import { SidebarHeaderActions } from "./SidebarHeaderActions";

const { openServerSettingsSpy } = vi.hoisted(() => ({ openServerSettingsSpy: vi.fn() }));

vi.mock("@/lib/nativeBridge", () => ({
  isAndroidShell: () => true,
  openNativeServerSettings: openServerSettingsSpy,
}));

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
