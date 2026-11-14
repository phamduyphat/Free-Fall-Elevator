import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

type View = "overview" | "simulation" | "elevators" | "credentials" | "security" | "analytics" | "audit";
type ElevatorStatus = {
  elevator_id: string;
  state: "IN_SERVICE" | "OCCUPIED" | "MAINTENANCE" | "FAULT" | "EMERGENCY_UNAVAILABLE" | "STALE";
  current_floor: number;
  direction: "UP" | "DOWN" | "IDLE";
  door_state: "OPEN" | "CLOSED" | "OBSTRUCTED";
  available: boolean;
  target_floor: number | null;
  pending_stops: number;
  message: string;
  active_fault?: string | null;
  message_to_occupants?: string;
};
type FleetResponse = { elevators: ElevatorStatus[]; source: string; sim_tick: number };
type HallCall = {
  request_id: string;
  origin_floor: number;
  call_type: "ORDINARY" | "FREIGHT";
  direction: "UP" | "DOWN" | null;
  status: string;
  assigned_car: string | null;
  wait_seconds: number | null;
  reassignment_count: number;
};
type ApiResult = { allowed?: boolean; message?: string; detail?: string; request?: HallCall; merged?: boolean; incident?: Incident; session_token?: string; details?: { role?: string; passed?: boolean; total_records_inspected?: number } };
type Summary = { source: string; total_hall_calls: number; completed_hall_calls: number; active_hall_calls: number; mean_wait_seconds: number | null; median_wait_seconds: number | null; p95_wait_seconds: number | null; completed_car_calls: number; active_incidents: number; fleet_available: number; fleet_count: number; events_count: number };
type Incident = { incident_id: string; kind: string; elevator_id: string | null; floor: number | null; status: string; severity: string; details: string; history: string[]; rescue_car_id: string | null };
type StubEvent = { timestamp: string; event_type: string; elevator_id: string | null; detail: string };

async function authorizedApi<T>(path: string, token: string, data?: object): Promise<T> {
  const response = await fetch(path, {
    method: data === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
  const result = await response.json() as T & { detail?: string; message?: string };
  if (!response.ok) throw new Error(result.message ?? result.detail ?? `HTTP ${response.status}`);
  return result;
}

async function getSummary(): Promise<Summary> {
  const response = await fetch("/api/v1/operational/summary");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json() as Promise<Summary>;
}

async function postApi(path: string, data: object): Promise<ApiResult> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const result = await response.json() as ApiResult;
  if (!response.ok) throw new Error(result.message ?? result.detail ?? `HTTP ${response.status}`);
  return result;
}

const floorLabel = (floor: number | null): string => floor === null ? "—" : floor === -1 ? "B1" : `F${floor}`;

const navigation: { id: View; label: string; icon: string }[] = [
  { id: "overview", label: "Overview", icon: "O" },
  { id: "simulation", label: "Simulator (stub)", icon: "S" },
  { id: "elevators", label: "Elevators", icon: "E" },
  { id: "credentials", label: "Credentials", icon: "C" },
  { id: "security", label: "Incidents & diagnostics", icon: "!" },
  { id: "analytics", label: "Analytics", icon: "A" },
  { id: "audit", label: "Audit log", icon: "L" },
];

async function loadFleet(): Promise<FleetResponse> {
  const response = await fetch("/api/v1/fleet/status");
  if (!response.ok) throw new Error(`Fleet request failed with status ${response.status}`);
  return response.json() as Promise<FleetResponse>;
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="empty-state"><span className="empty-icon">—</span><strong>{title}</strong><p>{detail}</p></div>;
}

function PageHeader({ eyebrow, title, detail, action }: { eyebrow: string; title: string; detail?: string; action?: ReactNode }) {
  return <div className="page-header"><div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1>{detail && <p className="page-detail">{detail}</p>}</div>{action}</div>;
}

function FleetCards({ fleet }: { fleet: FleetResponse | null }) {
  return <div className="elevator-grid">{(fleet?.elevators ?? []).map((car) => (
    <article className="elevator-card" key={car.elevator_id}>
      <div className="card-top"><span className="elevator-id">{car.elevator_id}</span><span className={`badge ${car.available ? "ok" : "unknown"}`}>{car.state.replaceAll("_", " ")}</span></div>
      <div className="floor-display">{floorLabel(car.current_floor)}<span>floor</span></div>
      <div className="card-details"><span>Direction</span><strong>{car.direction}</strong></div>
      <div className="card-details"><span>Next stop</span><strong>{floorLabel(car.target_floor)}</strong></div>
      <p className="card-note">{car.message_to_occupants || car.message}</p>
    </article>
  ))}</div>;
}

function Overview({ fleet, summary }: { fleet: FleetResponse | null; summary: Summary | null }) {
  return <>
    <section className="hero-card"><div><p className="eyebrow">Live operations</p><h2>Elevator fleet status</h2><p className="muted">Displaying a software-only test stub. This is not physical elevator telemetry.</p></div><div className="hero-metric"><strong>{fleet?.elevators.length ?? "—"}</strong><span>cars monitored</span></div></section>
    <section className="summary-grid">
      <article className="summary-card"><span className="summary-icon blue">~</span><div><span>Active calls</span><strong>{summary?.active_hall_calls ?? "—"}</strong><small>In-memory hall calls</small></div></article>
      <article className="summary-card"><span className="summary-icon amber">!</span><div><span>Open incidents</span><strong>{summary?.active_incidents ?? "—"}</strong><small>Software test stub</small></div></article>
      <article className="summary-card"><span className="summary-icon green">+</span><div><span>Availability</span><strong>{summary ? `${summary.fleet_available}/${summary.fleet_count}` : "—"}</strong><small>Available for dispatch</small></div></article>
    </section>
    <section className="section"><div className="section-heading"><div><p className="eyebrow">Fleet</p><h2>Elevator snapshot</h2></div><span className="refresh-label">Software test-stub snapshot</span></div><FleetCards fleet={fleet} /></section>
    <section className="section panel"><div className="section-heading"><div><p className="eyebrow">Activity</p><h2>Recent operations</h2></div></div><EmptyState title="No operational events available" detail="Connect the dispatch and telemetry sources to see recent activity." /></section>
  </>;
}

function Elevators({ fleet, refresh }: { fleet: FleetResponse | null; refresh: () => Promise<void> }) {
  return <><PageHeader eyebrow="Fleet management" title="Elevators" detail="Software test-stub states for E1 to E5." action={<button className="button secondary" onClick={() => { void refresh(); }}>Refresh snapshot</button>} /><FleetCards fleet={fleet} /><section className="section panel"><div className="section-heading"><div><p className="eyebrow">Coverage</p><h2>Service matrix</h2></div></div><div className="matrix">{["Car", "Residential", "Freight", "Maintenance"].map((heading) => <strong key={heading}>{heading}</strong>)}{(fleet?.elevators ?? []).flatMap((car) => [<span key={`${car.elevator_id}-id`}>{car.elevator_id}</span>, <span className="matrix-value" key={`${car.elevator_id}-r`}>{car.elevator_id === "E5" ? "No" : "Yes"}</span>, <span className="matrix-value" key={`${car.elevator_id}-f`}>{car.elevator_id === "E5" ? "Yes" : "No"}</span>, <span className="matrix-value" key={`${car.elevator_id}-m`}>{car.state === "MAINTENANCE" ? "Yes" : "No"}</span>])}</div></section></>;
}

const SIM_FLOORS = [-1, ...Array.from({ length: 30 }, (_, index) => index + 1)];

function Simulation({ fleet, onFleetRefresh }: { fleet: FleetResponse | null; onFleetRefresh: () => Promise<void> }) {
  const [selectedId, setSelectedId] = useState("E1");
  const [cardId, setCardId] = useState("ADMIN-CARD-0001");
  const [announcement, setAnnouncement] = useState("Submit a hall call or present a simulated credential.");
  const [hallFloor, setHallFloor] = useState("1");
  const [hallRequest, setHallRequest] = useState<HallCall | null>(null);
  const [calls, setCalls] = useState<HallCall[]>([]);
  const [updating, setUpdating] = useState(true);
  const [busy, setBusy] = useState(false);
  const cars = fleet?.elevators ?? [];
  const selectedCar = cars.find((car) => car.elevator_id === selectedId);

  useEffect(() => {
    if (!updating) return;
    let alive = true;
    const refresh = async () => {
      try {
        const response = await fetch("/api/v1/operational/hall-calls");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json() as { requests: HallCall[] };
        if (alive) {
          setCalls(payload.requests);
          setHallRequest((current) => payload.requests.find((r) => r.request_id === current?.request_id) ?? current);
        }
        await onFleetRefresh();
      } catch (reason) {
        if (alive) setAnnouncement(reason instanceof Error ? reason.message : "Unable to refresh simulation");
      }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 1000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [updating, onFleetRefresh]);

  const runAction = async (action: () => Promise<string>) => {
    if (busy) return;
    setBusy(true);
    try {
      const message = await action();
      setAnnouncement(message);
      await onFleetRefresh();
    } catch (reason) {
      setAnnouncement(reason instanceof Error ? reason.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const hallCall = (callType: "ORDINARY" | "FREIGHT", direction?: "UP" | "DOWN") => {
    void runAction(async () => {
      const result = await postApi("/api/v1/operational/hall-calls", {
        origin_floor: Number(hallFloor), call_type: callType, direction: direction ?? null,
      });
      const request = result.request;
      if (!request) throw new Error("No hall-call record returned");
      setHallRequest(request);
      return `${result.merged ? "Existing request reused" : "Request accepted"}: ${request.status}, assigned ${request.assigned_car ?? "pending"}.`;
    });
  };

  const emergency = (option: "HELP" | "MEDICAL") => {
    void runAction(async () => {
      const result = await postApi("/api/v1/operational/emergency/trigger", { elevator_id: selectedId, option });
      return `${option} registered. Incident ${result.incident?.incident_id ?? "unknown"}; staff alerted in test stub.`;
    });
  };

  const doorCommand = (action: "OPEN" | "CLOSE") => {
    void runAction(async () => {
      await postApi("/api/v1/operational/doors", { elevator_id: selectedId, action });
      return `Door ${action.toLowerCase()} accepted by software test stub.`;
    });
  };

  const swipe = () => {
    if (!selectedCar) return;
    void runAction(async () => {
      const result = await postApi("/api/v1/acs/auth/cab/swipe", {
        elevator_id: selectedId, credential_id: cardId, origin_floor: selectedCar.current_floor,
      });
      return result.message ?? "Card processed";
    });
  };

  const selectDestination = (floor: number) => {
    if (!selectedCar) return;
    void runAction(async () => {
      const result = await postApi("/api/v1/acs/cab/destination", {
        elevator_id: selectedId, origin_floor: selectedCar.current_floor, requested_floor: floor,
      });
      return result.message ?? `Destination ${floorLabel(floor)} accepted`;
    });
  };

  return <section className="simulation-page">
    <PageHeader eyebrow="Mode 1 · test-stub integration" title="Elevator operations" detail="Hall calls, direction-aware dispatch, and ACS-authorized destinations." action={<button className="button primary" onClick={() => setUpdating((on) => !on)}>{updating ? "Pause updates" : "Resume updates"}</button>} />
    <div className="simulation-notice"><strong>Software test stub</strong><span>These car positions come from the in-memory backend simulator. They are NOT live equipment readings. Pausing updates does not stop the server.</span><span className="srs-tag">SRS SWI-2</span></div>
    <div className="simulation-toolbar"><label>Selected car<select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{["E1", "E2", "E3", "E4", "E5"].map((id) => <option key={id}>{id}</option>)}</select></label><label>Card reader<input value={cardId} onChange={(event) => setCardId(event.target.value)} aria-label="Simulated card credential" /></label><button className="button secondary" disabled={busy || !selectedCar} onClick={swipe}>Present card</button></div>
    <div className="simulation-layout">
      <div className="shaft-panel">
        <div className="shaft-labels">{[...SIM_FLOORS].reverse().map((floor) => <span key={floor}>{floorLabel(floor)}</span>)}</div>
        <div className="shaft-grid">{[...SIM_FLOORS].reverse().map((floor) => <div className="floor-line" key={floor}><span>{floor === -1 ? "Basement 1" : `Floor ${floor}`}</span></div>)}
          {cars.map((car) => <div className={`sim-car ${car.state === "EMERGENCY_UNAVAILABLE" ? "emergency-car" : ""} ${car.state === "MAINTENANCE" ? "maintenance-car" : ""}`} key={car.elevator_id} style={{ bottom: `${((car.current_floor === -1 ? 0 : car.current_floor) / 30) * 100}%`, left: `${(Number(car.elevator_id.slice(1)) - 1) * 20 + 1}%` }} title={`${car.elevator_id}: ${floorLabel(car.current_floor)}`}>
            <div className="sim-car-header"><strong>{car.elevator_id}</strong><span>{car.direction === "UP" ? "↑" : car.direction === "DOWN" ? "↓" : "·"}</span></div>
            <small>{floorLabel(car.current_floor)}</small>
          </div>)}
        </div>
      </div>
      <div className="simulation-side">
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-8 · SEC-2.6</p><h2>{selectedId} operating panel</h2></div><span className={`state-pill ${selectedCar?.available ? "ok" : "warn"}`}>{selectedCar?.state.replaceAll("_", " ") ?? "Loading"}</span></div><div className="car-state-grid"><div><span>Floor</span><strong>{floorLabel(selectedCar?.current_floor ?? null)}</strong></div><div><span>Direction</span><strong>{selectedCar?.direction ?? "—"}</strong></div><div><span>Door</span><strong>{selectedCar?.door_state ?? "—"}</strong></div><div><span>Next stop</span><strong>{floorLabel(selectedCar?.target_floor ?? null)}</strong></div></div><p className="authorization-feedback" aria-live="polite">{announcement}</p><div className="control-row"><button className="button secondary" disabled={busy || !selectedCar} onClick={() => doorCommand("OPEN")}>Door open</button><button className="button secondary" disabled={busy || !selectedCar} onClick={() => doorCommand("CLOSE")}>Door close</button><button className="button emergency-button" disabled={busy || !selectedCar} onClick={() => emergency("HELP")}>Emergency / help</button><button className="button medical-button" disabled={busy || !selectedCar} onClick={() => emergency("MEDICAL")}>Medical</button></div><div className="destination-panel"><strong>Destination keys</strong><div className="destination-keys"><button disabled={busy || !selectedCar?.available || selectedId === "E5"} onClick={() => selectDestination(-1)}>B1</button>{Array.from({ length: 30 }, (_, index) => index + 1).map((floor) => <button disabled={busy || !selectedCar?.available} key={floor} onClick={() => selectDestination(floor)}>{floor}</button>)}</div></div></div>
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-9 · FR-1.3 to FR-1.7</p><h2>Hall panel</h2></div></div><div className="hall-controls"><label>Call floor<select value={hallFloor} onChange={(event) => setHallFloor(event.target.value)}>{SIM_FLOORS.map((floor) => <option key={floor} value={floor}>{floor === -1 ? "B1" : `Floor ${floor}`}</option>)}</select></label><button className="button secondary" disabled={busy || hallFloor === "30"} onClick={() => hallCall("ORDINARY", "UP")}>↑ Up</button><button className="button secondary" disabled={busy || hallFloor === "-1"} onClick={() => hallCall("ORDINARY", "DOWN")}>↓ Down</button><button className="button secondary freight-button" disabled={busy || hallFloor === "-1"} onClick={() => hallCall("FREIGHT")}>E5 Freight</button></div>{hallRequest && <div className="request-feedback" aria-live="polite"><strong>Request status: </strong>{hallRequest.status} · {hallRequest.assigned_car ?? "Unassigned"} · wait {hallRequest.wait_seconds ?? "—"}s</div>}</div>
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-3 · monitoring</p><h2>Fleet state and calls</h2></div></div><div className="sim-car-list">{cars.map((car) => <div className="sim-car-row" key={car.elevator_id}><span className="elevator-id">{car.elevator_id}</span><span className="sim-direction">{car.direction}</span><strong>{floorLabel(car.current_floor)}</strong><span>{car.state}</span><span className="weight">{car.pending_stops} stops</span></div>)}</div><p className="muted dark">{calls.filter((request) => request.status !== "COMPLETED").length} active hall requests. Data resets when the application restarts.</p></div>
      </div>
    </div>
  </section>;
}

function Credentials() {
  return <><PageHeader eyebrow="Access control" title="Credentials" detail="Manage cardholders, access scopes, and credential lifecycle." action={<button className="button primary" disabled>Issue credential</button>} /><div className="toolbar"><input aria-label="Search credentials" placeholder="Search by name or credential ID" disabled /><select aria-label="Credential status" disabled><option>All statuses</option></select></div><section className="panel"><div className="table-heading"><strong>Cardholders</strong><span>0 records</span></div><EmptyState title="Credential records are not connected" detail="The API will populate this table from the credentials and users tables." /></section></>;
}

function Security({ token, role, refreshFleet }: { token: string; role: string; refreshFleet: () => Promise<void> }) {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [medicalFloor, setMedicalFloor] = useState("3");
  const [diagnosticCar, setDiagnosticCar] = useState("E1");
  const [faultCode, setFaultCode] = useState("SIMULATED_FAULT");
  const refresh = useCallback(async () => {
    if (!token) { setIncidents([]); return; }
    try {
      const result = await authorizedApi<{items: Incident[]}>("/api/v1/operational/incidents", token);
      setIncidents(result.items);
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Failed to load incidents"); }
  }, [token]);
  useEffect(() => { void refresh(); }, [refresh]);
  const act = async (path: string, body: object) => {
    setBusy(true);
    try {
      await authorizedApi(path, token, body);
      setError(null);
      await refresh();
      await refreshFleet();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Operation failed"); }
    finally { setBusy(false); }
  };
  return <><PageHeader eyebrow="Mode 3 · software test stub" title="Incidents and emergency response" detail="Actions require an ACS dashboard session. Simulation records are not the database audit log." action={<button className="button secondary" disabled={!token} onClick={() => { void refresh(); }}>Refresh incidents</button>} />
    {!token ? <EmptyState title="Dashboard sign-in required" detail="Present an Admin or Technician credential in the header to inspect and resolve simulated incidents." /> : <>
      {error && <p className="authorization-feedback" role="alert">{error}</p>}
      {role === "ADMIN" && <section className="panel section"><div className="section-heading"><h2>Admin demo controls</h2></div><div className="hall-controls"><label>Telephone medical floor<input type="number" min={1} max={30} value={medicalFloor} onChange={(e) => setMedicalFloor(e.target.value)} /></label><button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/phone-medical", {floor: Number(medicalFloor)}); }}>Record medical call</button><button className="button emergency-button" disabled={busy} onClick={() => { void act("/api/v1/operational/stub/building-event", {kind:"FIRE"}); }}>Inject fire (stub)</button></div></section>}
      <section className="panel section"><div className="table-heading"><strong>Simulated incident queue</strong><span>{incidents.length} records</span></div>
      {incidents.length === 0 ? <EmptyState title="No incidents in memory" detail="Use HELP or MEDICAL on the simulator screen, or an Admin test event." /> : incidents.map((incident) =>
        <article className="incident-card" key={incident.incident_id}><div><strong>{incident.kind}</strong> · {incident.status} · {incident.severity}</div><p>{incident.details}</p><small>{incident.elevator_id ?? "Building"} · {floorLabel(incident.floor)} · {incident.incident_id}</small><div className="incident-actions">
          {incident.status === "OPEN" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/action", {incident_id: incident.incident_id, action:"ACKNOWLEDGE"}); }}>Acknowledge</button>}
          {(incident.status === "OPEN" || incident.status === "ACKNOWLEDGED") && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/action", {incident_id: incident.incident_id, action:"RESOLVE", recovery_confirmed: true}); }}>Resolve (after checks)</button>}
          {incident.status === "RESOLVED" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/action", {incident_id: incident.incident_id, action:"CLOSE"}); }}>Close</button>}
          {role === "ADMIN" && incident.kind === "HELP" && incident.status !== "RESOLVED" && incident.status !== "CLOSED" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/confirm-stuck", {incident_id: incident.incident_id}); }}>Confirm stuck</button>}
          {role === "ADMIN" && ["MEDICAL", "STUCK_CAR"].includes(incident.kind) && !incident.rescue_car_id && incident.status !== "CLOSED" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/rescue", {incident_id: incident.incident_id, confirmed_empty:true, wait_seconds:120}); }}>Send empty rescue car (confirm)</button>}
          {role === "ADMIN" && incident.kind === "MEDICAL" && incident.rescue_car_id && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/confirm-boarding", {incident_id: incident.incident_id}); }}>Confirm rescue boarding</button>}
        </div></article>
      )}</section>
    </>}
  </>;
}

function Analytics({ summary }: { summary: Summary | null }) {
  const values: {title: string; value: string; unit: string}[] = [
    { title: "Mean hall wait", value: summary?.mean_wait_seconds == null ? "—" : summary.mean_wait_seconds.toFixed(1), unit: "seconds" },
    { title: "Median hall wait", value: summary?.median_wait_seconds == null ? "—" : summary.median_wait_seconds.toFixed(1), unit: "seconds" },
    { title: "P95 hall wait", value: summary?.p95_wait_seconds == null ? "—" : summary.p95_wait_seconds.toFixed(1), unit: "seconds" },
    { title: "Completed hall calls", value: String(summary?.completed_hall_calls ?? "—"), unit: "requests" },
    { title: "Completed car calls", value: String(summary?.completed_car_calls ?? "—"), unit: "requests" },
    { title: "Available cars", value: String(summary?.fleet_available ?? "—"), unit: "out of 5" },
  ];
  return <><PageHeader eyebrow="Mode 5 · test-stub aggregates" title="Analytics" detail="These measurements are derived from process-local hall calls. They are not the persisted PostgreSQL analytics and do not support traffic profiles." />
    <div className="kpi-grid">{values.map(({title,value,unit}) => <article className="kpi-card" key={title}><span>{title}</span><strong>{value}</strong><small>{unit}</small></article>)}</div>
    <section className="panel section"><h2>Measurement definition</h2><p>Hall wait time measures accepted hall call to car arrival. P95 uses the nearest-rank percentile. No values are shown when the stub has not completed a hall call. Restarting the API clears these measurements.</p></section>
  </>;
}

function Audit({ token, role }: { token: string; role: string }) {
  const [logs, setLogs] = useState<Record<string, unknown>[]>([]);
  const [stubEvents, setStubEvents] = useState<StubEvent[]>([]);
  const [integrity, setIntegrity] = useState<string>("");
  const [error, setError] = useState<string>("");
  const refresh = useCallback(async () => {
    if (!token || role !== "ADMIN") { setLogs([]); setStubEvents([]); return; }
    try {
      const [data, events] = await Promise.all([
        authorizedApi<{items: Record<string, unknown>[]}>("/api/v1/acs/logs/audit?limit=20", token),
        authorizedApi<{items: StubEvent[]}>("/api/v1/operational/events", token),
      ]);
      setLogs(data.items); setStubEvents(events.items); setError("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Audit retrieval failed"); }
  }, [token, role]);
  useEffect(() => { void refresh(); }, [refresh]);
  const verify = async () => {
    try {
      const result = await authorizedApi<ApiResult>("/api/v1/acs/logs/audit/verify", token, {});
      setIntegrity(result.details?.passed ? `Verified ${result.details.total_records_inspected ?? "?"} DB records` : "Audit-integrity check reported an error");
    } catch (reason) { setIntegrity(reason instanceof Error ? reason.message : "Verification failed"); }
  };
  return <><PageHeader eyebrow="Governance" title="Audit log" detail="Database-backed ACS audit chain and separate, non-tamper-evident simulator event trace." action={<button className="button secondary" disabled={!token || role !== "ADMIN"} onClick={() => { void verify(); }}>Verify ACS hash chain</button>} />
    {!token || role !== "ADMIN" ? <EmptyState title="Admin sign-in required" detail="The real ACS audit log is only accessible to authorized Admins." /> : <>
      {integrity && <p className="authorization-feedback">{integrity}</p>}{error && <p role="alert">{error}</p>}
      <section className="panel section"><div className="table-heading"><strong>ACS audit records · database</strong><span>{logs.length} shown</span></div>{logs.map((item, i) => <pre className="event-row" key={i}>{JSON.stringify(item)}</pre>)}{!logs.length && <EmptyState title="No ACS audit records loaded" detail="This view requires a connected PostgreSQL database." />}</section>
      <section className="panel section"><div className="table-heading"><strong>Simulator event trace · not tamper-evident</strong><span>{stubEvents.length} shown</span></div>{stubEvents.map((event, i) => <p className="event-row" key={i}>{event.event_type} · {event.elevator_id ?? "Building"} · {event.detail}</p>)}</section>
    </>}
  </>;
}

function App() {
  const [view, setView] = useState<View>("overview");
  const [fleet, setFleet] = useState<FleetResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [credential, setCredential] = useState("ADMIN-CARD-0001");
  const [token, setToken] = useState("");
  const [role, setRole] = useState("");
  const [loginError, setLoginError] = useState("");
  const active = useMemo(() => navigation.find((item) => item.id === view) ?? navigation[0], [view]);

  const onFleetRefresh = useCallback(async () => {
    try {
      const [nextFleet, nextSummary] = await Promise.all([loadFleet(), getSummary()]);
      setFleet(nextFleet);
      setSummary(nextSummary);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load fleet status.");
    }
  }, []);

  useEffect(() => { void onFleetRefresh(); }, [onFleetRefresh]);

  const login = async () => {
    try {
      const response = await postApi("/api/v1/acs/auth/dashboard/login", {credential_id:credential});
      if (!response.allowed || !response.session_token) throw new Error(response.message ?? "No dashboard session issued");
      setToken(response.session_token); setRole(response.details?.role ?? "UNKNOWN"); setLoginError("");
    } catch (reason) { setToken(""); setRole(""); setLoginError(reason instanceof Error ? reason.message : "Login failed"); }
  };
  const logout = async () => {
    try { await authorizedApi("/api/v1/acs/auth/dashboard/logout", token, {}); }
    catch { /* Clear client session even when the server has expired it. */ }
    setToken(""); setRole("");
  };
  const page = view === "overview" ? <Overview fleet={fleet} summary={summary} /> : view === "simulation" ? <Simulation fleet={fleet} onFleetRefresh={onFleetRefresh} /> : view === "elevators" ? <Elevators fleet={fleet} refresh={onFleetRefresh} /> : view === "credentials" ? <Credentials /> : view === "security" ? <Security token={token} role={role} refreshFleet={onFleetRefresh} /> : view === "analytics" ? <Analytics summary={summary} /> : <Audit token={token} role={role} />;
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark">S</span><div><strong>SREMS</strong><span>Operations</span></div></div><nav aria-label="Primary navigation">{navigation.map((item) => <button className={`nav-item ${item.id === view ? "active" : ""}`} key={item.id} onClick={() => setView(item.id)}><span className="nav-icon">{item.icon}</span>{item.label}</button>)}</nav><div className="sidebar-footer"><span className="status-dot" /> {error ? "API unavailable" : fleet ? "Test stub connected" : "Connecting"}</div></aside>
    <main className="content"><header className="topbar"><div><p className="eyebrow">Building operations</p><div className="breadcrumb">SREMS / {active.label}</div></div><div className="operator">{token ? <><span className="avatar">{role.slice(0,1)}</span><span><strong>{role}</strong><small>ACS session</small></span><button className="button secondary" onClick={() => { void logout(); }}>Log out</button></> : <><label className="login-field">Dashboard card<input aria-label="Dashboard card" value={credential} onChange={(event) => setCredential(event.target.value)} /></label><button className="button secondary" onClick={() => { void login(); }}>Sign in</button></>}</div></header>{loginError && <div className="error-banner" role="alert">{loginError}</div>}{error && <div className="error-banner" role="alert">{error}</div>}<div className="page-content">{page}</div></main>
  </div>;
}

export default App;
