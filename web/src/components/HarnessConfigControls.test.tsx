import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DescribedSelect } from "./HarnessConfigControls";

describe("DescribedSelect", () => {
  it("renders disabled options as unavailable and does not select them", () => {
    const onValueChange = vi.fn();
    render(
      <DescribedSelect
        value="default"
        onValueChange={onValueChange}
        options={[
          { value: "default", label: "Default", description: "Use the harness default" },
          {
            value: "blocked",
            label: "Unavailable",
            description: "Not supported for this session",
            disabled: true,
          },
        ]}
        testId="config-select"
        ariaLabel="Configuration"
      />,
    );

    fireEvent.click(screen.getByTestId("config-select"));
    const unavailable = screen.getByRole("option", { name: "Unavailable" });
    expect(unavailable).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(unavailable);
    expect(onValueChange).not.toHaveBeenCalled();
    expect(screen.getByTestId("config-select")).toHaveTextContent("Default");
  });
});
