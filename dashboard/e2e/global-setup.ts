import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const dashboardDir = process.cwd();
const repoRoot = path.resolve(dashboardDir, "..");
const apiDir = path.join(repoRoot, "api");
const logDir = path.join(dashboardDir, "test-results", "e2e-logs");
const apiPort = 18080;
const dashboardPort = 4173;
const apiKey = process.env.HOMELINK_API_KEY ?? "dev-key";
const children: ChildProcess[] = [];

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(
  description: string,
  predicate: () => Promise<boolean>,
  timeoutMs = 45_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    try {
      if (await predicate()) return;
    } catch (error) {
      lastError = error;
    }
    await sleep(300);
  }
  throw new Error(
    `Timed out waiting for ${description}${lastError ? `: ${String(lastError)}` : ""}. See ${logDir}`,
  );
}

async function httpStatus(url: string, headers: Record<string, string> = {}): Promise<number> {
  const response = await fetch(url, { headers });
  return response.status;
}

function startProcess(
  name: string,
  command: string,
  args: string[],
  cwd: string,
  env: Record<string, string> = {},
): ChildProcess {
  fs.mkdirSync(logDir, { recursive: true });
  const logPath = path.join(logDir, `${name}.log`);
  fs.writeFileSync(logPath, `[${name}] starting ${command} ${args.join(" ")}\n`);
  const logFd = fs.openSync(logPath, "a");
  const child = spawn(command, args, {
    cwd,
    detached: true,
    stdio: ["ignore", logFd, logFd],
    env: { ...process.env, ...env },
  });
  fs.closeSync(logFd);
  child.on("exit", (code) => {
    fs.appendFileSync(logPath, `\n[${name}] exited with code ${code}\n`);
  });
  children.push(child);
  return child;
}

function stopProcess(child: ChildProcess): void {
  if (child.exitCode !== null || child.signalCode !== null) return;
  try {
    if (child.pid !== undefined) process.kill(-child.pid, "SIGTERM");
    else child.kill("SIGTERM");
  } catch {
    child.kill("SIGTERM");
  }
}

async function globalSetup(): Promise<() => Promise<void>> {
  fs.mkdirSync(logDir, { recursive: true });
  const pythonEnv = process.env.PYTHON_BIN;
  const python = pythonEnv ?? path.join(repoRoot, ".venv", "bin", "python");
  if (!pythonEnv && !fs.existsSync(python)) {
    throw new Error(`Python virtualenv not found at ${python}; run make install`);
  }
  if (!fs.existsSync(path.join(apiDir, "dist", "local.js"))) {
    throw new Error("api/dist/local.js missing; run `npm run build` in api/ before e2e");
  }
  const analyticsFile = path.join(repoRoot, "analytics", "out", "fleet.json");
  if (!fs.existsSync(analyticsFile)) {
    console.log("e2e: generating fleet analytics");
    execFileSync(
      python,
      ["-m", "homelink.analytics.generate_fleet", "--seed", "7", "--devices", "40", "--days", "30"],
      { cwd: repoRoot, stdio: "inherit" },
    );
    execFileSync(python, ["-m", "homelink.analytics.fleet"], {
      cwd: repoRoot,
      stdio: "inherit",
    });
  }

  // Remove any stale database from a previous run so the API cannot attach to
  // an inode that the demo deletes during its own startup cleanup.
  for (const suffix of ["", "-wal", "-shm"]) {
    const candidate = path.join(repoRoot, `homelink.db${suffix}`);
    if (fs.existsSync(candidate)) fs.unlinkSync(candidate);
  }
  fs.rmSync(path.join(repoRoot, "device_spool"), { recursive: true, force: true });

  const demoLog = path.join(logDir, "demo.log");
  startProcess(
    "demo",
    python,
    ["scripts/run_demo.py", "--seconds", "150", "--no-outage", "--json-out", path.join(logDir, "demo-summary.json")],
    repoRoot,
  );

  await waitFor("demo database", async () => fs.existsSync(path.join(repoRoot, "homelink.db")));

  startProcess("api", process.execPath, ["dist/local.js"], apiDir, {
    PORT: String(apiPort),
    HOMELINK_MQTT_HOST: "127.0.0.1",
    HOMELINK_MQTT_PORT: "18831",
    HOMELINK_DB: path.join(repoRoot, "homelink.db"),
    HOMELINK_API_KEY: apiKey,
    HOMELINK_ANALYTICS_PATH: path.join(repoRoot, "analytics", "out", "fleet.json"),
  });

  startProcess(
    "dashboard",
    process.platform === "win32" ? "npm.cmd" : "npm",
    ["run", "dev", "--", "--host", "127.0.0.1", "--port", String(dashboardPort), "--strictPort"],
    dashboardDir,
    { VITE_API_URL: `http://127.0.0.1:${apiPort}`, VITE_API_KEY: apiKey },
  );

  try {
    await waitFor("API health", async () => (await httpStatus(`http://127.0.0.1:${apiPort}/health`)) === 200);
    await waitFor("dashboard", async () => (await httpStatus(`http://127.0.0.1:${dashboardPort}/`)) === 200);
    await waitFor("demo fleet in API", async () => {
      const response = await fetch(`http://127.0.0.1:${apiPort}/devices`, {
        headers: { "x-api-key": apiKey },
      });
      if (response.status !== 200) return false;
      const body = (await response.json()) as { devices?: unknown[] };
      return Array.isArray(body.devices) && body.devices.length >= 1;
    });
  } catch (error) {
    for (const child of children) stopProcess(child);
    throw error;
  }

  console.log(`e2e stack ready: dashboard=http://127.0.0.1:${dashboardPort} api=http://127.0.0.1:${apiPort}`);
  if (fs.existsSync(demoLog)) {
    // Keep the log path visible in CI output for quick diagnosis.
    console.log(`demo log: ${demoLog}`);
  }

  return async () => {
    for (const child of [...children].reverse()) stopProcess(child);
    await sleep(1500);
    for (const child of children) {
      if (child.exitCode === null && child.signalCode === null) {
        try {
          if (child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
          else child.kill("SIGKILL");
        } catch {
          child.kill("SIGKILL");
        }
      }
    }
  };
}

export default globalSetup;
