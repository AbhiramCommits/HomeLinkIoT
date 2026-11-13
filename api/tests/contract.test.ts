import fs from "node:fs";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import request from "supertest";
import Ajv from "ajv";
import { AUTH, createTestContext, type TestContext } from "./testEnv.js";

let ctx: TestContext;
const ajv = new Ajv({ allErrors: true, strict: false });

function loadSchema(name: string): object {
  const file = path.resolve(process.cwd(), "../schemas", name);
  const schema = JSON.parse(fs.readFileSync(file, "utf-8")) as Record<string, unknown>;
  // Ajv v8's default build validates draft-07; strip the 2020-12 dialect
  // declaration because this schema only uses the draft-07 keyword subset.
  delete schema.$schema;
  return schema;
}

beforeAll(async () => {
  ctx = await createTestContext();
});

afterAll(async () => {
  await ctx.cleanup();
});

describe("API/schema contract", () => {
  test("device shadow and telemetry validate against shared JSON Schemas", async () => {
    const response = await request(ctx.app).get("/devices/vac-001").set(AUTH);
    expect(response.status).toBe(200);

    const shadowValidator = ajv.compile(loadSchema("shadow.schema.json"));
    const telemetryValidator = ajv.compile(loadSchema("telemetry.schema.json"));

    const shadowOk = shadowValidator(response.body.shadow);
    expect(shadowOk, JSON.stringify(shadowValidator.errors)).toBe(true);

    const telemetryOk = telemetryValidator(response.body.telemetry);
    expect(telemetryOk, JSON.stringify(telemetryValidator.errors)).toBe(true);
  });
});
