import type { APIGatewayProxyEventV2 } from "aws-lambda";
import { getStore } from "../lib/store.js";
import { deviceSummary, requireDevice } from "../lib/devices.js";
import { getThingId, jsonResponse, route } from "../lib/http.js";

export const handler = route(async (event: APIGatewayProxyEventV2) => {
  const thingId = getThingId(event);
  const store = getStore();
  const shadow = requireDevice(store, thingId);
  const telemetry = store.getTelemetry(thingId);
  const device = deviceSummary(thingId, shadow, telemetry);
  return jsonResponse(200, {
    device,
    shadow,
    telemetry,
    online: device.online,
    stale: !device.online,
    last_seen_ms: device.last_seen_ms,
    fw_version: device.fw_version,
  });
});
