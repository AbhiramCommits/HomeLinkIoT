import { afterAll, beforeAll, test } from "vitest";
import request from "supertest";
import { AUTH, createTestContext, type TestContext } from "./testEnv.js";

let ctx: TestContext;

beforeAll(async () => {
  ctx = await createTestContext();
});

afterAll(async () => {
  await ctx.cleanup();
});

function percentile(sorted: number[], fraction: number): number {
  if (sorted.length === 0) return 0;
  const index = Math.min(sorted.length - 1, Math.ceil(fraction * sorted.length) - 1);
  return sorted[index] ?? 0;
}

test("200-request GET /devices latency loop", async () => {
  const samples: number[] = [];
  for (let index = 0; index < 200; index += 1) {
    const started = performance.now();
    const response = await request(ctx.app).get("/devices").set(AUTH);
    samples.push(performance.now() - started);
    if (response.status !== 200) {
      throw new Error(`unexpected status ${response.status}`);
    }
  }
  const sorted = [...samples].sort((a, b) => a - b);
  const p50 = percentile(sorted, 0.5);
  const p95 = percentile(sorted, 0.95);
  const p99 = percentile(sorted, 0.99);
  console.log(
    `API_LATENCY_MS p50=${p50.toFixed(2)} p95=${p95.toFixed(2)} p99=${p99.toFixed(2)} requests=${samples.length}`,
  );
});
