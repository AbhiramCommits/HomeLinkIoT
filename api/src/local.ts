import path from "node:path";
import { pathToFileURL } from "node:url";
import cors from "cors";
import express, { type NextFunction, type Request, type Response } from "express";
import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from "aws-lambda";
import { apiPort } from "./lib/config.js";
import { handler as listDevices } from "./handlers/listDevices.js";
import { handler as getDevice } from "./handlers/getDevice.js";
import { handler as listRuns } from "./handlers/listRuns.js";
import { handler as postCommand } from "./handlers/postCommand.js";
import { handler as patchShadow } from "./handlers/patchShadow.js";
import { handler as triggerOta } from "./handlers/triggerOta.js";
import { handler as getFleetAnalytics } from "./handlers/getFleetAnalytics.js";
import type { Handler } from "./lib/http.js";

function toEvent(req: Request): APIGatewayProxyEventV2 {
  const query: Record<string, string> = {};
  for (const [key, value] of Object.entries(req.query)) {
    if (typeof value === "string") query[key] = value;
    else if (Array.isArray(value)) query[key] = value.map(String).join(",");
    else if (value !== undefined) query[key] = String(value);
  }
  const headers: Record<string, string> = {};
  for (const [key, value] of Object.entries(req.headers)) {
    if (Array.isArray(value)) headers[key] = value.join(",");
    else if (value !== undefined) headers[key] = value;
  }
  let body: string | null = null;
  if (Buffer.isBuffer(req.body)) body = req.body.toString("utf-8");
  else if (typeof req.body === "string") body = req.body;
  else if (req.body !== undefined && req.body !== null && Object.keys(req.body).length > 0) {
    body = JSON.stringify(req.body);
  }
  return {
    version: "2.0",
    routeKey: "",
    rawPath: req.path,
    rawQueryString: req.originalUrl.includes("?") ? req.originalUrl.split("?")[1] ?? "" : "",
    cookies: undefined,
    headers,
    queryStringParameters: Object.keys(query).length > 0 ? query : undefined,
    pathParameters: { ...req.params } as Record<string, string>,
    requestContext: {
      accountId: "local",
      apiId: "local",
      domainName: "localhost",
      domainPrefix: "localhost",
      http: {
        method: req.method,
        path: req.path,
        protocol: "HTTP/1.1",
        sourceIp: req.ip ?? "127.0.0.1",
        userAgent: req.headers["user-agent"] ?? "local",
      },
      requestId: `local-${Date.now()}`,
      routeKey: "",
      stage: "$default",
      time: new Date().toISOString(),
      timeEpoch: Date.now(),
    },
    body: body ?? undefined,
    isBase64Encoded: false,
  };
}

function expressAdapter(handler: Handler) {
  return async (req: Request, res: Response, next: NextFunction): Promise<void> => {
    try {
      const result = (await handler(toEvent(req))) as APIGatewayProxyResultV2;
      if (typeof result === "string") {
        res.status(200).send(result);
        return;
      }
      const response = result as {
        statusCode?: number;
        headers?: Record<string, string | number | boolean | undefined>;
        body?: string;
        isBase64Encoded?: boolean;
      };
      res.status(response.statusCode ?? 200);
      for (const [key, value] of Object.entries(response.headers ?? {})) {
        if (value !== undefined) res.setHeader(key, String(value));
      }
      if (response.isBase64Encoded && response.body) {
        res.send(Buffer.from(response.body, "base64"));
      } else {
        res.send(response.body ?? "");
      }
    } catch (error) {
      next(error);
    }
  };
}

export function createApp(): express.Express {
  const app = express();
  app.use(cors());
  app.use(express.json({ limit: "2mb" }));

  app.get("/health", (_req, res) => {
    res.json({ status: "ok", service: "homelink-api" });
  });
  app.get("/devices", expressAdapter(listDevices));
  app.get("/devices/:id/runs", expressAdapter(listRuns));
  app.get("/devices/:id", expressAdapter(getDevice));
  app.post("/devices/:id/commands", expressAdapter(postCommand));
  app.patch("/devices/:id/shadow", expressAdapter(patchShadow));
  app.post("/devices/:id/ota", expressAdapter(triggerOta));
  app.get("/fleet/analytics", expressAdapter(getFleetAnalytics));

  app.use((error: Error, _req: Request, res: Response, _next: NextFunction) => {
    if ((error as Error & { type?: string }).type === "entity.parse.failed") {
      res.status(400).json({ error: "Invalid JSON body", details: { reason: "body must be valid JSON" } });
      return;
    }
    console.error("local adapter error", error);
    res.status(500).json({ error: "Internal server error", details: null });
  });

  return app;
}

const isMain =
  process.argv[1] !== undefined &&
  pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url;

if (isMain) {
  const port = apiPort();
  createApp().listen(port, () => {
    console.log(`homelink api listening on http://localhost:${port}`);
  });
}
