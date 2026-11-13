import type { APIGatewayProxyEventV2 } from "aws-lambda";
import { z } from "zod";
import { getStore } from "../lib/store.js";
import { requireDevice } from "../lib/devices.js";
import { ApiError, getThingId, jsonResponse, parseBody, route } from "../lib/http.js";
import { MqttError, publishMessages, type OutgoingMessage } from "../lib/mqtt.js";
import { brokerConfig } from "../lib/config.js";

const patchSchema = z
  .object({
    state: z
      .object({
        desired: z.record(z.unknown()),
      })
      .strict(),
    version: z.number().int().nonnegative(),
    clientToken: z.string().max(64).optional(),
  })
  .strict();

export const handler = route(async (event: APIGatewayProxyEventV2) => {
  const thingId = getThingId(event);
  const body = parseBody(event, patchSchema);
  const store = getStore();
  const shadow = requireDevice(store, thingId);
  if (body.version !== shadow.version) {
    throw new ApiError(409, "Version conflict", {
      expected: shadow.version,
      received: body.version,
    });
  }
  const update: Record<string, unknown> = {
    state: { desired: body.state.desired },
    version: body.version,
  };
  if (body.clientToken !== undefined) {
    update.clientToken = body.clientToken;
  }
  const messages: OutgoingMessage[] = [
    {
      topic: `$iot/things/${thingId}/shadow/update`,
      payload: JSON.stringify(update),
    },
  ];
  const { host, port } = brokerConfig();
  try {
    await publishMessages(messages, host, port);
  } catch (error) {
    if (error instanceof MqttError) {
      throw new ApiError(503, "MQTT broker unavailable", { reason: error.message });
    }
    throw error;
  }
  return jsonResponse(202, {
    device_id: thingId,
    accepted: true,
    version: body.version + 1,
    clientToken: body.clientToken ?? null,
    desired: { ...(shadow.state.desired ?? {}), ...body.state.desired },
  });
});
