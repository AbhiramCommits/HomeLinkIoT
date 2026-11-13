import { afterAll, beforeAll, beforeEach, describe, expect, test } from "vitest";
import request from "supertest";
import { AUTH, createTestContext, type TestContext } from "./testEnv.js";

let ctx: TestContext;

beforeAll(async () => {
  ctx = await createTestContext();
});

afterAll(async () => {
  await ctx.cleanup();
});

beforeEach(() => {
  ctx.broker.messages.length = 0;
});

describe("PATCH /devices/{id}/shadow", () => {
  test("publishes a desired patch with the current version", async () => {
    const response = await request(ctx.app)
      .patch("/devices/vac-001/shadow")
      .set(AUTH)
      .send({ state: { desired: { power_level: 5 } }, version: 3, clientToken: "tok" });
    expect(response.status).toBe(202);
    expect(response.body.accepted).toBe(true);
    expect(response.body.version).toBe(4);
    expect(response.body.clientToken).toBe("tok");

    const update = ctx.broker.messages.find(
      (message) => message.topic === "$iot/things/vac-001/shadow/update",
    );
    expect(update).toBeDefined();
    const payload = JSON.parse(update?.payload.toString() ?? "{}");
    expect(payload.version).toBe(3);
    expect(payload.state.desired.power_level).toBe(5);
    expect(payload.clientToken).toBe("tok");
  });

  test("rejects a stale version with 409", async () => {
    const response = await request(ctx.app)
      .patch("/devices/vac-001/shadow")
      .set(AUTH)
      .send({ state: { desired: { power_level: 1 } }, version: 1 });
    expect(response.status).toBe(409);
    expect(response.body.error).toBe("Version conflict");
    expect(response.body.details.expected).toBe(3);
    expect(ctx.broker.messages).toHaveLength(0);
  });

  test("rejects a malformed desired patch with 400", async () => {
    const response = await request(ctx.app)
      .patch("/devices/vac-001/shadow")
      .set(AUTH)
      .send({ state: { desired: "not-an-object" }, version: 3 });
    expect(response.status).toBe(400);
    expect(response.body.error).toBe("Invalid request body");
  });

  test("rejects a missing version with 400", async () => {
    const response = await request(ctx.app)
      .patch("/devices/vac-001/shadow")
      .set(AUTH)
      .send({ state: { desired: { power_level: 2 } } });
    expect(response.status).toBe(400);
  });

  test("returns 404 for an unknown device", async () => {
    const response = await request(ctx.app)
      .patch("/devices/vac-999/shadow")
      .set(AUTH)
      .send({ state: { desired: { power_level: 2 } }, version: 0 });
    expect(response.status).toBe(404);
  });

  test("requires an api key", async () => {
    const response = await request(ctx.app)
      .patch("/devices/vac-001/shadow")
      .send({ state: { desired: { power_level: 2 } }, version: 3 });
    expect(response.status).toBe(401);
  });
});
