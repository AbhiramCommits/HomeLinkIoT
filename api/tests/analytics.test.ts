import { afterAll, beforeAll, describe, expect, test } from "vitest";
import request from "supertest";
import { AUTH, createTestContext, type TestContext } from "./testEnv.js";

let ctx: TestContext;
let originalPath: string | undefined;

beforeAll(async () => {
  ctx = await createTestContext();
  originalPath = process.env.HOMELINK_ANALYTICS_PATH;
});

afterAll(async () => {
  if (originalPath !== undefined) process.env.HOMELINK_ANALYTICS_PATH = originalPath;
  await ctx.cleanup();
});

describe("GET /fleet/analytics", () => {
  test("serves the generated fleet analytics document", async () => {
    const response = await request(ctx.app).get("/fleet/analytics").set(AUTH);
    expect(response.status).toBe(200);
    expect(response.body.fleet.devices).toBe(40);
    expect(response.body.error_pareto[0].code).toBe("BRUSH_JAM");
    expect(response.body.anomalies.f1).toBe(0.9);
  });

  test("requires an api key", async () => {
    const response = await request(ctx.app).get("/fleet/analytics");
    expect(response.status).toBe(401);
  });

  test("returns 503 when analytics have not been generated", async () => {
    process.env.HOMELINK_ANALYTICS_PATH = "/tmp/definitely-missing-fleet.json";
    const response = await request(ctx.app).get("/fleet/analytics").set(AUTH);
    expect(response.status).toBe(503);
    expect(response.body.error).toBe("Fleet analytics not generated");
    if (originalPath !== undefined) process.env.HOMELINK_ANALYTICS_PATH = originalPath;
  });
});
