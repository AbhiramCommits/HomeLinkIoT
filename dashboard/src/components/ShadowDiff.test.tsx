import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { ShadowDiff, shadowDiffRows } from "./ShadowDiff";

describe("ShadowDiff", () => {
  test("marks differing, equal, and missing leaves", () => {
    const rows = shadowDiffRows(
      { power_level: 4, schedule: { days: [1, 3], hour: 9 } },
      { power_level: 2, schedule: { days: [1, 3], hour: 9 } },
    );
    const byPath = Object.fromEntries(rows.map((row) => [row.path, row.status]));
    expect(byPath.power_level).toBe("different");
    expect(byPath["schedule.days"]).toBe("same");
    expect(byPath["schedule.hour"]).toBe("same");
  });

  test("renders a diff table with statuses", () => {
    render(
      <ShadowDiff
        desired={{ power_level: 4, schedule: { days: [1, 3] } }}
        reported={{ power_level: 2 }}
      />,
    );
    expect(screen.getByTestId("shadow-diff")).toBeInTheDocument();
    expect(screen.getByTestId("diff-row-power_level")).toBeInTheDocument();
    expect(screen.getByTestId("status-power_level")).toHaveTextContent("different");
    expect(screen.getByTestId("status-schedule.days")).toHaveTextContent("missing");
    expect(screen.getByText("2 differences")).toBeInTheDocument();
  });

  test("handles empty shadows", () => {
    render(<ShadowDiff desired={null} reported={null} />);
    expect(screen.getByText(/No desired or reported keys/)).toBeInTheDocument();
  });
});
