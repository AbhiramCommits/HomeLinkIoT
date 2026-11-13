import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from "aws-lambda";
import { ZodError, type ZodTypeAny, type z } from "zod";
import { apiKey } from "./config.js";

export type Handler = (event: APIGatewayProxyEventV2) => Promise<APIGatewayProxyResultV2>;

export class ApiError extends Error {
  readonly statusCode: number;
  readonly details: unknown;

  constructor(statusCode: number, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.statusCode = statusCode;
    this.details = details;
  }
}

export function jsonResponse(statusCode: number, body: unknown): APIGatewayProxyResultV2 {
  return {
    statusCode,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
    },
    body: JSON.stringify(body),
  };
}

export function headerValue(event: APIGatewayProxyEventV2, name: string): string | undefined {
  const target = name.toLowerCase();
  for (const [key, value] of Object.entries(event.headers ?? {})) {
    if (key.toLowerCase() === target) {
      return value ?? undefined;
    }
  }
  return undefined;
}

export function requireApiKey(event: APIGatewayProxyEventV2): void {
  const supplied = headerValue(event, "x-api-key");
  if (!supplied || supplied !== apiKey()) {
    throw new ApiError(401, "Unauthorized", { reason: "missing or invalid x-api-key" });
  }
}

export function getThingId(event: APIGatewayProxyEventV2): string {
  const fromPath = event.pathParameters?.id;
  const fallback = event.rawPath.split("/").filter(Boolean)[1];
  const thingId = fromPath ?? fallback;
  if (!thingId) {
    throw new ApiError(400, "Missing device id", { path: event.rawPath });
  }
  return decodeURIComponent(thingId);
}

function decodeBody(event: APIGatewayProxyEventV2): unknown {
  if (!event.body) {
    return undefined;
  }
  const raw = event.isBase64Encoded
    ? Buffer.from(event.body, "base64").toString("utf-8")
    : event.body;
  try {
    return JSON.parse(raw);
  } catch {
    throw new ApiError(400, "Invalid JSON body", { reason: "body must be valid JSON" });
  }
}

export function parseBody<S extends ZodTypeAny>(
  event: APIGatewayProxyEventV2,
  schema: S,
  options: { required?: boolean } = {},
): z.infer<S> {
  const required = options.required ?? true;
  const raw = decodeBody(event);
  if (raw === undefined) {
    if (required) {
      throw new ApiError(400, "Request body is required", { reason: "missing body" });
    }
    return schema.parse({});
  }
  const parsed = schema.safeParse(raw);
  if (!parsed.success) {
    throw new ApiError(400, "Invalid request body", parsed.error.issues);
  }
  return parsed.data;
}

export function parseQuery<S extends ZodTypeAny>(
  event: APIGatewayProxyEventV2,
  schema: S,
): z.infer<S> {
  const params = Object.fromEntries(
    Object.entries(event.queryStringParameters ?? {}).filter(([, value]) => value !== undefined),
  );
  const parsed = schema.safeParse(params);
  if (!parsed.success) {
    throw new ApiError(400, "Invalid query parameters", parsed.error.issues);
  }
  return parsed.data;
}

export function route(handler: Handler): Handler {
  return async (event) => {
    try {
      requireApiKey(event);
      return await handler(event);
    } catch (error) {
      if (error instanceof ApiError) {
        return jsonResponse(error.statusCode, { error: error.message, details: error.details });
      }
      if (error instanceof ZodError) {
        return jsonResponse(400, { error: "Invalid request", details: error.issues });
      }
      console.error("unhandled API error", error);
      return jsonResponse(500, { error: "Internal server error", details: null });
    }
  };
}
