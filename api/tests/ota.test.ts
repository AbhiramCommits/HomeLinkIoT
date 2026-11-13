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

describe("POST /devices/{id}/ota", () => {
  test("publishes a desired OTA spec and all firmware chunks", async () => {
    const response = await request(ctx.app).post("/devices/vac-001/ota").set(AUTH).send({});
    expect(response.status).toBe(202);
    expect(response.body.ota.fw_version).toBe("1.3.0");
    expect(response.body.ota.chunks).toBe(8);
    expect(response.body.ota.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(response.body.chunks_published).toBe(8);
    expect(response.body.corrupted).toBe(false);

    const update = ctx.broker.messages.find(
      (message) => message.topic === "$iot/things/vac-001/shadow/update",
    );
    const desired = JSON.parse(update?.payload.toString() ?? "{}");
    expect(desired.state.desired.ota.fw_version).toBe("1.3.0");

    const chunks = ctx.broker.messages.filter(
      (message) => message.topic === "homelink/vac-001/ota/chunk",
    );
    expect(chunks).toHaveLength(8);
    const first = JSON.parse(chunks[0]?.payload.toString() ?? "{}");
    expect(first.index).toBe(0);
    expect(first.total).toBe(8);
    expect(Buffer.from(first.data, "base64").length).toBe(1024);
  });

  test("can publish a deliberately corrupted image", async () => {
    const response = await request(ctx.app)
      .post("/devices/vac-001/ota")
      .set(AUTH)
      .send({ version: "1.4.0", corrupt: true });
    expect(response.status).toBe(202);
    expect(response.body.corrupted).toBe(true);
    expect(response.body.ota.fw_version).toBe("1.4.0");
  });

  test("rejects an invalid version with 400", async () => {
    const response = await request(ctx.app)
      .post("/devices/vac-001/ota")
      .set(AUTH)
      .send({ version: "one-point-three" });
    expect(response.status).toBe(400);
    expect(response.body.error).toBe("Invalid request body");
  });

  test("returns 404 for an unknown device", async () => {
    const response = await request(ctx.app).post("/devices/vac-999/ota").set(AUTH).send({});
    expect(response.status).toBe(404);
  });

  test("requires an api key", async () => {
    const response = await request(ctx.app).post("/devices/vac-001/ota").send({});
    expect(response.status).toBe(401);
  });
});
