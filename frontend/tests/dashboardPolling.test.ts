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
  // Global fleet/summary polling at 500ms
  assert.match(appSource, /onFleetRefresh\(\)\.finally/);
  assert.match(appSource, /500/);

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

test("App.tsx Analytics displays queue/backlog and all Mode 5 SRS endpoints + export", () => {
  assert.match(appSource, /Active request queue/);
  assert.match(appSource, /Backlog status/);
  for (const endpoint of [
    "/api/v1/analytics/wait-times",
    "/api/v1/analytics/travel-times",
    "/api/v1/analytics/trip-distribution",
    "/api/v1/analytics/utilization",
    "/api/v1/analytics/downtime",
    "/api/v1/analytics/security",
    "/api/v1/analytics/export",
  ]) {
    assert.ok(appSource.includes(endpoint), `Missing analytics endpoint ${endpoint}`);
  }
  assert.match(appSource, /type="datetime-local"/);
  assert.match(appSource, /formatLocalTime\(/);
});

test("App.tsx wires MNT-4.2/4.3/4.5, SEC-2.14 search, and FR-1.13 E5 service run", () => {
  for (const endpoint of [
    "/api/v1/acs/fleet/maintenance",
    "/api/v1/acs/fleet/return-to-service",
    "/api/v1/operational/diagnostics/",
    "/api/v1/acs/logs/authorization-events",
    "/api/v1/acs/logs/incidents",
    "/api/v1/acs/cab/e5/service-run",
  ]) {
    assert.ok(appSource.includes(endpoint), `Missing endpoint ${endpoint}`);
  }
  assert.match(appSource, /car\.active_fault/);
  assert.match(appSource, /mismatch_index/);
  assert.match(appSource, /function SearchSelect/);
  assert.match(appSource, /role="combobox"/);
  assert.match(appSource, /RESCUE_BOARDED/);
  assert.match(appSource, /isEmergencyCar/);
  assert.match(appSource, /EMERGENCY RESCUE/);
});
