import { afterAll, beforeAll, describe, expect, test } from "vitest";
import request from "supertest";
import { AUTH, createTestContext, type TestContext } from "./testEnv.js";

let ctx: TestContext;

beforeAll(async () => {
  ctx = await createTestContext();
});

afterAll(async () => {
  await ctx.cleanup();
});

describe("GET /devices/{id}/runs", () => {
  test("lists run history newest first", async () => {
    const response = await request(ctx.app).get("/devices/vac-001/runs").set(AUTH);
    expect(response.status).toBe(200);
    expect(response.body.count).toBe(2);
    expect(response.body.runs[0].run_id).toBe("run-1");
    expect(response.body.runs[1].run_id).toBe("run-0");
  });

  test("filters by from/to and limit", async () => {
    const from = ctx.seed.now - 2_700_000;
    const to = ctx.seed.now - 1_000_000;
    const response = await request(ctx.app)
      .get("/devices/vac-001/runs")
      .query({ from, to })
      .set(AUTH);
    expect(response.status).toBe(200);
    expect(response.body.count).toBe(1);
    expect(response.body.runs[0].run_id).toBe("run-1");

    const limited = await request(ctx.app)
      .get("/devices/vac-001/runs")
      .query({ limit: 1 })
      .set(AUTH);
    expect(limited.status).toBe(200);
    expect(limited.body.count).toBe(1);
    expect(limited.body.runs[0].run_id).toBe("run-1");
  });

  test("rejects malformed query parameters with 400", async () => {
    const response = await request(ctx.app)
      .get("/devices/vac-001/runs?from=not-a-number")
      .set(AUTH);
    expect(response.status).toBe(400);
    expect(response.body.error).toBe("Invalid query parameters");
    expect(Array.isArray(response.body.details)).toBe(true);
  });

  test("rejects an inverted time range", async () => {
    const response = await request(ctx.app)
      .get("/devices/vac-001/runs?from=200&to=100")
      .set(AUTH);
    expect(response.status).toBe(400);
    expect(response.body.error).toBe("Invalid time range");
  });

  test("returns 404 for an unknown device", async () => {
    const response = await request(ctx.app).get("/devices/nope/runs").set(AUTH);
    expect(response.status).toBe(404);
  });

  test("requires an api key", async () => {
    const response = await request(ctx.app).get("/devices/vac-001/runs");
    expect(response.status).toBe(401);
  });
});
