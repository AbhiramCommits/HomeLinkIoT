import { describe, expect, test } from "vitest";
import { isStale, STALE_AFTER_MS } from "./stale";

describe("isStale", () => {
  test("is fresh at exactly the 10 second boundary", () => {
    expect(isStale(0, STALE_AFTER_MS)).toBe(false);
  });

  test("is stale one millisecond past the boundary", () => {
    expect(isStale(0, STALE_AFTER_MS + 1)).toBe(true);
  });

  test("is fresh for a recent timestamp", () => {
    expect(isStale(1000, 5000)).toBe(false);
  });

  test("never marks a future timestamp stale", () => {
    expect(isStale(10_000, 5000)).toBe(false);
  });
});
