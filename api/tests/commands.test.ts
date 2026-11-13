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

describe("POST /devices/{id}/commands", () => {
  test("queues a command and publishes shadow + command messages", async () => {
    const response = await request(ctx.app)
      .post("/devices/vac-001/commands")
      .set(AUTH)
      .send({ action: "start" });
    expect(response.status).toBe(202);
    expect(response.body.queued).toBe(false);
    expect(response.body.device_online).toBe(true);
    expect(response.body.command.action).toBe("start");
    expect(response.body.command.status).toBe("queued");

    const topics = ctx.broker.messages.map((message) => message.topic);
    expect(topics).toContain("$iot/things/vac-001/shadow/update");
    expect(topics).toContain("homelink/vac-001/commands");
    const commandMessage = ctx.broker.messages.find(
      (message) => message.topic === "homelink/vac-001/commands",
    );
    const payload = JSON.parse(commandMessage?.payload.toString() ?? "{}");
    expect(payload.command_id).toBe(response.body.command.command_id);
    expect(payload.action).toBe("start");
  });

  test("marks commands to offline devices as queued", async () => {
    const response = await request(ctx.app)
      .post("/devices/vac-002/commands")
      .set(AUTH)
      .send({ action: "dock" });
    expect(response.status).toBe(202);
    expect(response.body.queued).toBe(true);
    expect(response.body.device_online).toBe(false);
  });

  test("supports set_power with a level", async () => {
    const response = await request(ctx.app)
      .post("/devices/vac-001/commands")
      .set(AUTH)
      .send({ action: "set_power", power_level: 5 });
    expect(response.status).toBe(202);
    const update = ctx.broker.messages.find(
      (message) => message.topic === "$iot/things/vac-001/shadow/update",
    );
    const payload = JSON.parse(update?.payload.toString() ?? "{}");
    expect(payload.state.desired.power_level).toBe(5);
    expect(payload.state.desired.command.action).toBe("set_power");
  });

  test("rejects an invalid action with 400", async () => {
    const response = await request(ctx.app)
      .post("/devices/vac-001/commands")
      .set(AUTH)
      .send({ action: "explode" });
    expect(response.status).toBe(400);
    expect(response.body.error).toBe("Invalid request body");
    expect(Array.isArray(response.body.details)).toBe(true);
  });

  test("requires power_level for set_power", async () => {
    const response = await request(ctx.app)
      .post("/devices/vac-001/commands")
      .set(AUTH)
      .send({ action: "set_power" });
    expect(response.status).toBe(400);
    const detail = JSON.stringify(response.body.details);
    expect(detail).toContain("power_level");
  });

  test("rejects unknown fields", async () => {
    const response = await request(ctx.app)
      .post("/devices/vac-001/commands")
      .set(AUTH)
      .send({ action: "start", surprise: true });
    expect(response.status).toBe(400);
  });

  test("returns 404 for an unknown device", async () => {
    const response = await request(ctx.app)
      .post("/devices/vac-999/commands")
      .set(AUTH)
      .send({ action: "start" });
    expect(response.status).toBe(404);
  });

  test("requires an api key", async () => {
    const response = await request(ctx.app)
      .post("/devices/vac-001/commands")
      .send({ action: "start" });
    expect(response.status).toBe(401);
  });
});
