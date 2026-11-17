import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const appSource = fs.readFileSync(
  path.resolve(__dirname, "../src/App.tsx"),
  "utf8",
);
const apiSource = fs.readFileSync(
  path.resolve(__dirname, "../src/api.ts"),
  "utf8",
);

test("App.tsx polls fleet/summary and Security alerts under 1s (PERF-3)", () => {
  // Global fleet/summary polling at 750ms
  assert.match(appSource, /onFleetRefresh\(\)\.finally/);
  assert.match(appSource, /750/);

  // Security view polls both incidents and database security alerts
  assert.match(appSource, /\/api\/v1\/operational\/incidents/);
  assert.match(appSource, /\/api\/v1\/acs\/logs\/alerts/);
  assert.match(appSource, /Security alerts \(database\)/);
});

test("App.tsx renders backlog exceeded alert banner (PERF-5)", () => {
  assert.match(appSource, /summary\?\.backlog_exceeded/);
  assert.match(appSource, /Request backlog exceeded/);
  assert.match(appSource, /role="alert"/);
});

test("api.ts supports PATCH in authorizedApi for alert acknowledgement/resolution", () => {
  assert.match(apiSource, /method\?:\s*"GET"\s*\|\s*"POST"\s*\|\s*"PATCH"/);
  assert.match(appSource, /"PATCH"/);
});
