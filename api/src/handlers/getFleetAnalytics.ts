import { readFile } from "node:fs/promises";
import type { APIGatewayProxyEventV2 } from "aws-lambda";
import { analyticsPath } from "../lib/config.js";
import { ApiError, jsonResponse, route } from "../lib/http.js";

export const handler = route(async (_event: APIGatewayProxyEventV2) => {
  const file = analyticsPath();
  let raw: string;
  try {
    raw = await readFile(file, "utf-8");
  } catch {
    throw new ApiError(503, "Fleet analytics not generated", {
      path: file,
      hint: "run `make analytics`",
    });
  }
  try {
    return jsonResponse(200, JSON.parse(raw));
  } catch {
    throw new ApiError(500, "Fleet analytics file is not valid JSON", { path: file });
  }
});
