import type { APIGatewayProxyEventV2 } from "aws-lambda";
import { z } from "zod";
import { getStore } from "../lib/store.js";
import { requireDevice } from "../lib/devices.js";
import { ApiError, getThingId, jsonResponse, parseBody, route } from "../lib/http.js";
import { MqttError, publishMessages } from "../lib/mqtt.js";
import { brokerConfig } from "../lib/config.js";
import { buildFirmware, otaMessages } from "../lib/ota.js";

const otaSchema = z
  .object({
    version: z
      .string()
      .regex(/^\d+\.\d+\.\d+$/, "version must be semver, e.g. 1.3.0")
      .default("1.3.0"),
    corrupt: z.boolean().default(false),
  })
  .strict();

export const handler = route(async (event: APIGatewayProxyEventV2) => {
  const thingId = getThingId(event);
  const body = parseBody(event, otaSchema, { required: false });
  const store = getStore();
  requireDevice(store, thingId);

  const build = buildFirmware(body.version, { corrupt: body.corrupt });
  const { messages, published, dropped } = otaMessages(
    thingId,
    build,
    `$iot/things/${thingId}/shadow/update`,
    `homelink/${thingId}/ota/chunk`,
  );
  const { host, port } = brokerConfig();
  try {
    await publishMessages(messages, host, port, 5000);
  } catch (error) {
    if (error instanceof MqttError) {
      throw new ApiError(503, "MQTT broker unavailable", { reason: error.message });
    }
    throw error;
  }
  return jsonResponse(202, {
    device_id: thingId,
    ota: build.spec,
    corrupted: build.corrupted,
    chunks_published: published,
    chunks_dropped: dropped,
  });
});
