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

describe("GET /devices", () => {
  test("lists every device with live status", async () => {
    const response = await request(ctx.app).get("/devices").set(AUTH);
    expect(response.status).toBe(200);
    expect(response.body.count).toBe(2);
    const online = response.body.devices.find(
      (device: { thing_id: string }) => device.thing_id === "vac-001",
    );
    const offline = response.body.devices.find(
      (device: { thing_id: string }) => device.thing_id === "vac-002",
    );
    expect(online.online).toBe(true);
    expect(online.fw_version).toBe("1.2.0");
    expect(online.battery_pct).toBe(87.5);
    expect(offline.online).toBe(false);
  });

  test("rejects a missing api key", async () => {
    const response = await request(ctx.app).get("/devices");
    expect(response.status).toBe(401);
    expect(response.body.error).toBe("Unauthorized");
  });

  test("rejects a wrong api key", async () => {
    const response = await request(ctx.app).get("/devices").set("x-api-key", "nope");
    expect(response.status).toBe(401);
  });
});

describe("GET /devices/{id}", () => {
  test("returns desired/reported/delta, firmware and telemetry", async () => {
    const response = await request(ctx.app).get("/devices/vac-001").set(AUTH);
    expect(response.status).toBe(200);
    expect(response.body.fw_version).toBe("1.2.0");
    expect(response.body.shadow.version).toBe(3);
    expect(response.body.shadow.state.desired.power_level).toBe(4);
    expect(response.body.shadow.state.reported.power_level).toBe(2);
    expect(response.body.shadow.state.delta.power_level).toBe(4);
    expect(response.body.telemetry.battery_pct).toBe(87.5);
    expect(response.body.online).toBe(true);
  });

  test("returns 404 for an unknown device", async () => {
    const response = await request(ctx.app).get("/devices/vac-999").set(AUTH);
    expect(response.status).toBe(404);
    expect(response.body.error).toContain("Unknown device");
  });

  test("returns 401 without a key", async () => {
    const response = await request(ctx.app).get("/devices/vac-001");
    expect(response.status).toBe(401);
  });
});
