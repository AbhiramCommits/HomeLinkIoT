import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { LineChart } from "./LineChart";

describe("LineChart", () => {
  test("renders a series path with one point per sample", () => {
    render(
      <LineChart
        title="Motor current"
        unit="A"
        series={[
          {
            label: "motor current",
            color: "#3fb59a",
            points: [
              { x: 1000, y: 1.2 },
              { x: 2000, y: 1.4 },
              { x: 3000, y: 1.1 },
            ],
          },
        ]}
      />,
    );
    expect(screen.getByText("Motor current")).toBeInTheDocument();
    expect(screen.getByTestId("line-chart")).toBeInTheDocument();
    const path = screen.getByTestId("series-motor-current");
    expect(path.getAttribute("d")).toMatch(/^M .*L .*L /);
    expect(path.getAttribute("d")?.match(/L/g)?.length).toBe(2);
    expect(screen.getByText("motor current")).toBeInTheDocument();
  });

  test("shows an empty state with no samples", () => {
    render(<LineChart title="Suction" series={[{ label: "suction", color: "#e0a458", points: [] }]} />);
    expect(screen.getByText(/No samples yet/)).toBeInTheDocument();
  });
});
