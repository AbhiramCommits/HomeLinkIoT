import type { APIGatewayProxyEventV2 } from "aws-lambda";
import { z } from "zod";
import { getStore } from "../lib/store.js";
import { requireDevice } from "../lib/devices.js";
import { ApiError, getThingId, jsonResponse, parseQuery, route } from "../lib/http.js";

const querySchema = z
  .object({
    from: z.coerce.number().int().nonnegative().optional(),
    to: z.coerce.number().int().nonnegative().optional(),
    limit: z.coerce.number().int().min(1).max(1000).optional(),
  })
  .strict();

export const handler = route(async (event: APIGatewayProxyEventV2) => {
  const thingId = getThingId(event);
  const store = getStore();
  requireDevice(store, thingId);
  const query = parseQuery(event, querySchema);
  if (query.from !== undefined && query.to !== undefined && query.from > query.to) {
    throw new ApiError(400, "Invalid time range", { reason: "from must be <= to" });
  }
  const runs = store.queryRuns(thingId, query.from, query.to, query.limit ?? 200);
  return jsonResponse(200, {
    device_id: thingId,
    from: query.from ?? null,
    to: query.to ?? null,
    count: runs.length,
    runs,
  });
});
