import type { ReactElement } from "react";

export interface Point {
  x: number;
  y: number;
}

export interface LineSeries {
  label: string;
  color: string;
  points: Point[];
}

interface LineChartProps {
  title: string;
  series: LineSeries[];
  unit?: string;
  height?: number;
}

const WIDTH = 640;
const PADDING_LEFT = 56;
const PADDING_RIGHT = 16;
const PADDING_TOP = 18;
const PADDING_BOTTOM = 28;

function formatNumber(value: number): string {
  if (Math.abs(value) >= 1000) return value.toFixed(0);
  if (Math.abs(value) >= 10) return value.toFixed(1);
  return value.toFixed(2);
}

function formatTime(value: number): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function LineChart({
  title,
  series,
  unit = "",
  height = 220,
}: LineChartProps): ReactElement {
  const allPoints = series.flatMap((line) => line.points);
  if (allPoints.length === 0) {
    return (
      <div className="chart-card" data-testid="line-chart">
        <h3>{title}</h3>
        <p className="muted">No samples yet — waiting for telemetry.</p>
      </div>
    );
  }

  const xs = allPoints.map((point) => point.x);
  const ys = allPoints.map((point) => point.y);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMinRaw = Math.min(...ys);
  const yMaxRaw = Math.max(...ys);
  const yPad = Math.max((yMaxRaw - yMinRaw) * 0.08, Math.abs(yMaxRaw) * 0.02, 0.01);
  const yMin = yMinRaw - yPad;
  const yMax = yMaxRaw + yPad;
  const innerWidth = WIDTH - PADDING_LEFT - PADDING_RIGHT;
  const innerHeight = height - PADDING_TOP - PADDING_BOTTOM;

  const xScale = (value: number): number =>
    xMax === xMin
      ? PADDING_LEFT + innerWidth / 2
      : PADDING_LEFT + ((value - xMin) / (xMax - xMin)) * innerWidth;
  const yScale = (value: number): number =>
    PADDING_TOP + innerHeight - ((value - yMin) / (yMax - yMin)) * innerHeight;

  const gridLines = [0, 0.25, 0.5, 0.75, 1];

  return (
    <div className="chart-card" data-testid="line-chart">
      <div className="chart-head">
        <h3>{title}</h3>
        <span className="muted">{unit}</span>
      </div>
      <svg
        className="line-chart"
        viewBox={`0 0 ${WIDTH} ${height}`}
        role="img"
        aria-label={title}
      >
        {gridLines.map((fraction) => {
          const y = PADDING_TOP + innerHeight * (1 - fraction);
          const value = yMin + (yMax - yMin) * fraction;
          return (
            <g key={fraction}>
              <line
                x1={PADDING_LEFT}
                x2={WIDTH - PADDING_RIGHT}
                y1={y}
                y2={y}
                className="grid-line"
              />
              <text x={PADDING_LEFT - 8} y={y + 4} textAnchor="end" className="axis-label">
                {formatNumber(value)}
              </text>
            </g>
          );
        })}
        {series.map((line) => {
          const path = line.points
            .map((point, index) => `${index === 0 ? "M" : "L"} ${xScale(point.x).toFixed(2)} ${yScale(point.y).toFixed(2)}`)
            .join(" ");
          const slug = line.label.toLowerCase().replace(/[^a-z0-9]+/g, "-");
          return (
            <g key={line.label}>
              <path
                d={path}
                fill="none"
                stroke={line.color}
                strokeWidth={2.2}
                strokeLinejoin="round"
                strokeLinecap="round"
                data-testid={`series-${slug}`}
              />
              {line.points.length >= 1 && (
                <circle
                  cx={xScale(line.points[line.points.length - 1]?.x ?? xMax)}
                  cy={yScale(line.points[line.points.length - 1]?.y ?? yMax)}
                  r={3.5}
                  fill={line.color}
                />
              )}
            </g>
          );
        })}
        <text x={PADDING_LEFT} y={height - 8} className="axis-label">
          {formatTime(xMin)}
        </text>
        <text x={WIDTH - PADDING_RIGHT} y={height - 8} textAnchor="end" className="axis-label">
          {formatTime(xMax)}
        </text>
      </svg>
      <div className="legend">
        {series.map((line) => (
          <span key={line.label} className="legend-item">
            <span className="legend-swatch" style={{ background: line.color }} />
            {line.label}
          </span>
        ))}
      </div>
    </div>
  );
}
