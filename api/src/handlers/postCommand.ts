import type { APIGatewayProxyEventV2 } from "aws-lambda";
import { z } from "zod";
import { getStore } from "../lib/store.js";
import { deviceSummary, requireDevice } from "../lib/devices.js";
import { ApiError, getThingId, jsonResponse, parseBody, route } from "../lib/http.js";
import { MqttError, publishMessages, type OutgoingMessage } from "../lib/mqtt.js";
import { brokerConfig } from "../lib/config.js";

const commandSchema = z
  .object({
    action: z.enum(["start", "stop", "dock", "set_power"]),
    power_level: z.number().int().min(1).max(5).optional(),
  })
  .strict()
  .superRefine((value, ctx) => {
    if (value.action === "set_power" && value.power_level === undefined) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["power_level"],
        message: "power_level is required for set_power",
      });
    }
  });

export const handler = route(async (event: APIGatewayProxyEventV2) => {
  const thingId = getThingId(event);
  const body = parseBody(event, commandSchema);
  const store = getStore();
  const shadow = requireDevice(store, thingId);
  const telemetry = store.getTelemetry(thingId);
  const device = deviceSummary(thingId, shadow, telemetry);
  const command = store.putCommand(thingId, body);
  const desiredCommand: Record<string, unknown> = {
    id: command.command_id,
    action: body.action,
    ...(body.power_level !== undefined ? { power_level: body.power_level } : {}),
  };
  const desired: Record<string, unknown> = { command: desiredCommand };
  if (body.power_level !== undefined) {
    desired.power_level = body.power_level;
  }
  const messages: OutgoingMessage[] = [
    {
      topic: `$iot/things/${thingId}/shadow/update`,
      payload: JSON.stringify({ state: { desired } }),
    },
    {
      topic: `homelink/${thingId}/commands`,
      payload: JSON.stringify(command),
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
    command,
    queued: !device.online,
    device_online: device.online,
  });
});
