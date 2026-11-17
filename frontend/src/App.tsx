import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { authorizedApi, postApi, requestCabDestination, swipeCabCard } from "./api.ts";
import type { ApiResult } from "./api.ts";

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
type FleetResponse = {
  elevators: ElevatorStatus[];
  source: string;
  sim_tick: number;
  active_requests?: number;
  backlog_limit?: number;
  backlog_exceeded?: boolean;
  backlog_count?: number;
};
type HallCall = NonNullable<ApiResult["request"]>;
type Summary = {
  source: string;
  total_hall_calls: number;
  completed_hall_calls: number;
  active_hall_calls: number;
  active_car_calls?: number;
  active_requests?: number;
  backlog_limit?: number;
  backlog_exceeded?: boolean;
  backlog_count?: number;
  mean_wait_seconds: number | null;
  median_wait_seconds: number | null;
  p95_wait_seconds: number | null;
  completed_car_calls: number;
  active_incidents: number;
  fleet_available: number;
  fleet_count: number;
  events_count: number;
};
type Incident = NonNullable<ApiResult["incident"]>;
type SecurityAlert = {
  alert_id: string;
  severity: string;
  alert_type: string;
  credential_id: string | null;
  primary_car_id: string | null;
  secondary_car_id: string | null;
  details: string;
  status: string;
  created_at: string;
};
type StubEvent = { timestamp: string; event_type: string; elevator_id: string | null; detail: string };

async function getSummary(): Promise<Summary> {
  const response = await fetch("/api/v1/operational/summary");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json() as Promise<Summary>;
}

const floorLabel = (floor: number | null): string => floor === null ? "—" : floor === -1 ? "B1" : `F${floor}`;

const formatLocalTime = (value: string | null | undefined): string => {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
};

const toLocalDateTimeInput = (date: Date): string => {
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
};

const fromLocalDateTimeInput = (value: string): string => {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toISOString();
};

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

function FleetCards({ fleet, renderActions }: { fleet: FleetResponse | null; renderActions?: (car: ElevatorStatus) => ReactNode }) {
  return <div className="elevator-grid">{(fleet?.elevators ?? []).map((car) => (
    <article className="elevator-card" key={car.elevator_id}>
      <div className="card-top"><span className="elevator-id">{car.elevator_id}</span><span className={`badge ${car.available ? "ok" : "unknown"}`}>{car.state.replaceAll("_", " ")}</span></div>
      <div className="floor-display">{floorLabel(car.current_floor)}<span>floor</span></div>
      <div className="card-details"><span>Direction</span><strong>{car.direction}</strong></div>
      <div className="card-details"><span>Next stop</span><strong>{floorLabel(car.target_floor)}</strong></div>
      {car.active_fault && <div className="card-details"><span>Active fault</span><strong style={{ color: "#c8514c" }}>{car.active_fault}</strong></div>}
      <p className="card-note">{car.message_to_occupants || car.message}</p>
      {renderActions && renderActions(car)}
    </article>
  ))}</div>;
}

function Overview({ fleet, summary, token }: { fleet: FleetResponse | null; summary: Summary | null; token: string }) {
  const [events, setEvents] = useState<StubEvent[]>([]);
  const [eventsError, setEventsError] = useState("");

  const refreshEvents = useCallback(async () => {
    if (!token) {
      setEvents([]);
      setEventsError("");
      return;
    }
    try {
      const data = await authorizedApi<{ items: StubEvent[] }>("/api/v1/operational/events", token);
      setEvents([...(data.items ?? [])].reverse());
      setEventsError("");
    } catch (reason) {
      setEvents([]);
      setEventsError(reason instanceof Error ? reason.message : "Unable to load operational events");
    }
  }, [token]);

  useEffect(() => {
    void refreshEvents();
  }, [refreshEvents, summary?.events_count]);

  return <>
    <section className="hero-card"><div><p className="eyebrow">Live operations</p><h2>Elevator fleet status</h2><p className="muted">Displaying a software-only test stub. This is not physical elevator telemetry.</p></div><div className="hero-metric"><strong>{fleet?.elevators.length ?? "—"}</strong><span>cars monitored</span></div></section>
    <section className="summary-grid">
      <article className="summary-card"><span className="summary-icon blue">~</span><div><span>Active calls</span><strong>{summary?.active_hall_calls ?? "—"}</strong><small>In-memory hall calls</small></div></article>
      <article className="summary-card"><span className="summary-icon amber">!</span><div><span>Open incidents</span><strong>{summary?.active_incidents ?? "—"}</strong><small>Software test stub</small></div></article>
      <article className="summary-card"><span className="summary-icon green">+</span><div><span>Availability</span><strong>{summary ? `${summary.fleet_available}/${summary.fleet_count}` : "—"}</strong><small>Available for dispatch</small></div></article>
    </section>
    <section className="section"><div className="section-heading"><div><p className="eyebrow">Fleet</p><h2>Elevator snapshot</h2></div><span className="refresh-label">Software test-stub snapshot</span></div><FleetCards fleet={fleet} /></section>
    <section className="section panel">
      <div className="section-heading" style={{ marginBottom: "14px" }}>
        <div><p className="eyebrow">Activity</p><h2>Recent operations</h2></div>
        {token && <button className="button secondary" onClick={() => { void refreshEvents(); }}>Refresh</button>}
      </div>
      {!token ? (
        <EmptyState title="Dashboard sign-in required" detail="Sign in with an Admin credential in the header to view recent operational events." />
      ) : eventsError ? (
        <EmptyState title="Unable to load operational events" detail={eventsError} />
      ) : events.length === 0 ? (
        <EmptyState title="No operational events available" detail="Submit hall calls, destination requests, or simulator actions to see recent activity." />
      ) : (
        <div className="table-wrap">
          <table className="operations-table">
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>Event</th>
                <th>Car / Scope</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event, idx) => (
                <tr key={`${event.timestamp}-${event.event_type}-${idx}`}>
                  <td>{formatLocalTime(event.timestamp)}</td>
                  <td><span className="badge blue">{event.event_type.replaceAll("_", " ")}</span></td>
                  <td><strong>{event.elevator_id ?? "Building"}</strong></td>
                  <td>{event.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  </>;
}

function Elevators({ fleet, refresh, token, role }: { fleet: FleetResponse | null; refresh: () => Promise<void>; token: string; role: string }) {
  const [diagCar, setDiagCar] = useState<string | null>(null);
  const [confirmedByCar, setConfirmedByCar] = useState<Record<string, boolean>>({});
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState("");
  const [diag, setDiag] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const isStaff = Boolean(token) && (role === "ADMIN" || role === "TECHNICIAN");

  const loadDiagnostics = useCallback(async (carId: string, showBusy = true) => {
    if (!isStaff) return;
    if (showBusy) setBusy(true);
    setError("");
    try {
      const result = await authorizedApi<Record<string, unknown>>(`/api/v1/operational/diagnostics/${carId}`, token);
      setDiagCar(carId);
      setDiag(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to load diagnostics");
    } finally {
      if (showBusy) setBusy(false);
    }
  }, [isStaff, token]);

  useEffect(() => {
    let alive = true;
    let inFlight = false;
    const poll = () => {
      if (!alive || inFlight) return;
      inFlight = true;
      const tasks: Promise<unknown>[] = [refresh()];
      if (isStaff && diagCar) tasks.push(loadDiagnostics(diagCar, false));
      void Promise.all(tasks).finally(() => { inFlight = false; });
    };
    const timer = window.setInterval(poll, 750);
    return () => { alive = false; window.clearInterval(timer); };
  }, [refresh, isStaff, diagCar, loadDiagnostics]);

  const enterMaintenance = async (carId: string) => {
    if (!isStaff) return;
    setBusy(true);
    setError("");
    setFeedback("");
    try {
      const res = await authorizedApi<ApiResult>("/api/v1/acs/fleet/maintenance", token, {
        elevator_id: carId,
        operation_type: "ENTER_MAINTENANCE",
      });
      if (res.allowed === false) throw new Error(res.message ?? "Maintenance entry denied");
      setFeedback(res.message ?? `${carId} isolated in MAINTENANCE.`);
      await refresh();
      await loadDiagnostics(carId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to enter maintenance");
    } finally {
      setBusy(false);
    }
  };

  const returnToService = async (carId: string) => {
    if (!isStaff) return;
    const confirmed = Boolean(confirmedByCar[carId]);
    setBusy(true);
    setError("");
    setFeedback("");
    try {
      const res = await authorizedApi<ApiResult>("/api/v1/acs/fleet/return-to-service", token, {
        elevator_id: carId,
        technician_confirmation: confirmed,
      });
      if (res.allowed === false) throw new Error(res.message ?? "Return to service denied");
      setFeedback(res.message ?? `${carId} returned to IN_SERVICE.`);
      setConfirmedByCar((prev) => ({ ...prev, [carId]: false }));
      await refresh();
      await loadDiagnostics(carId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to return to service");
    } finally {
      setBusy(false);
    }
  };

  return <><PageHeader eyebrow="Fleet management" title="Elevators" detail="Live polling (750 ms), maintenance isolation, return-to-service clearance, and diagnostics for E1 to E5." action={<button className="button secondary" onClick={() => { void refresh(); }}>Refresh snapshot</button>} />
    {feedback && <p className="authorization-feedback">{feedback}</p>}
    {error && <p className="error-banner" role="alert">{error}</p>}
    <FleetCards fleet={fleet} renderActions={isStaff ? (car) => (
      <div style={{ marginTop: "12px", paddingTop: "10px", borderTop: "1px solid #edf0f4", display: "grid", gap: "8px" }}>
        <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
          <button className="button secondary" style={{ padding: "6px 9px", fontSize: "11px" }} disabled={busy} onClick={() => { void loadDiagnostics(car.elevator_id); }}>View Diagnostics</button>
          <button className="button secondary" style={{ padding: "6px 9px", fontSize: "11px" }} disabled={busy || car.state === "MAINTENANCE"} onClick={() => { void enterMaintenance(car.elevator_id); }}>Enter Maintenance</button>
        </div>
        <div style={{ paddingTop: "8px", borderTop: "1px dashed #dfe6ef", display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px", flexWrap: "wrap" }}>
          <label style={{ display: "flex", alignItems: "center", gap: "5px", fontSize: "11px", fontWeight: 600, color: "#354156" }}>
            <input type="checkbox" checked={Boolean(confirmedByCar[car.elevator_id])} onChange={(e) => setConfirmedByCar((prev) => ({ ...prev, [car.elevator_id]: e.target.checked }))} />
            Safety check confirmed
          </label>
          <button className="button primary" style={{ padding: "6px 9px", fontSize: "11px" }} disabled={busy || !confirmedByCar[car.elevator_id]} onClick={() => { void returnToService(car.elevator_id); }}>Return to Service</button>
        </div>
      </div>
    ) : undefined} />
    {!isStaff ? (
      <section className="section panel"><EmptyState title="Admin or Technician sign-in required for controls" detail="Sign in with an Admin or Technician credential to inspect per-car diagnostics, enter maintenance, or return cars to service." /></section>
    ) : diag && (
      <section className="section panel">
        <div className="section-heading"><div><p className="eyebrow">MNT-4.2 · MNT-4.5</p><h2>Live Diagnostics · {diagCar}</h2></div><button className="button secondary" onClick={() => { setDiag(null); setDiagCar(null); }}>Close</button></div>
        <pre className="event-row" style={{ marginTop: "12px" }}>{JSON.stringify(diag, null, 2)}</pre>
      </section>
    )}
    <section className="section panel"><div className="section-heading"><div><p className="eyebrow">Coverage</p><h2>Service matrix</h2></div></div><div className="matrix">{["Car", "Residential", "Freight", "Maintenance"].map((heading) => <strong key={heading}>{heading}</strong>)}{(fleet?.elevators ?? []).flatMap((car) => [<span key={`${car.elevator_id}-id`}>{car.elevator_id}</span>, <span className="matrix-value" key={`${car.elevator_id}-r`}>{car.elevator_id === "E5" ? "No" : "Yes"}</span>, <span className="matrix-value" key={`${car.elevator_id}-f`}>{car.elevator_id === "E5" ? "Yes" : "No"}</span>, <span className="matrix-value" key={`${car.elevator_id}-m`}>{car.state === "MAINTENANCE" ? "Yes" : "No"}</span>])}</div></section>
  </>;
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
  const [serviceFloor, setServiceFloor] = useState("10");
  const [servicePurpose, setServicePurpose] = useState("FREIGHT_DELIVERY");
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
      } catch (reason) {
        if (alive) setAnnouncement(reason instanceof Error ? reason.message : "Unable to refresh simulation");
      }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 750);
    return () => { alive = false; window.clearInterval(timer); };
  }, [updating]);

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
      const result = await swipeCabCard(selectedId, cardId, selectedCar.current_floor);
      return result.message ?? "Card processed";
    });
  };

  const selectDestination = (floor: number) => {
    if (!selectedCar) return;
    void runAction(async () => {
      const result = await requestCabDestination(selectedId, selectedCar.current_floor, floor);
      return result.message ?? `Destination ${floorLabel(floor)} accepted`;
    });
  };

  const startE5ServiceRun = () => {
    void runAction(async () => {
      const result = await postApi("/api/v1/acs/cab/e5/service-run", {
        elevator_id: "E5",
        destination_floor: Number(serviceFloor),
        purpose: servicePurpose.trim() || "FREIGHT_DELIVERY",
      });
      return result.message ?? `E5 exclusive service run started to ${floorLabel(Number(serviceFloor))}`;
    });
  };

  return <section className="simulation-page">
    <PageHeader eyebrow="Mode 1 · test-stub integration" title="Elevator operations" detail="Hall calls, direction-aware dispatch, and ACS-authorized destinations." action={<button className="button primary" onClick={() => setUpdating((on) => !on)}>{updating ? "Pause updates" : "Resume updates"}</button>} />
    <div className="simulation-notice"><strong>Software test stub</strong><span>These car positions come from the in-memory backend simulator. They are NOT live equipment readings. Pausing updates does not stop the server.</span><span className="srs-tag">SRS SWI-2</span></div>
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
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-8 · SEC-2.6</p><h2>{selectedId} operating panel</h2></div><span className={`state-pill ${selectedCar?.available ? "ok" : "warn"}`}>{selectedCar?.state.replaceAll("_", " ") ?? "Loading"}</span></div><div className="simulation-toolbar"><label>Selected car<select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{["E1", "E2", "E3", "E4", "E5"].map((id) => <option key={id}>{id}</option>)}</select></label><label className="card-reader-field">Card reader<input value={cardId} onChange={(event) => setCardId(event.target.value)} aria-label="Simulated card credential" /></label><button className="button secondary" disabled={busy || !selectedCar} onClick={swipe}>Present card</button></div><div className="car-state-grid"><div><span>Floor</span><strong>{floorLabel(selectedCar?.current_floor ?? null)}</strong></div><div><span>Direction</span><strong>{selectedCar?.direction ?? "—"}</strong></div><div><span>Door</span><strong>{selectedCar?.door_state ?? "—"}</strong></div><div><span>Next stop</span><strong>{floorLabel(selectedCar?.target_floor ?? null)}</strong></div></div><p className="authorization-feedback" aria-live="polite">{announcement}</p><div className="control-row"><button className="button secondary" disabled={busy || !selectedCar} onClick={() => doorCommand("OPEN")}>Door open</button><button className="button secondary" disabled={busy || !selectedCar} onClick={() => doorCommand("CLOSE")}>Door close</button><button className="button emergency-button" disabled={busy || !selectedCar} onClick={() => emergency("HELP")}>Emergency / help</button><button className="button medical-button" disabled={busy || !selectedCar} onClick={() => emergency("MEDICAL")}>Medical</button></div>{selectedId === "E5" && <div className="hall-controls" style={{ marginTop: "12px" }}><label>E5 service floor<select value={serviceFloor} onChange={(e) => setServiceFloor(e.target.value)}>{Array.from({ length: 30 }, (_, i) => i + 1).map((f) => <option key={f} value={f}>Floor {f}</option>)}</select></label><label>Purpose<input value={servicePurpose} onChange={(e) => setServicePurpose(e.target.value)} /></label><button className="button secondary freight-button" disabled={busy || !selectedCar?.available} onClick={startE5ServiceRun}>Start E5 Service Run</button></div>}<div className="destination-panel"><strong>Destination keys</strong><div className="destination-keys"><button disabled={busy || !selectedCar?.available || selectedId === "E5"} onClick={() => selectDestination(-1)}>B1</button>{Array.from({ length: 30 }, (_, index) => index + 1).map((floor) => <button disabled={busy || !selectedCar?.available} key={floor} onClick={() => selectDestination(floor)}>{floor}</button>)}</div></div></div>
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-9 · FR-1.3 to FR-1.7</p><h2>Hall panel</h2></div></div><div className="hall-controls"><label>Call floor<select value={hallFloor} onChange={(event) => setHallFloor(event.target.value)}>{SIM_FLOORS.map((floor) => <option key={floor} value={floor}>{floor === -1 ? "B1" : `Floor ${floor}`}</option>)}</select></label><button className="button secondary" disabled={busy || hallFloor === "30"} onClick={() => hallCall("ORDINARY", "UP")}>↑ Up</button><button className="button secondary" disabled={busy || hallFloor === "-1"} onClick={() => hallCall("ORDINARY", "DOWN")}>↓ Down</button><button className="button secondary freight-button" disabled={busy || hallFloor === "-1"} onClick={() => hallCall("FREIGHT")}>E5 Freight</button></div>{hallRequest && <div className="request-feedback" aria-live="polite"><strong>Request status: </strong>{hallRequest.status} · {hallRequest.assigned_car ?? "Unassigned"} · wait {hallRequest.wait_seconds ?? "—"}s</div>}</div>
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-3 · monitoring</p><h2>Fleet state and calls</h2></div></div><div className="sim-car-list">{cars.map((car) => <div className="sim-car-row" key={car.elevator_id}><span className="elevator-id">{car.elevator_id}</span><span className="sim-direction">{car.direction}</span><strong>{floorLabel(car.current_floor)}</strong><span>{car.state}</span><span className="weight">{car.pending_stops} stops</span></div>)}</div><p className="muted dark">{calls.filter((request) => request.status !== "COMPLETED").length} active hall requests. Data resets when the application restarts.</p></div>
      </div>
    </div>
  </section>;
}

type CredentialRecord = {
  card_id: string;
  user_id: string;
  full_name: string | null;
  role: string | null;
  apartment_unit: string | null;
  assigned_floor: number | null;
  status: string;
  suspension_reason: string | null;
  allowed_floors: number[];
  is_visitor_pass: boolean;
  host_user_id: string | null;
  issued_at: string | null;
  expires_at: string | null;
};

type UserRecord = {
  user_id: string;
  full_name: string;
  role: string;
  apartment_unit: string | null;
  assigned_floor: number | null;
};

const ROLE_LABELS: Record<string, string> = {
  RESIDENT: "Resident",
  VISITOR: "Visitor",
  BUILDING_SERVICE: "Building Service",
  TECHNICIAN: "Technician",
  SECURITY_OPERATOR: "Security Operator",
  ADMIN: "Administrator",
};

const STATUS_LABELS: Record<string, string> = {
  ACTIVE: "Active",
  SUSPENDED: "Suspended",
  REVOKED: "Revoked",
  EXPIRED: "Expired",
};

const SUSPENSION_REASON_LABELS: Record<string, string> = {
  LOST: "Lost Card",
  CLONE_SUSPECTED: "Suspected Clone",
  ADMIN_MANUAL: "Admin Action",
};

function formatRoleLabel(role: string | null | undefined): string {
  if (!role) return "—";
  return ROLE_LABELS[role] ?? role;
}

function formatStatusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return STATUS_LABELS[status] ?? status;
}

function formatSuspensionReason(reason: string | null | undefined): string {
  if (!reason) return "";
  return SUSPENSION_REASON_LABELS[reason] ?? reason;
}

function formatAllowedFloors(floors: number[] | null | undefined): string {
  if (!floors || floors.length === 0) return "None";
  const floorSet = new Set(floors);
  if (SIM_FLOORS.every((f) => floorSet.has(f))) {
    return "All floors";
  }
  return floors.map((f) => floorLabel(f)).join(", ");
}

function Credentials({ token, role }: { token: string; role: string }) {
  const [records, setRecords] = useState<CredentialRecord[]>([]);
  const [users, setUsers] = useState<UserRecord[]>([]);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("ALL");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState("");

  // Create user state
  const [newUserName, setNewUserName] = useState("");
  const [newUserRole, setNewUserRole] = useState("RESIDENT");
  const [newUserUnit, setNewUserUnit] = useState("10A");
  const [newUserFloor, setNewUserFloor] = useState("10");

  // Issue credential state
  const [issueAction, setIssueAction] = useState<"ISSUE" | "ISSUE_VISITOR_PASS">("ISSUE");
  const [issueUserId, setIssueUserId] = useState("");
  const [issueCardId, setIssueCardId] = useState("RES-CARD-1001");
  const [issueFloors, setIssueFloors] = useState("1,10");
  const [visitorHostId, setVisitorHostId] = useState("");
  const [visitorDurationHours, setVisitorDurationHours] = useState("24");

  // Replace lost card state
  const [lostCardId, setLostCardId] = useState("");
  const [replacementCardId, setReplacementCardId] = useState("REPL-CARD-2001");

  const refresh = useCallback(async () => {
    if (!token) {
      setRecords([]);
      setUsers([]);
      return;
    }
    try {
      const qs = new URLSearchParams();
      if (search.trim()) qs.set("search", search.trim());
      if (statusFilter !== "ALL") qs.set("status", statusFilter);
      const suffix = qs.toString() ? `?${qs.toString()}` : "";
      const data = await authorizedApi<{ items: CredentialRecord[]; users: UserRecord[]; count: number }>(
        `/api/v1/acs/credentials${suffix}`,
        token
      );
      setRecords(data.items ?? []);
      setUsers(data.users ?? []);
      if (!issueUserId && data.users?.length) {
        setIssueUserId(data.users[0].user_id);
      }
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to load credentials");
    }
  }, [token, search, statusFilter, issueUserId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const runManageAction = async (
    action: string,
    targetCredentialId?: string,
    targetUserId?: string,
    payloadDetails: Record<string, unknown> = {}
  ) => {
    setBusy(true);
    try {
      const res = await authorizedApi<ApiResult>("/api/v1/acs/credentials/manage", token, {
        action,
        target_credential_id: targetCredentialId ?? null,
        target_user_id: targetUserId ?? null,
        payload_details: payloadDetails,
      });
      setFeedback(res.message ?? `${action} completed`);
      setError("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `${action} failed`);
    } finally {
      setBusy(false);
    }
  };

  const handleCreateUser = async () => {
    if (!newUserName.trim()) return;
    setBusy(true);
    try {
      const res = await authorizedApi<{ user: UserRecord }>("/api/v1/acs/credentials/users", token, {
        full_name: newUserName.trim(),
        role: newUserRole,
        apartment_unit: newUserUnit.trim() || null,
        assigned_floor: newUserFloor.trim() ? Number(newUserFloor) : null,
      });
      setFeedback(`User created: ${res.user.full_name} (${formatRoleLabel(res.user.role)})`);
      setNewUserName("");
      setIssueUserId(res.user.user_id);
      setError("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to create user");
    } finally {
      setBusy(false);
    }
  };

  const handleIssueCredential = async () => {
    if (!issueUserId || !issueCardId.trim()) return;
    const parsedFloors = issueFloors
      .split(",")
      .map((s) => Number(s.trim()))
      .filter((n) => !Number.isNaN(n));
    const details: Record<string, unknown> = {
      allowed_floors: parsedFloors,
    };
    if (issueAction === "ISSUE_VISITOR_PASS") {
      details.host_user_id = visitorHostId || issueUserId;
      details.duration_hours = Number(visitorDurationHours) || 24;
      if (parsedFloors.length > 0) {
        details.destination_floor = parsedFloors[parsedFloors.length - 1];
      }
    }
    await runManageAction(issueAction, issueCardId.trim(), issueUserId, details);
  };

  const handleReplaceLost = async (cardToReplace?: string) => {
    const targetLost = (cardToReplace ?? lostCardId).trim();
    if (!targetLost || !replacementCardId.trim()) return;
    setBusy(true);
    try {
      const res = await authorizedApi<ApiResult>("/api/v1/acs/credentials/replace-lost", token, {
        lost_credential_id: targetLost,
        replacement_credential_id: replacementCardId.trim(),
        reason: "LOST_AND_REPLACED",
      });
      setFeedback(res.message ?? `Replaced ${targetLost} with ${replacementCardId.trim()}`);
      setLostCardId("");
      setError("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to replace lost card");
    } finally {
      setBusy(false);
    }
  };

  return <>
    <PageHeader
      eyebrow="Mode 2 · Access control (SEC-2.8 to SEC-2.12)"
      title="Credentials"
      detail="Manage cardholders, floor scopes, visitor passes, lost-card replacement, and credential lifecycle."
      action={<button className="button secondary" disabled={!token || busy} onClick={() => { void refresh(); }}>Refresh credentials</button>}
    />
    {!token ? (
      <EmptyState title="Dashboard sign-in required" detail="Sign in with an Admin or Security Operator card in the header to manage credentials." />
    ) : (
      <>
        {feedback && <p className="authorization-feedback" aria-live="polite">{feedback}</p>}
        {error && <p className="error-banner" role="alert">{error}</p>}

        {(role === "ADMIN" || role === "SECURITY_OPERATOR") && (
          <section className="panel section">
            <div className="section-heading" style={{ marginBottom: "12px" }}><h2 style={{ fontSize: "13px" }}>Register user &amp; issue credential</h2></div>
            <div className="hall-controls">
              <label>Full name<input value={newUserName} onChange={(e) => setNewUserName(e.target.value)} placeholder="Jane Resident" /></label>
              <label>Role<select value={newUserRole} onChange={(e) => setNewUserRole(e.target.value)}>
                <option value="RESIDENT">Resident</option>
                <option value="VISITOR">Visitor</option>
                <option value="BUILDING_SERVICE">Building Service</option>
                <option value="TECHNICIAN">Technician</option>
                <option value="ADMIN">Administrator</option>
              </select></label>
              <label>Unit<input value={newUserUnit} onChange={(e) => setNewUserUnit(e.target.value)} placeholder="10A" /></label>
              <label>Assigned floor<input type="number" min={-1} max={30} value={newUserFloor} onChange={(e) => setNewUserFloor(e.target.value)} /></label>
              <button className="button secondary" disabled={busy || !newUserName.trim()} onClick={() => { void handleCreateUser(); }}>Create user</button>
            </div>
            <div className="hall-controls" style={{ marginTop: "0.75rem" }}>
              <label>Credential type<select value={issueAction} onChange={(e) => setIssueAction(e.target.value as "ISSUE" | "ISSUE_VISITOR_PASS")}>
                <option value="ISSUE">Standard Credential</option>
                <option value="ISSUE_VISITOR_PASS">Visitor Pass</option>
              </select></label>
              <label>User<select value={issueUserId} onChange={(e) => setIssueUserId(e.target.value)}>
                {users.map((u) => <option key={u.user_id} value={u.user_id}>{u.full_name} ({formatRoleLabel(u.role)}{u.assigned_floor != null ? ` · ${floorLabel(u.assigned_floor)}` : ""})</option>)}
              </select></label>
              <label>Card ID<input value={issueCardId} onChange={(e) => setIssueCardId(e.target.value)} /></label>
              <label>Allowed floors (CSV)<input value={issueFloors} onChange={(e) => setIssueFloors(e.target.value)} placeholder="1,10" /></label>
              {issueAction === "ISSUE_VISITOR_PASS" && (
                <>
                  <label>Host user<select value={visitorHostId} onChange={(e) => setVisitorHostId(e.target.value)}>
                    <option value="">Same as selected user</option>
                    {users.map((u) => <option key={u.user_id} value={u.user_id}>{u.full_name} ({formatRoleLabel(u.role)})</option>)}
                  </select></label>
                  <label>Hours<input type="number" min={1} max={168} value={visitorDurationHours} onChange={(e) => setVisitorDurationHours(e.target.value)} /></label>
                </>
              )}
              <button className="button primary" disabled={busy || !issueUserId || !issueCardId.trim()} onClick={() => { void handleIssueCredential(); }}>Issue credential</button>
            </div>
          </section>
        )}

        {(role === "ADMIN" || role === "SECURITY_OPERATOR") && (
          <section className="panel section">
            <div className="section-heading" style={{ marginBottom: "12px" }}><h2 style={{ fontSize: "13px" }}>Lost-card replacement (SEC-2.10)</h2></div>
            <div className="hall-controls">
              <label>Lost card ID<input value={lostCardId} onChange={(e) => setLostCardId(e.target.value)} placeholder="CARD-LOST-01" /></label>
              <label>Replacement card ID<input value={replacementCardId} onChange={(e) => setReplacementCardId(e.target.value)} placeholder="REPL-CARD-2001" /></label>
              <button className="button secondary" disabled={busy || !lostCardId.trim() || !replacementCardId.trim()} onClick={() => { void handleReplaceLost(); }}>Revoke &amp; reissue</button>
            </div>
          </section>
        )}

        <div className="toolbar section">
          <input
            aria-label="Search credentials"
            placeholder="Search by name, card ID, role, or unit"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select aria-label="Credential status" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="ALL">All statuses</option>
            <option value="ACTIVE">Active</option>
            <option value="SUSPENDED">Suspended</option>
            <option value="REVOKED">Revoked</option>
            <option value="EXPIRED">Expired</option>
          </select>
        </div>

        <section className="panel section">
          <div className="table-heading"><strong>Cardholders &amp; credentials</strong><span>{records.length} records</span></div>
          {records.length === 0 ? (
            <EmptyState title="No matching credentials found" detail="Create a user and issue a credential above, or adjust the search/status filter." />
          ) : (
            records.map((rec) => (
              <article className="incident-card" key={rec.card_id}>
                <div>
                  <strong>{rec.card_id}</strong> · <span className={`badge ${rec.status === "ACTIVE" ? "blue" : "unknown"}`}>{formatStatusLabel(rec.status)}</span>
                  {rec.suspension_reason ? ` (${formatSuspensionReason(rec.suspension_reason)})` : ""}
                  {rec.is_visitor_pass ? " · Visitor Pass" : ""}
                </div>
                <p>
                  Holder: <strong>{rec.full_name ?? "Unassigned"}</strong> ({formatRoleLabel(rec.role)})
                  {rec.apartment_unit ? ` · Unit ${rec.apartment_unit}` : ""}
                </p>
                <small>
                  Allowed floors: {formatAllowedFloors(rec.allowed_floors)}
                  {rec.expires_at ? ` · Expires: ${formatLocalTime(rec.expires_at)}` : ""}
                </small>
                <div className="incident-actions">
                  {rec.status === "ACTIVE" && (
                    <>
                      <button className="button secondary" disabled={busy} onClick={() => { void runManageAction("SUSPEND", rec.card_id, rec.user_id, { suspension_reason: "LOST" }); }}>Report Lost (Suspend)</button>
                      <button className="button secondary" disabled={busy} onClick={() => { void runManageAction("SUSPEND", rec.card_id, rec.user_id, { suspension_reason: "ADMIN_MANUAL" }); }}>Suspend (Admin)</button>
                    </>
                  )}
                  {rec.status === "SUSPENDED" && (
                    <button className="button secondary" disabled={busy} onClick={() => { void runManageAction("REINSTATE", rec.card_id, rec.user_id, {}); }}>Reinstate</button>
                  )}
                  {rec.status !== "REVOKED" && (
                    <>
                      <button className="button emergency-button" disabled={busy} onClick={() => { void runManageAction("REVOKE", rec.card_id, rec.user_id, { reason: "MANUAL_REVOKE" }); }}>Revoke</button>
                      <button className="button secondary" disabled={busy} onClick={() => { setLostCardId(rec.card_id); }}>Select for lost replacement</button>
                    </>
                  )}
                </div>
              </article>
            ))
          )}
        </section>
      </>
    )}
  </>;
}

function Security({ token, role, refreshFleet }: { token: string; role: string; refreshFleet: () => Promise<void> }) {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [alerts, setAlerts] = useState<SecurityAlert[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [medicalFloor, setMedicalFloor] = useState("3");
  const refresh = useCallback(async () => {
    if (!token) { setIncidents([]); setAlerts([]); return; }
    try {
      const [incResult, alertResult] = await Promise.all([
        authorizedApi<{ items: Incident[] }>("/api/v1/operational/incidents", token),
        authorizedApi<{ items: SecurityAlert[] }>("/api/v1/acs/logs/alerts", token).catch(() => ({ items: [] as SecurityAlert[] })),
      ]);
      setIncidents(incResult.items);
      setAlerts(alertResult.items ?? []);
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Failed to load incidents"); }
  }, [token]);
  useEffect(() => {
    void refresh();
    if (!token) return;
    let alive = true;
    let inFlight = false;
    const timer = window.setInterval(() => {
      if (!alive || inFlight) return;
      inFlight = true;
      void refresh().finally(() => { inFlight = false; });
    }, 750);
    return () => { alive = false; window.clearInterval(timer); };
  }, [refresh, token]);
  const act = async (path: string, body: object, method: "POST" | "PATCH" = "POST") => {
    setBusy(true);
    try {
      await authorizedApi(path, token, body, method);
      setError(null);
      await refresh();
      await refreshFleet();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Operation failed"); }
    finally { setBusy(false); }
  };
  return <><PageHeader eyebrow="Mode 3 · software test stub" title="Incidents and emergency response" detail="Polls incidents and ACS security alerts every 750 ms (PERF-3)." action={<button className="button secondary" disabled={!token} onClick={() => { void refresh(); }}>Refresh incidents</button>} />
    {!token ? <EmptyState title="Dashboard sign-in required" detail="Present an Admin or Technician credential in the header to inspect and resolve simulated incidents." /> : <>
      {error && <p className="authorization-feedback" role="alert">{error}</p>}
      {role === "ADMIN" && <section className="panel section"><div className="section-heading"><h2>Admin demo controls</h2></div><div className="hall-controls"><label>Telephone medical floor<input type="number" min={1} max={30} value={medicalFloor} onChange={(e) => setMedicalFloor(e.target.value)} /></label><button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/phone-medical", { floor: Number(medicalFloor) }); }}>Record medical call</button><button className="button emergency-button" disabled={busy} onClick={() => { void act("/api/v1/operational/stub/building-event", { kind: "FIRE" }); }}>Inject fire (stub)</button></div></section>}
      <section className="panel section"><div className="table-heading"><strong>Security alerts (database)</strong><span>{alerts.length} records</span></div>
        {alerts.length === 0 ? <EmptyState title="No security alerts" detail="Repeated access failures, cloned cards, or card scanning alerts appear here within 1 second." /> : alerts.map((alert) =>
          <article className="incident-card" key={alert.alert_id}><div><strong>{alert.alert_type}</strong> · {alert.status} · {alert.severity}</div><p>{alert.details}</p><small>{alert.primary_car_id ?? "System"}{alert.secondary_car_id ? ` / ${alert.secondary_car_id}` : ""} · {alert.credential_id ?? "No card"} · {formatLocalTime(alert.created_at)}</small><div className="incident-actions">
            {role === "ADMIN" && alert.status === "OPEN" && <button className="button secondary" disabled={busy} onClick={() => { void act(`/api/v1/acs/logs/alerts/${alert.alert_id}`, { new_status: "ACKNOWLEDGED" }, "PATCH"); }}>Acknowledge alert</button>}
            {role === "ADMIN" && alert.status !== "RESOLVED" && <button className="button secondary" disabled={busy} onClick={() => { void act(`/api/v1/acs/logs/alerts/${alert.alert_id}`, { new_status: "RESOLVED" }, "PATCH"); }}>Resolve alert</button>}
          </div></article>
        )}</section>
      <section className="panel section"><div className="table-heading"><strong>Simulated incident queue</strong><span>{incidents.length} records</span></div>
        {incidents.length === 0 ? <EmptyState title="No incidents in memory" detail="Use HELP or MEDICAL on the simulator screen, or an Admin test event." /> : incidents.map((incident) =>
          <article className="incident-card" key={incident.incident_id}><div><strong>{incident.kind}</strong> · {incident.status} · {incident.severity}</div><p>{incident.details}</p><small>{incident.elevator_id ?? "Building"} · {floorLabel(incident.floor)} · {incident.incident_id}</small><div className="incident-actions">
            {incident.status === "OPEN" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/action", { incident_id: incident.incident_id, action: "ACKNOWLEDGE" }); }}>Acknowledge</button>}
            {(incident.status === "OPEN" || incident.status === "ACKNOWLEDGED") && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/action", { incident_id: incident.incident_id, action: "RESOLVE", recovery_confirmed: true }); }}>Resolve (after checks)</button>}
            {incident.status === "RESOLVED" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/action", { incident_id: incident.incident_id, action: "CLOSE" }); }}>Close</button>}
            {role === "ADMIN" && incident.kind === "HELP" && incident.status !== "RESOLVED" && incident.status !== "CLOSED" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/confirm-stuck", { incident_id: incident.incident_id }); }}>Confirm stuck</button>}
            {role === "ADMIN" && ["MEDICAL", "STUCK_CAR"].includes(incident.kind) && !incident.rescue_car_id && incident.status !== "CLOSED" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/rescue", { incident_id: incident.incident_id, confirmed_empty: true, wait_seconds: 120 }); }}>Send empty rescue car (confirm)</button>}
            {role === "ADMIN" && incident.kind === "MEDICAL" && incident.rescue_car_id && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/confirm-boarding", { incident_id: incident.incident_id }); }}>Confirm rescue boarding</button>}
          </div></article>
        )}</section>
    </>}
  </>;
}

type WaitTimeReport = {
  total_calls: number;
  completed_calls: number;
  dropped_calls: number;
  drop_rate_pct: number;
  mean_wait_seconds: number;
  median_wait_seconds: number;
  p90_wait_seconds: number;
  p95_wait_seconds: number;
  min_wait_seconds: number;
  max_wait_seconds: number;
  reassigned_calls_count: number;
  no_boarding_count: number;
  target_met: boolean;
};
type TravelTimeReport = {
  total_trips: number;
  mean_travel_seconds: number;
  median_travel_seconds: number;
  p90_travel_seconds: number;
  p95_travel_seconds: number;
  min_travel_seconds: number;
  max_travel_seconds: number;
  estimated_trips_count: number;
  service_runs_count: number;
};
type TripDistributionReport = {
  total_trips: number;
  up_trips_count: number;
  up_trips_share_pct: number;
  down_non_b1_trips_count: number;
  down_non_b1_trips_share_pct: number;
  down_to_b1_trips_count: number;
  down_to_b1_trips_share_pct: number;
  service_runs_count: number;
  service_runs_share_pct: number;
};
type CarUtilizationReport = {
  elevator_id: string;
  trip_count: number;
  motor_starts: number;
  floors_traversed: number;
  distance_meters: number;
  in_service_seconds: number;
  fault_seconds: number;
  maintenance_seconds: number;
  emergency_seconds: number;
  occupied_service_seconds: number;
  stale_seconds: number;
};
type DowntimeReport = {
  total_downtime_seconds: number;
  per_car_downtime_seconds: Record<string, number>;
  ongoing_intervals: Record<string, unknown>[];
  system_availability_pct: number;
  total_window_seconds: number;
  unannounced_downtime_seconds: number;
  announced_maintenance_seconds: number;
  meets_availability_target: boolean;
};
type SecuritySummaryReport = {
  total_denials: number;
  denials_by_reason: Record<string, number>;
  failed_auth_attempts: number;
  repeated_failure_alerts: number;
  cloned_credential_alerts: number;
  card_scanning_alerts: number;
  door_obstruction_alerts: number;
  external_validation_failures: number;
  emergency_incidents_by_type: Record<string, number>;
  total_incidents: number;
};
type DbAnalyticsBundle = {
  wait: WaitTimeReport;
  travel: TravelTimeReport;
  distribution: TripDistributionReport;
  utilization: Record<string, CarUtilizationReport>;
  downtime: DowntimeReport;
  security: SecuritySummaryReport;
};
type ExportResponse = {
  run_id: string;
  export_format: "JSON" | "CSV";
  generated_at: string;
  record_count: number;
  content: string;
};

function Analytics({ summary, token, role }: { summary: Summary | null; token?: string; role?: string }) {
  const [dbData, setDbData] = useState<DbAnalyticsBundle | null>(null);
  const [dbError, setDbError] = useState("");
  const [loading, setLoading] = useState(false);
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [elevatorFilter, setElevatorFilter] = useState("");
  const [exportStatus, setExportStatus] = useState("");

  const resolveWindow = useCallback(async (): Promise<{ startIso: string; endIso: string }> => {
    if (startTime.trim() && endTime.trim()) {
      return {
        startIso: fromLocalDateTimeInput(startTime.trim()),
        endIso: fromLocalDateTimeInput(endTime.trim()),
      };
    }
    let anchor = new Date();
    try {
      const clockRes = await fetch("/api/v1/system/clock");
      if (clockRes.ok) {
        const clockJson = (await clockRes.json()) as { current_sim_time?: string };
        if (clockJson.current_sim_time) anchor = new Date(clockJson.current_sim_time);
      }
    } catch { /* fall back to browser clock */ }
    const startDefault = new Date(anchor.getTime() - 24 * 3600 * 1000);
    const localEnd = endTime.trim() || toLocalDateTimeInput(anchor);
    const localStart = startTime.trim() || toLocalDateTimeInput(startDefault);
    if (!startTime) setStartTime(localStart);
    if (!endTime) setEndTime(localEnd);
    return {
      startIso: fromLocalDateTimeInput(localStart),
      endIso: fromLocalDateTimeInput(localEnd),
    };
  }, [startTime, endTime]);

  const fetchDbAnalytics = async () => {
    if (!token || role !== "ADMIN") return;
    setLoading(true);
    setDbError("");
    try {
      const { startIso, endIso } = await resolveWindow();
      const rangeQs = `start_time=${encodeURIComponent(startIso)}&end_time=${encodeURIComponent(endIso)}`;
      const carQs = elevatorFilter ? `${rangeQs}&elevator_id=${encodeURIComponent(elevatorFilter)}` : rangeQs;
      const [wait, travel, distribution, utilization, downtime, security] = await Promise.all([
        authorizedApi<WaitTimeReport>(`/api/v1/analytics/wait-times?${rangeQs}`, token),
        authorizedApi<TravelTimeReport>(`/api/v1/analytics/travel-times?${carQs}`, token),
        authorizedApi<TripDistributionReport>(`/api/v1/analytics/trip-distribution?${carQs}`, token),
        authorizedApi<Record<string, CarUtilizationReport>>(`/api/v1/analytics/utilization?${rangeQs}`, token),
        authorizedApi<DowntimeReport>(`/api/v1/analytics/downtime?${rangeQs}`, token),
        authorizedApi<SecuritySummaryReport>(`/api/v1/analytics/security?${rangeQs}`, token),
      ]);
      setDbData({ wait, travel, distribution, utilization, downtime, security });
    } catch (err) {
      setDbError(err instanceof Error ? err.message : "Failed to query database analytics");
    } finally {
      setLoading(false);
    }
  };

  const exportReport = async (exportFormat: "JSON" | "CSV") => {
    if (!token || role !== "ADMIN") return;
    setLoading(true);
    setDbError("");
    setExportStatus("");
    try {
      const { startIso, endIso } = await resolveWindow();
      const payload = await authorizedApi<ExportResponse>("/api/v1/analytics/export", token, {
        run_id: `ui-export-${Date.now()}`,
        export_format: exportFormat,
        include_per_request: true,
        start_time: startIso,
        end_time: endIso,
      });
      const mime = exportFormat === "CSV" ? "text/csv;charset=utf-8" : "application/json;charset=utf-8";
      const blob = new Blob([payload.content], { type: mime });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `srems-analytics-${payload.run_id}.${exportFormat.toLowerCase()}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setExportStatus(`Exported ${exportFormat} (${payload.record_count} records at ${formatLocalTime(payload.generated_at)}).`);
    } catch (err) {
      setDbError(err instanceof Error ? err.message : "Analytics export failed");
    } finally {
      setLoading(false);
    }
  };

  const values: { title: string; value: string; unit: string }[] = [
    { title: "Active request queue", value: String(summary?.active_requests ?? "—"), unit: `hall + car (limit ${summary?.backlog_limit ?? 100})` },
    { title: "Backlog status", value: summary?.backlog_exceeded ? `EXCEEDED (+${summary.backlog_count ?? 0})` : "Normal", unit: summary?.backlog_exceeded ? "PERF-5 threshold > 100" : "within capacity" },
    { title: "Mean hall wait", value: summary?.mean_wait_seconds == null ? "—" : summary.mean_wait_seconds.toFixed(1), unit: "seconds" },
    { title: "Median hall wait", value: summary?.median_wait_seconds == null ? "—" : summary.median_wait_seconds.toFixed(1), unit: "seconds" },
    { title: "P95 hall wait", value: summary?.p95_wait_seconds == null ? "—" : summary.p95_wait_seconds.toFixed(1), unit: "seconds" },
    { title: "Completed hall calls", value: String(summary?.completed_hall_calls ?? "—"), unit: "requests" },
    { title: "Completed car calls", value: String(summary?.completed_car_calls ?? "—"), unit: "requests" },
    { title: "Available cars", value: String(summary?.fleet_available ?? "—"), unit: "out of 5" },
  ];

  return <><PageHeader eyebrow="Mode 5 · Analytics & Reporting" title="Analytics" detail="Real-time operational queue, wait indicators, and persisted PostgreSQL SRS reports." action={token && role === "ADMIN" ? <button className="button secondary" disabled={loading} onClick={() => { void fetchDbAnalytics(); }}>{loading ? "Querying..." : "Query PostgreSQL analytics"}</button> : undefined} />
    {summary?.backlog_exceeded && <p className="error-banner" role="alert">Request backlog exceeded ({summary.active_requests ?? 0} active requests; {summary.backlog_count ?? 0} over limit of {summary.backlog_limit ?? 100}).</p>}
    {dbError && <p className="error-banner" role="alert">{dbError}</p>}
    {exportStatus && <p className="authorization-feedback">{exportStatus}</p>}
    <div className="kpi-grid">{values.map(({ title, value, unit }) => <article className="kpi-card" key={title}><span>{title}</span><strong>{value}</strong><small>{unit}</small></article>)}</div>
    {!token || role !== "ADMIN" ? (
      <EmptyState title="Admin sign-in required for PostgreSQL reports" detail="Sign in with an Admin credential in the header to query Mode 5 database analytics (ANA-5.2–ANA-5.6) and export JSON/CSV reports (ANA-5.9)." />
    ) : (
      <section className="panel section">
        <div className="table-heading"><strong>PostgreSQL Query &amp; Export Controls</strong><span>ANA-5.2 – ANA-5.9</span></div>
        <div className="hall-controls" style={{ marginTop: "14px" }}>
          <label>Start time (local)<input type="datetime-local" step={1} value={startTime} onChange={(e) => setStartTime(e.target.value)} /></label>
          <label>End time (local)<input type="datetime-local" step={1} value={endTime} onChange={(e) => setEndTime(e.target.value)} /></label>
          <label>Elevator filter<select value={elevatorFilter} onChange={(e) => setElevatorFilter(e.target.value)}><option value="">All cars (E1–E5)</option>{["E1", "E2", "E3", "E4", "E5"].map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
        </div>
        <div className="hall-controls" style={{ marginTop: "12px" }}>
          <button className="button primary" disabled={loading} onClick={() => { void fetchDbAnalytics(); }}>Query metrics</button>
          <button className="button secondary" disabled={loading} onClick={() => { void exportReport("JSON"); }}>Export JSON</button>
          <button className="button secondary" disabled={loading} onClick={() => { void exportReport("CSV"); }}>Export CSV</button>
        </div>
      </section>
    )}
    {dbData && <>
      <section className="panel section">
        <div className="table-heading"><strong>Wait &amp; Travel KPIs (ANA-5.2 &amp; ANA-5.3)</strong><span>{dbData.wait.target_met ? "Wait target met" : "Wait target missed"}</span></div>
        <div className="kpi-grid" style={{ marginTop: "12px" }}>
          <article className="kpi-card"><span>Hall calls (completed / total)</span><strong>{dbData.wait.completed_calls} / {dbData.wait.total_calls}</strong><small>dropped: {dbData.wait.dropped_calls} ({dbData.wait.drop_rate_pct.toFixed(1)}%)</small></article>
          <article className="kpi-card"><span>DB wait (mean / median / P90 / P95)</span><strong>{dbData.wait.mean_wait_seconds.toFixed(1)}s</strong><small>med {dbData.wait.median_wait_seconds.toFixed(1)}s · p90 {dbData.wait.p90_wait_seconds.toFixed(1)}s · p95 {dbData.wait.p95_wait_seconds.toFixed(1)}s</small></article>
          <article className="kpi-card"><span>Wait range &amp; exceptions</span><strong>{dbData.wait.min_wait_seconds.toFixed(1)}–{dbData.wait.max_wait_seconds.toFixed(1)}s</strong><small>reassigned: {dbData.wait.reassigned_calls_count} · no-boarding: {dbData.wait.no_boarding_count}</small></article>
          <article className="kpi-card"><span>Completed trips (ANA-5.3)</span><strong>{dbData.travel.total_trips}</strong><small>estimated: {dbData.travel.estimated_trips_count} · service runs: {dbData.travel.service_runs_count}</small></article>
          <article className="kpi-card"><span>Travel (mean / median / P90 / P95)</span><strong>{dbData.travel.mean_travel_seconds.toFixed(1)}s</strong><small>med {dbData.travel.median_travel_seconds.toFixed(1)}s · p90 {dbData.travel.p90_travel_seconds.toFixed(1)}s · p95 {dbData.travel.p95_travel_seconds.toFixed(1)}s</small></article>
          <article className="kpi-card"><span>Travel range</span><strong>{dbData.travel.min_travel_seconds.toFixed(1)}–{dbData.travel.max_travel_seconds.toFixed(1)}s</strong><small>cab motion legs</small></article>
        </div>
      </section>
      <section className="panel section">
        <div className="table-heading"><strong>Trip Distribution (OR-10 / BEN-4)</strong><span>{dbData.distribution.total_trips} total trips</span></div>
        <div className="kpi-grid" style={{ marginTop: "12px" }}>
          <article className="kpi-card"><span>UP trips</span><strong>{dbData.distribution.up_trips_count}</strong><small>{dbData.distribution.up_trips_share_pct.toFixed(1)}%</small></article>
          <article className="kpi-card"><span>DOWN (non-B1)</span><strong>{dbData.distribution.down_non_b1_trips_count}</strong><small>{dbData.distribution.down_non_b1_trips_share_pct.toFixed(1)}%</small></article>
          <article className="kpi-card"><span>DOWN to B1</span><strong>{dbData.distribution.down_to_b1_trips_count}</strong><small>{dbData.distribution.down_to_b1_trips_share_pct.toFixed(1)}%</small></article>
          <article className="kpi-card"><span>Freight / Service runs</span><strong>{dbData.distribution.service_runs_count}</strong><small>{dbData.distribution.service_runs_share_pct.toFixed(1)}%</small></article>
        </div>
      </section>
      <section className="panel section">
        <div className="table-heading"><strong>Per-Car Utilization (ANA-5.4)</strong><span>E1–E5</span></div>
        <div className="table-wrap" style={{ marginTop: "12px" }}>
          <table className="operations-table">
            <thead>
              <tr><th>Car</th><th>Trips</th><th>Starts</th><th>Floors</th><th>Distance (m)</th><th>In Service (s)</th><th>Occupied (s)</th><th>Maint (s)</th><th>Fault (s)</th><th>Emerg (s)</th></tr>
            </thead>
            <tbody>
              {Object.values(dbData.utilization).map((car) => (
                <tr key={car.elevator_id}>
                  <td><strong>{car.elevator_id}</strong></td>
                  <td>{car.trip_count}</td>
                  <td>{car.motor_starts}</td>
                  <td>{car.floors_traversed}</td>
                  <td>{car.distance_meters.toFixed(1)}</td>
                  <td>{car.in_service_seconds.toFixed(1)}</td>
                  <td>{car.occupied_service_seconds.toFixed(1)}</td>
                  <td>{car.maintenance_seconds.toFixed(1)}</td>
                  <td>{car.fault_seconds.toFixed(1)}</td>
                  <td>{car.emergency_seconds.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="panel section">
        <div className="table-heading"><strong>Availability &amp; Downtime (ANA-5.5 / AVL-1)</strong><span>{dbData.downtime.meets_availability_target ? "Meets >= 99.5% target" : "Below 99.5% target"}</span></div>
        <div className="kpi-grid" style={{ marginTop: "12px" }}>
          <article className="kpi-card"><span>System availability</span><strong>{dbData.downtime.system_availability_pct.toFixed(2)}%</strong><small>window: {dbData.downtime.total_window_seconds.toFixed(1)}s</small></article>
          <article className="kpi-card"><span>Unannounced downtime</span><strong>{dbData.downtime.unannounced_downtime_seconds.toFixed(1)}s</strong><small>host outage</small></article>
          <article className="kpi-card"><span>Announced maintenance</span><strong>{dbData.downtime.announced_maintenance_seconds.toFixed(1)}s</strong><small>excluded from SLA penalty</small></article>
          <article className="kpi-card"><span>Total car downtime</span><strong>{dbData.downtime.total_downtime_seconds.toFixed(1)}s</strong><small>ongoing intervals: {dbData.downtime.ongoing_intervals.length}</small></article>
        </div>
        {Object.keys(dbData.downtime.per_car_downtime_seconds).length > 0 && (
          <p className="event-row" style={{ marginTop: "10px" }}>
            Per-car downtime (s): {Object.entries(dbData.downtime.per_car_downtime_seconds).map(([car, sec]) => `${car}: ${sec.toFixed(1)}s`).join(" · ")}
          </p>
        )}
      </section>
      <section className="panel section">
        <div className="table-heading"><strong>Security &amp; Incidents Summary (ANA-5.6)</strong><span>{dbData.security.total_denials} denials · {dbData.security.total_incidents} incidents</span></div>
        <div className="kpi-grid" style={{ marginTop: "12px" }}>
          <article className="kpi-card"><span>Access denials</span><strong>{dbData.security.total_denials}</strong><small>failed auth: {dbData.security.failed_auth_attempts}</small></article>
          <article className="kpi-card"><span>Anomaly alerts</span><strong>{dbData.security.repeated_failure_alerts + dbData.security.cloned_credential_alerts + dbData.security.card_scanning_alerts}</strong><small>repeat {dbData.security.repeated_failure_alerts} · clone {dbData.security.cloned_credential_alerts} · scan {dbData.security.card_scanning_alerts}</small></article>
          <article className="kpi-card"><span>Operational / external alerts</span><strong>{dbData.security.door_obstruction_alerts + dbData.security.external_validation_failures}</strong><small>obstruction: {dbData.security.door_obstruction_alerts} · ext fail: {dbData.security.external_validation_failures}</small></article>
          <article className="kpi-card"><span>Emergency incidents</span><strong>{dbData.security.total_incidents}</strong><small>{Object.entries(dbData.security.emergency_incidents_by_type).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"}</small></article>
        </div>
        {Object.keys(dbData.security.denials_by_reason).length > 0 && (
          <p className="event-row" style={{ marginTop: "10px" }}>
            Denials by reason: {Object.entries(dbData.security.denials_by_reason).map(([reason, count]) => `${reason}: ${count}`).join(" · ")}
          </p>
        )}
      </section>
    </>}
    <section className="panel section"><h2>Measurement definition</h2><p>Hall wait time measures accepted hall call to car arrival. P95 uses the nearest-rank percentile. Live simulator events sync automatically to PostgreSQL when database connectivity is established.</p></section>
  </>;
}

type AuditTab = "audit" | "auth" | "incidents" | "alerts";

function Audit({ token, role }: { token: string; role: string }) {
  const [tab, setTab] = useState<AuditTab>("audit");
  const [logs, setLogs] = useState<Record<string, unknown>[]>([]);
  const [stubEvents, setStubEvents] = useState<StubEvent[]>([]);
  const [integrity, setIntegrity] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [actorFilter, setActorFilter] = useState("");
  const [carFilter, setCarFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [startFilter, setStartFilter] = useState("");
  const [endFilter, setEndFilter] = useState("");

  const refresh = useCallback(async () => {
    if (!token || role !== "ADMIN") { setLogs([]); setStubEvents([]); return; }
    try {
      const params = new URLSearchParams({ limit: "50" });
      if (startFilter.trim()) params.set("start_time", fromLocalDateTimeInput(startFilter.trim()));
      if (endFilter.trim()) params.set("end_time", fromLocalDateTimeInput(endFilter.trim()));
      let endpoint = "/api/v1/acs/logs/audit";
      if (tab === "audit") {
        if (actorFilter.trim()) params.set("actor_id", actorFilter.trim());
        if (typeFilter.trim()) params.set("action_type", typeFilter.trim());
        if (carFilter.trim()) params.set("target_id", carFilter.trim());
      } else if (tab === "auth") {
        endpoint = "/api/v1/acs/logs/authorization-events";
        if (actorFilter.trim()) params.set("credential_id", actorFilter.trim());
        if (carFilter.trim()) params.set("elevator_id", carFilter.trim());
        if (statusFilter.trim()) params.set("outcome", statusFilter.trim());
        if (typeFilter.trim()) params.set("denial_reason", typeFilter.trim());
      } else if (tab === "incidents") {
        endpoint = "/api/v1/acs/logs/incidents";
        if (statusFilter.trim()) params.set("status", statusFilter.trim());
        if (typeFilter.trim()) params.set("emergency_type", typeFilter.trim());
        if (carFilter.trim()) params.set("elevator_id", carFilter.trim());
      } else {
        endpoint = "/api/v1/acs/logs/alerts";
        if (statusFilter.trim()) params.set("status", statusFilter.trim());
        if (typeFilter.trim()) params.set("alert_type", typeFilter.trim());
        if (actorFilter.trim()) params.set("credential_id", actorFilter.trim());
      }
      const [data, events] = await Promise.all([
        authorizedApi<{ items: Record<string, unknown>[] }>(`${endpoint}?${params.toString()}`, token),
        authorizedApi<{ items: StubEvent[] }>("/api/v1/operational/events", token),
      ]);
      setLogs(data.items); setStubEvents(events.items); setError("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Audit retrieval failed"); }
  }, [token, role, tab, actorFilter, carFilter, typeFilter, statusFilter, startFilter, endFilter]);

  useEffect(() => { void refresh(); }, [refresh]);

  const verify = async () => {
    try {
      const result = await authorizedApi<ApiResult>("/api/v1/acs/logs/audit/verify", token, {});
      if (result.details?.passed) {
        setIntegrity(`Verified ${result.details.total_records_inspected ?? "?"} DB records`);
      } else {
        const idx = result.details?.mismatch_index;
        const why = result.details?.reason ?? result.message ?? "Hash mismatch";
        setIntegrity(`Audit-integrity check failed${idx != null ? ` at index ${idx}` : ""}: ${why}`);
      }
    } catch (reason) { setIntegrity(reason instanceof Error ? reason.message : "Verification failed"); }
  };

  return <><PageHeader eyebrow="Governance · SEC-2.14 & ANA-5.8" title="Audit log" detail="Searchable ACS audit chain, authorization events, DB incidents, and security alerts." action={<button className="button secondary" disabled={!token || role !== "ADMIN"} onClick={() => { void verify(); }}>Verify ACS hash chain</button>} />
    {!token || role !== "ADMIN" ? <EmptyState title="Admin sign-in required" detail="The real ACS audit log and investigation search are only accessible to authorized Admins." /> : <>
      {integrity && <p className="authorization-feedback">{integrity}</p>}{error && <p className="error-banner" role="alert">{error}</p>}
      <section className="panel section">
        <div className="hall-controls">
          <button className={`button ${tab === "audit" ? "primary" : "secondary"}`} onClick={() => setTab("audit")}>Audit Chain</button>
          <button className={`button ${tab === "auth" ? "primary" : "secondary"}`} onClick={() => setTab("auth")}>Authorization Events</button>
          <button className={`button ${tab === "incidents" ? "primary" : "secondary"}`} onClick={() => setTab("incidents")}>Incidents (DB)</button>
          <button className={`button ${tab === "alerts" ? "primary" : "secondary"}`} onClick={() => setTab("alerts")}>Security Alerts (DB)</button>
        </div>
        <div className="hall-controls" style={{ marginTop: "12px" }}>
          <label>Actor / Card<input value={actorFilter} onChange={(e) => setActorFilter(e.target.value)} placeholder="ADMIN-CARD-0001" /></label>
          <label>Elevator / Target<input value={carFilter} onChange={(e) => setCarFilter(e.target.value)} placeholder="E1..E5 or ID" /></label>
          <label>Action / Type / Reason<input value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} placeholder="e.g. FIRE, DENIED" /></label>
          <label>Status / Outcome<input value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} placeholder="e.g. ALLOWED, OPEN" /></label>
          <label>Start time (local)<input type="datetime-local" step={1} value={startFilter} onChange={(e) => setStartFilter(e.target.value)} /></label>
          <label>End time (local)<input type="datetime-local" step={1} value={endFilter} onChange={(e) => setEndFilter(e.target.value)} /></label>
          <button className="button primary" onClick={() => { void refresh(); }}>Search logs</button>
        </div>
      </section>
      <section className="panel section"><div className="table-heading"><strong>{tab === "audit" ? "ACS audit records" : tab === "auth" ? "Access authorization events" : tab === "incidents" ? "Persisted incidents" : "Security alerts"} · database</strong><span>{logs.length} shown</span></div>{logs.map((item, i) => <pre className="event-row" key={i}>{JSON.stringify({ ...item, ...(typeof item.timestamp === "string" ? { timestamp: formatLocalTime(item.timestamp) } : {}), ...(typeof item.created_at === "string" ? { created_at: formatLocalTime(item.created_at) } : {}) })}</pre>)}{!logs.length && <EmptyState title="No matching records loaded" detail="Adjust filters or verify PostgreSQL connectivity." />}</section>
      <section className="panel section"><div className="table-heading"><strong>Simulator event trace · not tamper-evident</strong><span>{stubEvents.length} shown</span></div>{stubEvents.map((event, i) => <p className="event-row" key={i}>{formatLocalTime(event.timestamp)} · {event.event_type} · {event.elevator_id ?? "Building"} · {event.detail}</p>)}</section>
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

  useEffect(() => {
    void onFleetRefresh();
    let alive = true;
    let inFlight = false;
    const timer = window.setInterval(() => {
      if (!alive || inFlight) return;
      inFlight = true;
      void onFleetRefresh().finally(() => { inFlight = false; });
    }, 750);
    return () => { alive = false; window.clearInterval(timer); };
  }, [onFleetRefresh]);

  const login = async () => {
    try {
      const response = await postApi("/api/v1/acs/auth/dashboard/login", { credential_id: credential });
      if (!response.allowed || !response.session_token) throw new Error(response.message ?? "No dashboard session issued");
      setToken(response.session_token); setRole(response.details?.role ?? "UNKNOWN"); setLoginError("");
    } catch (reason) { setToken(""); setRole(""); setLoginError(reason instanceof Error ? reason.message : "Login failed"); }
  };
  const logout = async () => {
    try { await authorizedApi("/api/v1/acs/auth/dashboard/logout", token, {}); }
    catch { /* Clear client session even when the server has expired it. */ }
    setToken(""); setRole("");
  };
  const page = view === "overview" ? <Overview fleet={fleet} summary={summary} token={token} /> : view === "simulation" ? <Simulation fleet={fleet} onFleetRefresh={onFleetRefresh} /> : view === "elevators" ? <Elevators fleet={fleet} refresh={onFleetRefresh} token={token} role={role} /> : view === "credentials" ? <Credentials token={token} role={role} /> : view === "security" ? <Security token={token} role={role} refreshFleet={onFleetRefresh} /> : view === "analytics" ? <Analytics summary={summary} token={token} role={role} /> : <Audit token={token} role={role} />;
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark">S</span><div><strong>SREMS</strong><span>Operations</span></div></div><nav aria-label="Primary navigation">{navigation.map((item) => <button className={`nav-item ${item.id === view ? "active" : ""}`} key={item.id} onClick={() => setView(item.id)}><span className="nav-icon">{item.icon}</span>{item.label}</button>)}</nav><div className="sidebar-footer"><span className="status-dot" /> {error ? "API unavailable" : fleet ? "Test stub connected" : "Connecting"}</div></aside>
    <main className="content"><header className="topbar"><div><p className="eyebrow">Building operations</p><div className="breadcrumb">SREMS / {active.label}</div></div><div className="operator">{token ? <><span className="avatar">{role.slice(0, 1)}</span><span><strong>{role}</strong><small>ACS session</small></span><button className="button secondary" onClick={() => { void logout(); }}>Log out</button></> : <><label className="login-field">Dashboard card<input aria-label="Dashboard card" value={credential} onChange={(event) => setCredential(event.target.value)} /></label><button className="button secondary" onClick={() => { void login(); }}>Sign in</button></>}</div></header>{summary?.backlog_exceeded && <div className="error-banner" role="alert">Request backlog exceeded: {summary.active_requests} active requests (limit {summary.backlog_limit ?? 100}, +{summary.backlog_count ?? 0} queued above limit).</div>}{(summary?.active_incidents ?? 0) > 0 && <div className="error-banner" role="alert">Critical alert: {summary?.active_incidents} active incident(s) require review.</div>}{loginError && <div className="error-banner" role="alert">{loginError}</div>}{error && <div className="error-banner" role="alert">{error}</div>}<div className="page-content">{page}</div></main>
  </div>;
}

export default App;
