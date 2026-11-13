import type { APIGatewayProxyEventV2 } from "aws-lambda";
import { getStore } from "../lib/store.js";
import { deviceSummary } from "../lib/devices.js";
import { jsonResponse, route } from "../lib/http.js";

export const handler = route(async (_event: APIGatewayProxyEventV2) => {
  const store = getStore();
  const now = Date.now();
  const devices = store.listShadows().map(({ thingId, shadow }) =>
    deviceSummary(thingId, shadow, store.getTelemetry(thingId), now),
  );
  return jsonResponse(200, { devices, count: devices.length, generated_at: now });
});
