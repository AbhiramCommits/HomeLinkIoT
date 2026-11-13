import type { ReactElement } from "react";

interface ShadowDiffProps {
  desired: Record<string, unknown> | null;
  reported: Record<string, unknown> | null;
}

type DiffStatus = "same" | "different" | "missing";

interface DiffRow {
  path: string;
  desired: unknown;
  reported: unknown;
  status: DiffStatus;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function flatten(value: Record<string, unknown> | null, prefix = ""): Map<string, unknown> {
  const entries = new Map<string, unknown>();
  if (!isRecord(value)) return entries;
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (isRecord(child) && Object.keys(child).length > 0) {
      for (const [nestedKey, nestedValue] of flatten(child, path)) {
        entries.set(nestedKey, nestedValue);
      }
    } else {
      entries.set(path, child);
    }
  }
  return entries;
}

function renderValue(value: unknown): string {
  if (value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

export function shadowDiffRows(
  desired: Record<string, unknown> | null,
  reported: Record<string, unknown> | null,
): DiffRow[] {
  const desiredFlat = flatten(desired);
  const reportedFlat = flatten(reported);
  const paths = Array.from(new Set([...desiredFlat.keys(), ...reportedFlat.keys()])).sort();
  return paths.map((path) => {
    const hasDesired = desiredFlat.has(path);
    const hasReported = reportedFlat.has(path);
    const desiredValue = desiredFlat.get(path);
    const reportedValue = reportedFlat.get(path);
    let status: DiffStatus;
    if (!hasDesired || !hasReported) status = "missing";
    else if (JSON.stringify(desiredValue) === JSON.stringify(reportedValue)) status = "same";
    else status = "different";
    return { path, desired: desiredValue, reported: reportedValue, status };
  });
}

export function ShadowDiff({ desired, reported }: ShadowDiffProps): ReactElement {
  const rows = shadowDiffRows(desired, reported);
  return (
    <div className="panel" data-testid="shadow-diff">
      <div className="panel-head">
        <h2>Desired vs reported shadow</h2>
        <span className="muted">{rows.filter((row) => row.status !== "same").length} differences</span>
      </div>
      {rows.length === 0 ? (
        <p className="muted">No desired or reported keys.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Desired</th>
              <th>Reported</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.path} data-testid={`diff-row-${row.path}`}>
                <td className="mono">{row.path}</td>
                <td className="mono">{renderValue(row.desired)}</td>
                <td className="mono">{renderValue(row.reported)}</td>
                <td>
                  <span className={`pill pill-${row.status}`} data-testid={`status-${row.path}`}>
                    {row.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
