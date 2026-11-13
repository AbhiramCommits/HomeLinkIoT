import type { Express } from "express";
import { createApp } from "../src/local.js";
import { resetStoreCache } from "../src/lib/store.js";
import { seedDatabase, startFakeBroker, type FakeBroker, type SeedPaths } from "./helpers.js";

export interface TestContext {
  app: Express;
  broker: FakeBroker;
  seed: SeedPaths;
  cleanup: () => Promise<void>;
}

export async function createTestContext(): Promise<TestContext> {
  const broker = await startFakeBroker();
  const seed = seedDatabase();
  process.env.HOMELINK_MQTT_HOST = "127.0.0.1";
  process.env.HOMELINK_MQTT_PORT = String(broker.port);
  process.env.HOMELINK_DB = seed.dbFile;
  process.env.HOMELINK_API_KEY = "test-key";
  process.env.HOMELINK_ANALYTICS_PATH = seed.analyticsFile;
  resetStoreCache();
  const app = createApp();
  return {
    app,
    broker,
    seed,
    cleanup: async () => {
      resetStoreCache();
      await broker.close();
    },
  };
}

export const AUTH = { "x-api-key": "test-key" };
