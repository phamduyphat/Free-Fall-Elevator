import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { authorizedApi, postApi, requestCabDestination, swipeCabCard } from "./api.ts";
import type { ApiResult } from "./api.ts";

type View = "overview" | "simulation" | "wear_tear" | "elevators" | "credentials" | "security" | "analytics" | "audit";
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
  medical_run?: boolean;
  rescue_active?: boolean;
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

const isWarningOrError = (text: string): boolean => /failed|denied|error|invalid|rejected|forbidden|blocked|mismatch|unavailable/i.test(text);

const isEmergencyCar = (car: ElevatorStatus | undefined): boolean =>
  Boolean(car && (car.state === "EMERGENCY_UNAVAILABLE" || car.rescue_active));

const carDisplayState = (car: ElevatorStatus | undefined): string =>
  !car ? "Loading" : car.rescue_active ? "EMERGENCY RESCUE" : car.state.replaceAll("_", " ");

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

const navigation: { id: View; label: string; icon: ReactNode }[] = [
  {
    id: "overview",
    label: "Overview",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="9" rx="1" />
        <rect x="14" y="3" width="7" height="5" rx="1" />
        <rect x="14" y="12" width="7" height="9" rx="1" />
        <rect x="3" y="16" width="7" height="5" rx="1" />
      </svg>
    ),
  },
  {
    id: "simulation",
    label: "Simulator (stub)",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4" y="2" width="16" height="20" rx="2" />
        <path d="M9 10l3-3 3 3" />
        <path d="M9 14l3 3 3-3" />
      </svg>
    ),
  },
  {
    id: "wear_tear",
    label: "Wear & Tear",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 3v18" />
        <path d="M6 7l6-2 6 2" />
        <path d="M6 7L3 14h6L6 7z" />
        <path d="M18 7l-3 7h6l-3-7z" />
        <path d="M8 21h8" />
      </svg>
    ),
  },
  {
    id: "elevators",
    label: "Elevators",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <line x1="12" y1="3" x2="12" y2="21" />
        <path d="M7 8l2-2 2 2" />
        <path d="M17 16l-2 2-2-2" />
      </svg>
    ),
  },
  {
    id: "credentials",
    label: "Credentials",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="4" width="18" height="16" rx="2" />
        <circle cx="9" cy="10" r="2" />
        <path d="M15 8h2" />
        <path d="M15 12h2" />
        <path d="M7 16h10" />
      </svg>
    ),
  },
  {
    id: "security",
    label: "Incidents & diagnostics",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
    ),
  },
  {
    id: "analytics",
    label: "Analytics",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="18" y1="20" x2="18" y2="10" />
        <line x1="12" y1="20" x2="12" y2="4" />
        <line x1="6" y1="20" x2="6" y2="14" />
        <line x1="2" y1="20" x2="22" y2="20" />
      </svg>
    ),
  },
  {
    id: "audit",
    label: "Audit log",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="16" y1="13" x2="8" y2="13" />
        <line x1="16" y1="17" x2="8" y2="17" />
      </svg>
    ),
  },
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
      <div className="card-top"><span className="elevator-id">{car.elevator_id}</span><span className={`badge ${car.available ? "ok" : isEmergencyCar(car) ? "danger" : "unknown"}`}>{carDisplayState(car)}</span></div>
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
    if (showBusy) {
      setBusy(true);
      setError("");
    }
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
    const timer = window.setInterval(poll, 500);
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

  return <><PageHeader eyebrow="Fleet management" title="Elevators" detail="Live polling (500 ms), maintenance isolation, return-to-service clearance, and diagnostics for E1 to E5." action={<button className="button secondary" onClick={() => { void refresh(); }}>Refresh snapshot</button>} />
    {feedback && <p className="authorization-feedback">{feedback}</p>}
    {error && <p className="error-banner" role="alert">{error}</p>}
    <FleetCards fleet={fleet} renderActions={isStaff ? (car) => (
      <div style={{ marginTop: "auto", paddingTop: "12px", borderTop: "1px solid #edf0f4", display: "grid", gap: "8px" }}>
        <div style={{ display: "grid", gap: "8px" }}>
          <button className="button secondary" style={{ width: "100%" }} disabled={busy} onClick={() => { void loadDiagnostics(car.elevator_id); }}>View Diagnostics</button>
          <button className="button secondary" style={{ width: "100%" }} disabled={busy || car.state === "MAINTENANCE"} onClick={() => { void enterMaintenance(car.elevator_id); }}>Enter Maintenance</button>
        </div>
        <div style={{ paddingTop: "8px", borderTop: "1px dashed #dfe6ef", display: "grid", gap: "8px" }}>
          <label style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "12px", fontWeight: 600, color: "#354156" }}>
            <input type="checkbox" checked={Boolean(confirmedByCar[car.elevator_id])} onChange={(e) => setConfirmedByCar((prev) => ({ ...prev, [car.elevator_id]: e.target.checked }))} />
            Safety check confirmed
          </label>
          <button className="button primary" style={{ width: "100%" }} disabled={busy || !confirmedByCar[car.elevator_id]} onClick={() => { void returnToService(car.elevator_id); }}>Return to Service</button>
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

type LiveBenchmarkTrip = {
  trip_id: string;
  elevator_id: string;
  origin_floor: number;
  destination_floor: number;
  departure_time: string;
  arrival_time: string;
  travel_time_seconds: number;
  floors_traversed: number;
  motor_starts: number;
  is_service_run: boolean;
  traffic_profile: string;
};

type LiveBenchmarkMetrics = {
  scenario: string;
  total_calls: number;
  completed_calls: number;
  mean_wait_time: number;
  median_wait_time: number;
  p90_wait_time: number;
  p95_wait_time: number;
  max_wait_time: number;
  mean_transit_time: number;
  mean_journey_time: number;
  max_hall_queue_depth: number;
  awt_target_met: boolean;
  p95_target_met: boolean;
  queue_target_met: boolean;
  car_trips: Record<string, number>;
  wear_tear_enabled?: boolean;
  wear_tear_strategy?: string;
  wear_spread_pct?: number;
  wear_target_met?: boolean;
};

type LiveBenchmarkResponse = {
  metrics: LiveBenchmarkMetrics;
  summary_text: string;
  trips?: LiveBenchmarkTrip[];
};

type LoadBurstResponse = {
  injected_count: number;
  profile: string;
  active_calls_count: number;
  auto_step_seconds: number;
};

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

  // High-volume load generator & auto-traffic state
  const [loadProfile, setLoadProfile] = useState<string>("RANDOM");
  const [customBurstCount, setCustomBurstCount] = useState<number>(50);
  const [autoStepSeconds, setAutoStepSeconds] = useState<number>(0);
  const [autoTrafficRunning, setAutoTrafficRunning] = useState<boolean>(false);
  const [autoTrafficRate, setAutoTrafficRate] = useState<number>(3);
  const [autoTrafficInjected, setAutoTrafficInjected] = useState<number>(0);
  const [loadStatus, setLoadStatus] = useState<string>("");

  // Live Benchmark state
  const [benchScenario, setBenchScenario] = useState<string>("MORNING_PEAK");
  const [benchResetSim, setBenchResetSim] = useState<boolean>(false);
  const [benchWearEnabled, setBenchWearEnabled] = useState<boolean>(true);
  const [benchWearStrategy, setBenchWearStrategy] = useState<string>("BOTTOM_PRIORITY");
  const [benchTripFilter, setBenchTripFilter] = useState<string>("ALL");
  const [benchTripSearch, setBenchTripSearch] = useState<string>("" );
  const [benchRunning, setBenchRunning] = useState<boolean>(false);
  const [benchResult, setBenchResult] = useState<LiveBenchmarkResponse | null>(null);
  const [showBenchSummaryText, setShowBenchSummaryText] = useState<boolean>(false);
  const [showBenchTripsTable, setShowBenchTripsTable] = useState<boolean>(true);

  const allBenchmarkTrips = useMemo(() => benchResult?.trips ?? [], [benchResult]);
  const filteredBenchmarkTrips = useMemo(() => {
    return allBenchmarkTrips.filter((t) => {
      if (benchTripFilter !== "ALL" && t.elevator_id !== benchTripFilter) return false;
      if (benchTripSearch.trim()) {
        const query = benchTripSearch.toLowerCase();
        const originStr = floorLabel(t.origin_floor).toLowerCase();
        const destStr = floorLabel(t.destination_floor).toLowerCase();
        const profileStr = (t.traffic_profile || "").toLowerCase();
        if (!originStr.includes(query) && !destStr.includes(query) && !profileStr.includes(query)) {
          return false;
        }
      }
      return true;
    });
  }, [allBenchmarkTrips, benchTripFilter, benchTripSearch]);

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
    const timer = window.setInterval(() => { void refresh(); }, 500);
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

  // High-volume load burst injector
  const handleInjectBurst = async (count: number) => {
    if (busy || benchRunning) return;
    setBusy(true);
    try {
      const res = await postApi<LoadBurstResponse>("/api/v1/operational/load/burst", {
        count,
        profile: loadProfile,
        auto_step_seconds: autoStepSeconds,
      });
      setLoadStatus(`✓ Injected ${res.injected_count} ${res.profile} calls. Active hall calls: ${res.active_calls_count}.`);
      await onFleetRefresh();
    } catch (err) {
      setLoadStatus(err instanceof Error ? `Failed: ${err.message}` : "Failed to inject burst");
    } finally {
      setBusy(false);
    }
  };

  // Automatic background load generator
  useEffect(() => {
    if (!autoTrafficRunning) return;
    let active = true;
    const interval = window.setInterval(async () => {
      try {
        const res = await postApi<LoadBurstResponse>("/api/v1/operational/load/burst", {
          count: autoTrafficRate,
          profile: loadProfile,
          auto_step_seconds: 0,
        });
        if (active) {
          setAutoTrafficInjected((prev) => prev + res.injected_count);
          await onFleetRefresh();
        }
      } catch {
        // Continue auto-traffic interval even on transient errors
      }
    }, 1600);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [autoTrafficRunning, autoTrafficRate, loadProfile, onFleetRefresh]);

  // Live benchmark runner on active simulator
  const handleRunLiveBenchmark = async () => {
    if (benchRunning || busy) return;
    setBenchRunning(true);
    setLoadStatus(`Running live CIBSE benchmark (${benchScenario}) with wear balancing ${benchWearEnabled ? `[${benchWearStrategy}]` : "DISABLED"}...`);
    try {
      const res = await postApi<LiveBenchmarkResponse>("/api/v1/operational/benchmark/live", {
        scenario: benchScenario,
        reset_sim: benchResetSim,
        wear_tear_enabled: benchWearEnabled,
        strategy: benchWearStrategy,
      });
      setBenchResult(res);
      setLoadStatus(`✓ Benchmark "${benchScenario}" completed! AWT: ${res.metrics.mean_wait_time.toFixed(1)}s, Wear Spread: ${res.metrics.wear_spread_pct !== undefined ? `${res.metrics.wear_spread_pct.toFixed(2)}%` : "—"}.`);
      await onFleetRefresh();
    } catch (err) {
      setLoadStatus(err instanceof Error ? `Benchmark error: ${err.message}` : "Benchmark failed");
    } finally {
      setBenchRunning(false);
    }
  };

  return <section className="simulation-page">
    <PageHeader eyebrow="Mode 1 · test-stub integration" title="Elevator operations" detail="Hall calls, direction-aware dispatch, and ACS-authorized destinations." action={<button className="button primary" onClick={() => setUpdating((on) => !on)}>{updating ? "Pause updates" : "Resume updates"}</button>} />
    <div className="simulation-notice"><strong>Software test stub</strong><span>These car positions come from the in-memory backend simulator. They are NOT live equipment readings. Pausing updates does not stop the server.</span><span className="srs-tag">SRS SWI-2</span></div>
    <div className="simulation-layout">
      <div className="shaft-panel">
        <div className="shaft-labels">{[...SIM_FLOORS].reverse().map((floor) => <span key={floor}>{floorLabel(floor)}</span>)}</div>
        <div className="shaft-grid">{[...SIM_FLOORS].reverse().map((floor) => <div className="floor-line" key={floor}><span>{floor === -1 ? "Basement 1" : `Floor ${floor}`}</span></div>)}
          {cars.map((car) => <div className={`sim-car ${isEmergencyCar(car) ? "emergency-car" : ""} ${car.state === "MAINTENANCE" ? "maintenance-car" : ""}`} key={car.elevator_id} style={{ bottom: `${((car.current_floor === -1 ? 0 : car.current_floor) / 30) * 100}%`, left: `${(Number(car.elevator_id.slice(1)) - 1) * 20 + 1}%` }} title={`${car.elevator_id}: ${floorLabel(car.current_floor)}`}>
            <div className="sim-car-header"><strong>{car.elevator_id}</strong><span>{car.direction === "UP" ? "↑" : car.direction === "DOWN" ? "↓" : "·"}</span></div>
            <small>{floorLabel(car.current_floor)}</small>
          </div>)}
        </div>
      </div>
      <div className="simulation-side">
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-8 · SEC-2.6</p><h2>{selectedId} operating panel</h2></div><span className={`state-pill ${selectedCar?.available ? "ok" : isEmergencyCar(selectedCar) ? "danger" : "warn"}`}>{carDisplayState(selectedCar)}</span></div><div className="simulation-toolbar"><label>Selected car<select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{["E1", "E2", "E3", "E4", "E5"].map((id) => <option key={id}>{id}</option>)}</select></label><label className="card-reader-field">Card reader<input value={cardId} onChange={(event) => setCardId(event.target.value)} aria-label="Simulated card credential" /></label><button className="button secondary" disabled={busy || !selectedCar} onClick={swipe}>Present card</button></div><div className="car-state-grid"><div><span>Floor</span><strong>{floorLabel(selectedCar?.current_floor ?? null)}</strong></div><div><span>Direction</span><strong>{selectedCar?.direction ?? "—"}</strong></div><div><span>Door</span><strong>{selectedCar?.door_state ?? "—"}</strong></div><div><span>Next stop</span><strong>{floorLabel(selectedCar?.target_floor ?? null)}</strong></div></div><p className={isWarningOrError(announcement) ? "authorization-feedback danger-text" : "authorization-feedback"} aria-live="polite">{announcement}</p><div className="control-row"><button className="button secondary" disabled={busy || !selectedCar} onClick={() => doorCommand("OPEN")}>Door open</button><button className="button secondary" disabled={busy || !selectedCar || Boolean(selectedCar?.rescue_active)} title={selectedCar?.rescue_active ? "Door close disabled during active rescue wait" : undefined} onClick={() => doorCommand("CLOSE")}>Door close</button><button className="button emergency-button" disabled={busy || !selectedCar} onClick={() => emergency("HELP")}>Emergency / help</button><button className="button medical-button" disabled={busy || !selectedCar} onClick={() => emergency("MEDICAL")}>Medical</button></div>{selectedId === "E5" && <div className="hall-controls" style={{ marginTop: "12px" }}><label>E5 service floor<select value={serviceFloor} onChange={(e) => setServiceFloor(e.target.value)}>{Array.from({ length: 30 }, (_, i) => i + 1).map((f) => <option key={f} value={f}>Floor {f}</option>)}</select></label><label>Purpose<input value={servicePurpose} onChange={(e) => setServicePurpose(e.target.value)} /></label><button className="button secondary freight-button" disabled={busy || !selectedCar?.available} onClick={startE5ServiceRun}>Start E5 Service Run</button></div>}<div className="destination-panel"><strong>Destination keys</strong><div className="destination-keys"><button disabled={busy || !selectedCar?.available || selectedId === "E5"} onClick={() => selectDestination(-1)}>B1</button>{Array.from({ length: 30 }, (_, index) => index + 1).map((floor) => <button disabled={busy || !selectedCar?.available} key={floor} onClick={() => selectDestination(floor)}>{floor}</button>)}</div></div></div>
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-9 · FR-1.3 to FR-1.7</p><h2>Hall panel</h2></div></div><div className="hall-controls"><label>Call floor<select value={hallFloor} onChange={(event) => setHallFloor(event.target.value)}>{SIM_FLOORS.map((floor) => <option key={floor} value={floor}>{floor === -1 ? "B1" : `Floor ${floor}`}</option>)}</select></label><button className="button secondary" disabled={busy || hallFloor === "30"} onClick={() => hallCall("ORDINARY", "UP")}>↑ Up</button><button className="button secondary" disabled={busy || hallFloor === "-1"} onClick={() => hallCall("ORDINARY", "DOWN")}>↓ Down</button><button className="button secondary freight-button" disabled={busy || hallFloor === "-1"} onClick={() => hallCall("FREIGHT")}>E5 Freight</button></div>{hallRequest && <div className="request-feedback" aria-live="polite"><strong>Request status: </strong>{hallRequest.status} · {hallRequest.assigned_car ?? "Unassigned"} · wait {hallRequest.wait_seconds ?? "—"}s</div>}</div>
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-3 · monitoring</p><h2>Fleet state and calls</h2></div></div><div className="sim-car-list">{cars.map((car) => <div className="sim-car-row" key={car.elevator_id}><span className="elevator-id">{car.elevator_id}</span><span className="sim-direction">{car.direction}</span><strong>{floorLabel(car.current_floor)}</strong><span>{carDisplayState(car)}</span><span className="weight">{car.pending_stops} stops</span></div>)}</div><p className="muted dark">{calls.filter((request) => request.status !== "COMPLETED").length} active hall requests. Data resets when the application restarts.</p></div>
      </div>
    </div>

    {/* Dedicated Section for Live Benchmark & High-Volume Load Generator */}
    <div className="sim-tools-grid">
      {/* High-Volume Simulated Load & Continuous Auto-Pilot */}
      <div className="panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">CAP-1 · Load Testing</p>
            <h2>Simulated Load Generator</h2>
          </div>
          <span className={`state-pill ${autoTrafficRunning ? "ok" : "warn"}`}>
            {autoTrafficRunning ? "Auto-Traffic Active" : "Manual / Ready"}
          </span>
        </div>

        <p className="muted dark" style={{ marginTop: 0 }}>
          Inject reproducible high-volume bursts or enable auto-pilot traffic into the live simulator.
        </p>

        <div className="simulation-toolbar" style={{ marginTop: "10px" }}>
          <label>
            Traffic Profile
            <select value={loadProfile} onChange={(e) => setLoadProfile(e.target.value)}>
              <option value="RANDOM">Uniform Random Distribution</option>
              <option value="MORNING_PEAK">Morning Peak (80% Up-Peak from B1/L1)</option>
              <option value="EVENING_PEAK">Evening Peak (80% Down-Peak to L1)</option>
              <option value="TWO_WAY">Two-Way Inter-Floor Traffic</option>
              <option value="FREIGHT_E5">Freight E5 Exclusive Service</option>
            </select>
          </label>

          <label>
            Simulation Step
            <select value={autoStepSeconds} onChange={(e) => setAutoStepSeconds(Number(e.target.value))}>
              <option value={0}>0s (Real-time live animation)</option>
              <option value={30}>Advance 30s</option>
              <option value={60}>Advance 60s</option>
              <option value={120}>Advance 120s</option>
              <option value={300}>Advance 300s (Fast-forward)</option>
            </select>
          </label>
        </div>

        <div style={{ marginTop: "14px" }}>
          <strong style={{ fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.06em", color: "#6f7c92" }}>
            Quick Burst Injections
          </strong>
          <div className="burst-button-row">
            <button className="burst-btn" disabled={busy || benchRunning} onClick={() => void handleInjectBurst(25)}>
              +25 Calls
            </button>
            <button className="burst-btn" disabled={busy || benchRunning} onClick={() => void handleInjectBurst(50)}>
              +50 Calls
            </button>
            <button className="burst-btn stress" disabled={busy || benchRunning} onClick={() => void handleInjectBurst(100)}>
              +100 Calls (CAP-1 Stress)
            </button>
            <button className="burst-btn" disabled={busy || benchRunning} onClick={() => void handleInjectBurst(250)}>
              +250 Calls
            </button>
            <button className="burst-btn stress" disabled={busy || benchRunning} onClick={() => void handleInjectBurst(500)}>
              +500 Mega-Burst
            </button>
          </div>
        </div>

        <div className="simulation-toolbar" style={{ marginTop: "10px" }}>
          <label style={{ flex: 1, minWidth: "120px" }}>
            Custom Count
            <input
              type="number"
              min={1}
              max={1000}
              value={customBurstCount}
              onChange={(e) => setCustomBurstCount(Math.max(1, Number(e.target.value)))}
            />
          </label>
          <button
            className="button secondary"
            disabled={busy || benchRunning}
            onClick={() => void handleInjectBurst(customBurstCount)}
          >
            Inject {customBurstCount} Calls
          </button>
        </div>

        {/* Automatic Traffic Generator */}
        <div className="auto-traffic-bar">
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <span className={`pulse-indicator ${autoTrafficRunning ? "active" : "inactive"}`} />
            <div>
              <strong style={{ fontSize: "12px", color: "#354156", display: "block" }}>
                Auto-Pilot Traffic Generator
              </strong>
              <small style={{ color: "#7d899d", fontSize: "11px" }}>
                {autoTrafficRunning
                  ? `Injecting ${autoTrafficRate} calls / 1.6s · Total injected: ${autoTrafficInjected}`
                  : "Continuous automated passenger flow is paused"}
              </small>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <label style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "11px", color: "#596a7d" }}>
              Rate:
              <select
                value={autoTrafficRate}
                disabled={autoTrafficRunning}
                onChange={(e) => setAutoTrafficRate(Number(e.target.value))}
                style={{ padding: "4px 8px", fontSize: "11px" }}
              >
                <option value={1}>Light (1 / 1.6s)</option>
                <option value={3}>Medium (3 / 1.6s)</option>
                <option value={6}>Rush (6 / 1.6s)</option>
                <option value={10}>Heavy (10 / 1.6s)</option>
              </select>
            </label>
            <button
              className={`button ${autoTrafficRunning ? "emergency-button" : "primary"}`}
              style={{ padding: "7px 14px", fontSize: "11px" }}
              onClick={() => setAutoTrafficRunning((prev) => !prev)}
            >
              {autoTrafficRunning ? "Stop Auto-Traffic" : "Start Auto-Traffic"}
            </button>
          </div>
        </div>

        {loadStatus && (
          <p className="authorization-feedback" style={{ marginTop: "12px" }}>
            {loadStatus}
          </p>
        )}
      </div>

      {/* Live Simulation CIBSE Guide D Benchmark Runner */}
      <div className="panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">CIBSE Guide D · Live Benchmark</p>
            <h2>Run Benchmark in Live Simulation</h2>
          </div>
          <span className="wear-badge info">Standardized</span>
        </div>

        <p className="muted dark" style={{ marginTop: 0 }}>
          Execute standardized CIBSE traffic benchmarks directly on the active simulation fleet.
        </p>

        <div className="simulation-toolbar" style={{ marginTop: "10px" }}>
          <label style={{ flex: 1 }}>
            Benchmark Scenario
            <select value={benchScenario} onChange={(e) => setBenchScenario(e.target.value)}>
              <option value="MORNING_PEAK">CIBSE Morning Peak (50 calls, 80% up)</option>
              <option value="EVENING_PEAK">CIBSE Evening Peak (50 calls, 80% down)</option>
              <option value="OFF_PEAK">CIBSE Off-Peak (30 calls, balanced)</option>
              <option value="STRESS">CAP-1 Stress Burst (100 calls)</option>
            </select>
          </label>

          <label style={{ flex: 1 }}>
            Wear &amp; Tear Strategy
            <select
              value={benchWearStrategy}
              disabled={!benchWearEnabled}
              onChange={(e) => setBenchWearStrategy(e.target.value)}
            >
              <option value="BOTTOM_PRIORITY">1. Bottom Threshold Priority</option>
              <option value="TOP_HALT">2. Top Threshold Halt</option>
              <option value="LEAST_RUN_FIRST">3. Least-Run Priority</option>
              <option value="OFF_PEAK_REBALANCE">4. Off-Peak Rebalance</option>
              <option value="OUTLIER_CORRECTION">5. Statistical Outlier</option>
            </select>
          </label>
        </div>

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: "12px", gap: "12px", flexWrap: "wrap" }}>
          <div style={{ display: "flex", gap: "16px", flexWrap: "wrap" }}>
            <label style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "12px", color: "#596a7d", cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={benchWearEnabled}
                onChange={(e) => setBenchWearEnabled(e.target.checked)}
              />
              Follow Wear &amp; Tear Balancing
            </label>

            <label style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "12px", color: "#596a7d", cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={benchResetSim}
                onChange={(e) => setBenchResetSim(e.target.checked)}
              />
              Reset simulator before benchmark
            </label>
          </div>

          <button
            className="button primary"
            disabled={benchRunning || busy}
            onClick={() => void handleRunLiveBenchmark()}
          >
            {benchRunning ? "Running Benchmark..." : "▶ Run Live Benchmark"}
          </button>
        </div>

        {/* Benchmark Results Display */}
        {benchResult && (
          <div style={{ marginTop: "18px", borderTop: "1px solid #edf0f4", paddingTop: "14px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
              <strong style={{ fontSize: "13px", color: "#172033" }}>{benchResult.metrics.scenario}</strong>
              <small style={{ color: "#7d899d" }}>
                {benchResult.metrics.completed_calls} / {benchResult.metrics.total_calls} calls completed
              </small>
            </div>

            {/* Compliance Pills */}
            <div className="bench-compliance-row">
              <span className={`wear-badge ${benchResult.metrics.awt_target_met ? "pass" : "fail"}`}>
                AWT &lt; 30s: {benchResult.metrics.awt_target_met ? "PASSED" : "FAILED"} ({benchResult.metrics.mean_wait_time.toFixed(1)}s)
              </span>
              <span className={`wear-badge ${benchResult.metrics.p95_target_met ? "pass" : "fail"}`}>
                P95 &lt; 60s: {benchResult.metrics.p95_target_met ? "PASSED" : "FAILED"} ({benchResult.metrics.p95_wait_time.toFixed(1)}s)
              </span>
              <span className={`wear-badge ${benchResult.metrics.queue_target_met ? "pass" : "fail"}`}>
                Queue &lt; 15: {benchResult.metrics.queue_target_met ? "PASSED" : "FAILED"} (Peak: {benchResult.metrics.max_hall_queue_depth})
              </span>
              {benchResult.metrics.wear_spread_pct !== undefined && (
                <span className={`wear-badge ${benchResult.metrics.wear_target_met ? "pass" : "fail"}`}>
                  Wear Spread: {benchResult.metrics.wear_spread_pct.toFixed(2)}% ({benchResult.metrics.wear_target_met ? "PASSED [<= 1.8%]" : "EXCEEDED"})
                </span>
              )}
            </div>

            {/* KPI Metric Tiles */}
            <div className="bench-kpi-grid">
              <div className="bench-kpi-tile">
                <span>Avg Wait (AWT)</span>
                <strong>{benchResult.metrics.mean_wait_time.toFixed(1)}s</strong>
                <small>Target: &lt; 30.0s</small>
              </div>
              <div className="bench-kpi-tile">
                <span>P95 Wait</span>
                <strong>{benchResult.metrics.p95_wait_time.toFixed(1)}s</strong>
                <small>Target: &lt; 60.0s</small>
              </div>
              <div className="bench-kpi-tile">
                <span>Median Wait</span>
                <strong>{benchResult.metrics.median_wait_time.toFixed(1)}s</strong>
                <small>P90: {benchResult.metrics.p90_wait_time.toFixed(1)}s</small>
              </div>
              <div className="bench-kpi-tile">
                <span>Max Wait</span>
                <strong>{benchResult.metrics.max_wait_time.toFixed(1)}s</strong>
                <small>Worst case</small>
              </div>
              <div className="bench-kpi-tile">
                <span>Mean Transit</span>
                <strong>{benchResult.metrics.mean_transit_time.toFixed(1)}s</strong>
                <small>In-cab ride</small>
              </div>
              <div className="bench-kpi-tile">
                <span>Total Journey</span>
                <strong>{benchResult.metrics.mean_journey_time.toFixed(1)}s</strong>
                <small>Wait + Transit</small>
              </div>
            </div>

            {/* Car Trip Distribution */}
            <div style={{ marginTop: "12px", background: "#f8fafc", padding: "10px", borderRadius: "8px", border: "1px solid #e2e8f0" }}>
              <span style={{ fontSize: "11px", fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: "0.06em", display: "block", marginBottom: "6px" }}>
                Fleet Dispatch Distribution (Completed Trips)
              </span>
              <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
                {Object.entries(benchResult.metrics.car_trips).map(([cid, trips]) => (
                  <div key={cid} style={{ background: "#fff", border: "1px solid #dfe6ef", borderRadius: "6px", padding: "4px 10px", fontSize: "12px" }}>
                    <strong style={{ color: "#147aca" }}>{cid}: </strong>
                    <span style={{ color: "#354156", fontWeight: 600 }}>{trips} trips</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Individual Benchmark Trips Section */}
            <div style={{ marginTop: "16px", borderTop: "1px solid #edf0f4", paddingTop: "12px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "10px", flexWrap: "wrap", gap: "8px" }}>
                <div>
                  <strong style={{ fontSize: "12px", color: "#172033" }}>Individual Benchmark Trips Log</strong>
                  <span style={{ fontSize: "11px", color: "#7d899d", marginLeft: "8px" }}>
                    ({filteredBenchmarkTrips.length} of {allBenchmarkTrips.length} trips)
                  </span>
                </div>
                <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                  <button
                    className="button secondary"
                    style={{ padding: "4px 8px", fontSize: "10px" }}
                    onClick={() => setShowBenchTripsTable((prev) => !prev)}
                  >
                    {showBenchTripsTable ? "Hide Trips Table" : "Show Trips Table"}
                  </button>
                </div>
              </div>

              {showBenchTripsTable && (
                <>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px", marginBottom: "10px", flexWrap: "wrap" }}>
                    <div style={{ display: "flex", gap: "4px" }}>
                      {["ALL", "E1", "E2", "E3", "E4", "E5"].map((cid) => (
                        <button
                          key={cid}
                          className={`burst-btn ${benchTripFilter === cid ? "stress" : ""}`}
                          style={{ padding: "3px 8px", fontSize: "10px" }}
                          onClick={() => setBenchTripFilter(cid)}
                        >
                          {cid}
                        </button>
                      ))}
                    </div>
                    <input
                      type="text"
                      placeholder="Filter floor or profile..."
                      value={benchTripSearch}
                      onChange={(e) => setBenchTripSearch(e.target.value)}
                      style={{ padding: "4px 8px", fontSize: "11px", width: "170px", border: "1px solid #dfe6ef", borderRadius: "6px" }}
                    />
                  </div>

                  <div className="table-wrap" style={{ maxHeight: "260px", overflowY: "auto", border: "1px solid #edf0f4", borderRadius: "8px" }}>
                    <table className="operations-table" style={{ fontSize: "11px", width: "100%" }}>
                      <thead style={{ position: "sticky", top: 0, background: "#f8fafc", zIndex: 2 }}>
                        <tr>
                          <th>#</th>
                          <th>Unit</th>
                          <th>Route (Origin &rarr; Dest)</th>
                          <th>Distance</th>
                          <th>Travel Time</th>
                          <th>Type / Profile</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredBenchmarkTrips.length === 0 ? (
                          <tr>
                            <td colSpan={6} style={{ textAlign: "center", color: "#8d99ad", padding: "16px" }}>
                              No benchmark trips match filter.
                            </td>
                          </tr>
                        ) : (
                          filteredBenchmarkTrips.map((t, idx) => (
                            <tr key={t.trip_id || idx}>
                              <td><span style={{ color: "#7d899d" }}>#{idx + 1}</span></td>
                              <td>
                                <strong style={{ color: t.elevator_id === "E5" ? "#b57d18" : "#147aca" }}>
                                  {t.elevator_id}
                                </strong>
                              </td>
                              <td>
                                {floorLabel(t.origin_floor)} &rarr; {floorLabel(t.destination_floor)}
                              </td>
                              <td>{t.floors_traversed} fl</td>
                              <td>{t.travel_time_seconds.toFixed(1)}s</td>
                              <td>
                                <span className={`wear-badge ${t.is_service_run ? "warn" : "info"}`} style={{ fontSize: "9px", padding: "2px 6px" }}>
                                  {t.is_service_run ? "FREIGHT" : t.traffic_profile}
                                </span>
                              </td>
                            </tr>
                          ))
                        )}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>

            {/* Expandable Raw Text Summary */}
            <div style={{ marginTop: "12px" }}>
              <button
                className="button secondary"
                style={{ padding: "5px 10px", fontSize: "11px" }}
                onClick={() => setShowBenchSummaryText((prev) => !prev)}
              >
                {showBenchSummaryText ? "Hide Raw Summary Log" : "Show Raw Summary Log"}
              </button>
              {showBenchSummaryText && (
                <pre className="event-row" style={{ marginTop: "8px", fontSize: "11px", maxHeight: "200px", overflowY: "auto" }}>
                  {benchResult.summary_text}
                </pre>
              )}
            </div>
          </div>
        )}
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
        {feedback && <p className={isWarningOrError(feedback) ? "authorization-feedback danger-text" : "authorization-feedback"} aria-live="polite">{feedback}</p>}
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
  const refresh = useCallback(async (clearError = true) => {
    if (!token) { setIncidents([]); setAlerts([]); return; }
    try {
      const [incResult, alertResult] = await Promise.all([
        authorizedApi<{ items: Incident[] }>("/api/v1/operational/incidents", token),
        authorizedApi<{ items: SecurityAlert[] }>("/api/v1/acs/logs/alerts", token).catch(() => ({ items: [] as SecurityAlert[] })),
      ]);
      setIncidents(incResult.items);
      setAlerts(alertResult.items ?? []);
      if (clearError) setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Failed to load incidents"); }
  }, [token]);
  useEffect(() => {
    void refresh(true);
    if (!token) return;
    let alive = true;
    let inFlight = false;
    const timer = window.setInterval(() => {
      if (!alive || inFlight) return;
      inFlight = true;
      void refresh(false).finally(() => { inFlight = false; });
    }, 500);
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
  return <><PageHeader eyebrow="Mode 3 · software test stub" title="Incidents and emergency response" detail="Polls incidents and ACS security alerts every 500 ms (PERF-3)." action={<button className="button secondary" disabled={!token} onClick={() => { void refresh(); }}>Refresh incidents</button>} />
    {!token ? <EmptyState title="Dashboard sign-in required" detail="Present an Admin or Technician credential in the header to inspect and resolve simulated incidents." /> : <>
      {error && <p className="error-banner" role="alert">{error}</p>}
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
          <article className="incident-card" key={incident.incident_id}><div><strong>{incident.kind}</strong> · {incident.status} · {incident.severity} · {incident.notification_status ?? "NOTIFIED"}</div><p>{incident.details}</p><small>{incident.scope ?? "CAR_SPECIFIC"} · {incident.elevator_id ?? "Building"} · {floorLabel(incident.floor)}{incident.direction ? ` · ${incident.direction}` : ""} · {incident.trigger_source ?? "SYSTEM"} · {formatLocalTime(incident.created_at)} · {incident.incident_id}</small><small>History: {(incident.history ?? []).join(" → ") || incident.status}{(incident.related_requests?.length ?? 0) > 0 ? ` · Related requests (${incident.related_requests?.length}): ${incident.related_requests?.join(", ")}` : ""}</small><div className="incident-actions">
            {incident.status === "OPEN" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/action", { incident_id: incident.incident_id, action: "ACKNOWLEDGE" }); }}>Acknowledge</button>}
            {(incident.status === "OPEN" || incident.status === "ACKNOWLEDGED") && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/action", { incident_id: incident.incident_id, action: "RESOLVE", recovery_confirmed: true }); }}>Resolve (after checks)</button>}
            {incident.status === "RESOLVED" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/action", { incident_id: incident.incident_id, action: "CLOSE" }); }}>Close</button>}
            {role === "ADMIN" && incident.kind === "HELP" && incident.status !== "RESOLVED" && incident.status !== "CLOSED" && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/confirm-stuck", { incident_id: incident.incident_id }); }}>Confirm stuck</button>}
            {role === "ADMIN" && ["MEDICAL", "STUCK_CAR"].includes(incident.kind) && !incident.rescue_car_id && !["RESOLVED", "CLOSED", "FALSE_ALARM"].includes(incident.status) && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/rescue", { incident_id: incident.incident_id, confirmed_empty: true, wait_seconds: 120 }); }}>Send empty rescue car (confirm)</button>}
            {role === "ADMIN" && incident.kind === "MEDICAL" && incident.rescue_car_id && !["RESOLVED", "CLOSED", "FALSE_ALARM"].includes(incident.status) && !(incident.history ?? []).includes("RESCUE_BOARDED") && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/confirm-boarding", { incident_id: incident.incident_id }); }}>Confirm rescue boarding</button>}
            {role === "ADMIN" && incident.rescue_car_id && !["RESOLVED", "CLOSED", "FALSE_ALARM"].includes(incident.status) && !(incident.history ?? []).includes("RESCUE_BOARDED") && <button className="button secondary" disabled={busy} onClick={() => { void act("/api/v1/operational/incidents/rescue/cancel", { incident_id: incident.incident_id }); }}>Cancel rescue car</button>}
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

type WearTearConfigState = {
  enabled: boolean;
  strategy: string;
  monthly_target_pct: number;
  monthly_ceiling_pct: number;
  six_month_target_pct: number;
  six_month_ceiling_pct: number;
  safety_margin_pct: number;
  off_peak_start_hour: number;
  off_peak_end_hour: number;
};

type WearTearCarStat = {
  elevator_id: string;
  total_floors: number;
  total_trips: number;
  floors_monthly: number;
  trips_monthly: number;
  floors_six_months: number;
  trips_six_months: number;
};

type WearTearMetricsData = {
  min_distance: number;
  max_distance: number;
  mean_distance: number;
  delta_floors: number;
  spread_pct: number;
  monthly_target_met?: boolean;
  six_month_target_met?: boolean;
  outlier_car: string | null;
  outlier_type: string | null;
};

type WearTearStatusResponse = {
  config: WearTearConfigState;
  car_stats: Record<string, WearTearCarStat>;
  monthly: WearTearMetricsData;
  six_months: WearTearMetricsData;
};

type BenchmarkDailyPoint = {
  day: number;
  date_str: string;
  E1_floors: number;
  E2_floors: number;
  E3_floors: number;
  E4_floors: number;
  min_floors: number;
  max_floors: number;
  delta_floors: number;
  spread_pct: number;
  monthly_pass: boolean;
  six_month_pass: boolean;
};

type BenchmarkResponse = {
  strategy: string;
  strategy_description: string;
  enabled: boolean;
  seed: number;
  simulated_months: number;
  simulated_days: number;
  total_calls_generated: number;
  total_floors_traversed: number;
  car_floors: Record<string, number>;
  car_trips: Record<string, number>;
  monthly_delta_floors: number;
  monthly_spread_pct: number;
  monthly_target_met: boolean;
  monthly_requirement_met: boolean;
  six_month_delta_floors: number;
  six_month_spread_pct: number;
  six_month_target_met: boolean;
  six_month_requirement_met: boolean;
  time_series: BenchmarkDailyPoint[];
  baseline_comparison?: {
    unmitigated_car_floors: Record<string, number>;
    unmitigated_monthly_spread_pct: number;
    unmitigated_six_month_spread_pct: number;
    improvement_pct_points: number;
  };
};

function WearTearChart({ points }: { points: BenchmarkDailyPoint[] }) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  if (!points || points.length === 0) return null;

  const width = 840;
  const height = 260;
  const padLeft = 60;
  const padRight = 30;
  const padTop = 25;
  const padBottom = 35;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;

  const maxFloor = Math.max(
    ...points.map((p) => Math.max(p.E1_floors, p.E2_floors, p.E3_floors, p.E4_floors)),
    100
  );
  const yMax = Math.ceil(maxFloor * 1.05);

  const getX = (idx: number) => padLeft + (idx / (points.length - 1 || 1)) * plotW;
  const getY = (val: number) => padTop + plotH - (val / yMax) * plotH;

  const makePath = (key: "E1_floors" | "E2_floors" | "E3_floors" | "E4_floors") => {
    return points
      .map((p, idx) => `${idx === 0 ? "M" : "L"} ${getX(idx).toFixed(1)} ${getY(p[key]).toFixed(1)}`)
      .join(" ");
  };

  const activePoint = hoverIndex !== null && hoverIndex >= 0 && hoverIndex < points.length ? points[hoverIndex] : points[points.length - 1];

  const monthMarkers = [30, 60, 90, 120, 150, 180].filter((d) => d <= points.length);

  return (
    <div className="wear-chart-box">
      <div className="wear-chart-header">
        <div>
          <strong style={{ fontSize: "14px" }}>Accumulated Floors by Elevator Unit Over Time</strong>
          <p style={{ margin: "4px 0 0", color: "#8ec8f4", fontSize: "12px" }}>
            {activePoint ? `Day ${activePoint.day} (${activePoint.date_str}) · Delta: ${activePoint.delta_floors} fl · Spread: ${activePoint.spread_pct.toFixed(2)}% · ${activePoint.six_month_pass ? "TARGET MET (<= 4.0%)" : "EXCEEDED"}` : "180-Day Progression"}
          </p>
        </div>
        <div className="wear-legend">
          <div className="wear-legend-item"><span className="wear-dot" style={{ background: "#38bdf8" }} /> E1: {activePoint?.E1_floors?.toLocaleString() ?? 0} fl</div>
          <div className="wear-legend-item"><span className="wear-dot" style={{ background: "#34d399" }} /> E2: {activePoint?.E2_floors?.toLocaleString() ?? 0} fl</div>
          <div className="wear-legend-item"><span className="wear-dot" style={{ background: "#a78bfa" }} /> E3: {activePoint?.E3_floors?.toLocaleString() ?? 0} fl</div>
          <div className="wear-legend-item"><span className="wear-dot" style={{ background: "#fbbf24" }} /> E4: {activePoint?.E4_floors?.toLocaleString() ?? 0} fl</div>
        </div>
      </div>

      <svg
        className="wear-svg"
        viewBox={`0 0 ${width} ${height}`}
        onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const svgX = ((e.clientX - rect.left) / rect.width) * width;
          const ratio = Math.max(0, Math.min(1, (svgX - padLeft) / plotW));
          const idx = Math.round(ratio * (points.length - 1));
          setHoverIndex(idx);
        }}
        onMouseLeave={() => setHoverIndex(null)}
      >
        {/* Y Axis Grid Lines */}
        {[0, 0.25, 0.5, 0.75, 1.0].map((frac, i) => {
          const y = padTop + plotH * (1 - frac);
          const val = Math.round(yMax * frac);
          return (
            <g key={i}>
              <line x1={padLeft} y1={y} x2={width - padRight} y2={y} stroke="#1e324d" strokeDasharray="3 3" />
              <text x={padLeft - 10} y={y + 4} fill="#8ec8f4" fontSize="10" textAnchor="end">{val.toLocaleString()}</text>
            </g>
          );
        })}

        {/* Month Vertical Dividers */}
        {monthMarkers.map((day) => {
          const x = getX(day - 1);
          const monthNum = Math.round(day / 30);
          return (
            <g key={day}>
              <line x1={x} y1={padTop} x2={x} y2={height - padBottom} stroke="#2c4c70" strokeDasharray="4 4" />
              <text x={x} y={height - padBottom + 16} fill="#8ec8f4" fontSize="10" textAnchor="middle">M{monthNum} (D{day})</text>
            </g>
          );
        })}

        {/* Elevator distance curves */}
        <path d={makePath("E1_floors")} fill="none" stroke="#38bdf8" strokeWidth="2.5" />
        <path d={makePath("E2_floors")} fill="none" stroke="#34d399" strokeWidth="2.5" />
        <path d={makePath("E3_floors")} fill="none" stroke="#a78bfa" strokeWidth="2.5" />
        <path d={makePath("E4_floors")} fill="none" stroke="#fbbf24" strokeWidth="2.5" />

        {/* Hover vertical line */}
        {hoverIndex !== null && hoverIndex >= 0 && hoverIndex < points.length && (
          <line
            x1={getX(hoverIndex)}
            y1={padTop}
            x2={getX(hoverIndex)}
            y2={height - padBottom}
            stroke="#ffffff"
            strokeWidth="1.5"
            strokeDasharray="2 2"
          />
        )}
      </svg>
    </div>
  );
}

function WearTearView({ token: _token, role: _role, onFleetRefresh }: { token?: string; role?: string; onFleetRefresh?: () => Promise<void> }) {
  const [status, setStatus] = useState<WearTearStatusResponse | null>(null);
  const [configForm, setConfigForm] = useState<WearTearConfigState>({
    enabled: true,
    strategy: "BOTTOM_PRIORITY",
    monthly_target_pct: 1.8,
    monthly_ceiling_pct: 2.0,
    six_month_target_pct: 4.0,
    six_month_ceiling_pct: 5.0,
    safety_margin_pct: 0.2,
    off_peak_start_hour: 22,
    off_peak_end_hour: 5,
  });
  const [benchmarkResult, setBenchmarkResult] = useState<BenchmarkResponse | null>(null);
  const [benchDuration, setBenchDuration] = useState<number>(6);
  const [benchSeed, setBenchSeed] = useState<number>(42);
  const [benchCallsPerDay, setBenchCallsPerDay] = useState<number>(240);
  const [compareBaseline, setCompareBaseline] = useState<boolean>(true);
  const [loadingBench, setLoadingBench] = useState<boolean>(false);
  const [savingConfig, setSavingConfig] = useState<boolean>(false);
  const [feedback, setFeedback] = useState<string>("");
  const [error, setError] = useState<string>("");

  const loadStatus = useCallback(async () => {
    try {
      const res = await fetch("/api/v1/operational/wear-tear/status");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as WearTearStatusResponse;
      setStatus(data);
      if (data.config) {
        setConfigForm(data.config);
      }
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load wear & tear status");
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    const timer = setInterval(() => { void loadStatus(); }, 1500);
    return () => clearInterval(timer);
  }, [loadStatus]);

  const saveConfig = async () => {
    setSavingConfig(true);
    setError("");
    try {
      const res = await fetch("/api/v1/operational/wear-tear/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(configForm),
      });
      if (!res.ok) throw new Error(`Failed to save config: HTTP ${res.status}`);
      setFeedback("Configuration updated successfully!");
      setTimeout(() => setFeedback(""), 3500);
      await loadStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error saving config");
    } finally {
      setSavingConfig(false);
    }
  };

  const resetCounters = async (scope: string) => {
    setError("");
    try {
      const res = await fetch("/api/v1/operational/wear-tear/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope }),
      });
      if (!res.ok) throw new Error(`Reset failed: HTTP ${res.status}`);
      setFeedback(`Trip & floor counters reset (${scope})`);
      setTimeout(() => setFeedback(""), 3500);
      await loadStatus();
      if (onFleetRefresh) await onFleetRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error resetting counters");
    }
  };

  const runBenchmark = async () => {
    setLoadingBench(true);
    setError("");
    try {
      const res = await fetch("/api/v1/operational/wear-tear/benchmark", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          seed: benchSeed,
          months: benchDuration,
          days_per_month: 30,
          calls_per_day: benchCallsPerDay,
          strategy: configForm.strategy,
          enabled: configForm.enabled,
          compare_baseline: compareBaseline,
        }),
      });
      if (!res.ok) throw new Error(`Benchmark failed: HTTP ${res.status}`);
      const data = (await res.json()) as BenchmarkResponse;
      setBenchmarkResult(data);
      setFeedback(`Simulated ${data.simulated_days} days (${data.simulated_months} months) successfully!`);
      setTimeout(() => setFeedback(""), 3500);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Benchmark execution error");
    } finally {
      setLoadingBench(false);
    }
  };

  useEffect(() => {
    void runBenchmark();
  }, []);

  const monthlyMet = status?.monthly.monthly_target_met ?? true;
  const sixMonthMet = status?.six_months.six_month_target_met ?? true;

  return (
    <>
      <PageHeader
        eyebrow="Non-Functional Optimization · Fleet Wear &amp; Tear Balancing"
        title="Elevator Wear &amp; Tear Balancer"
        detail="Balances accumulated travel distance (floors traversed) across residential units E1–E4. Enforces strict safety targets of < 1.8% deviation per month and < 4.0% in 6 months with dual monthly/6-month trip counters."
        action={
          <div style={{ display: "flex", gap: "8px" }}>
            <button className="button secondary" onClick={() => { void resetCounters("MONTHLY"); }}>Reset Monthly Counter</button>
            <button className="button secondary" onClick={() => { void resetCounters("ALL"); }}>Reset All Counters</button>
          </div>
        }
      />

      {feedback && <p className="authorization-feedback" role="status">{feedback}</p>}
      {error && <p className="error-banner" role="alert">{error}</p>}

      <section className="panel section" style={{ marginTop: "18px" }}>
        <div className="table-heading">
          <strong>Live Fleet Distance Spread &amp; Compliance (E1–E4)</strong>
          <span>Dual Trip Counters: 30-Day Monthly &amp; 180-Day 6-Month Reset</span>
        </div>
        <div className="kpi-grid" style={{ marginTop: "14px" }}>
          <article className="kpi-card">
            <span>Monthly Spread (Target &lt; 1.8%)</span>
            <strong>{status?.monthly.spread_pct.toFixed(2) ?? "0.00"}%</strong>
            <small>
              Delta: {status?.monthly.delta_floors ?? 0} floors (Min {status?.monthly.min_distance ?? 0} / Max {status?.monthly.max_distance ?? 0}) ·{" "}
              <span className={`wear-badge ${monthlyMet ? "pass" : "fail"}`}>{monthlyMet ? "PASS (<= 1.8%)" : "EXCEEDED"}</span>
            </small>
          </article>
          <article className="kpi-card">
            <span>6-Month Spread (Target &lt; 4.0%)</span>
            <strong>{status?.six_months.spread_pct.toFixed(2) ?? "0.00"}%</strong>
            <small>
              Delta: {status?.six_months.delta_floors ?? 0} floors (Min {status?.six_months.min_distance ?? 0} / Max {status?.six_months.max_distance ?? 0}) ·{" "}
              <span className={`wear-badge ${sixMonthMet ? "pass" : "fail"}`}>{sixMonthMet ? "PASS (<= 4.0%)" : "EXCEEDED"}</span>
            </small>
          </article>
          <article className="kpi-card">
            <span>Statistical Outlier Detection</span>
            <strong style={{ fontSize: "18px", marginTop: "12px" }}>
              {status?.monthly.outlier_car ? `Car ${status.monthly.outlier_car} (${status.monthly.outlier_type})` : "Balanced Fleet"}
            </strong>
            <small>
              {status?.monthly.outlier_car
                ? status.monthly.outlier_type === "LOW"
                  ? "Car is lagging behind peer cluster; prioritized for assignments."
                  : "Car is leading peer cluster; paused for preventive rest."
                : "All units within ±1.5% cluster median deviation."}
            </small>
          </article>
        </div>

        <div className="table-wrap" style={{ marginTop: "18px" }}>
          <table className="operations-table">
            <thead>
              <tr>
                <th>Unit</th>
                <th>Role</th>
                <th>Total Traversed</th>
                <th>Total Trips</th>
                <th>Monthly Floors (30d)</th>
                <th>Monthly Trips</th>
                <th>6-Month Floors (180d)</th>
                <th>6-Month Trips</th>
              </tr>
            </thead>
            <tbody>
              {status && Object.values(status.car_stats).map((c) => (
                <tr key={c.elevator_id}>
                  <td><strong>{c.elevator_id}</strong></td>
                  <td>{c.elevator_id === "E5" ? "Freight / Service" : "Residential Passenger"}</td>
                  <td>{c.total_floors.toLocaleString()} fl</td>
                  <td>{c.total_trips.toLocaleString()}</td>
                  <td><strong>{c.floors_monthly.toLocaleString()} fl</strong></td>
                  <td>{c.trips_monthly.toLocaleString()}</td>
                  <td><strong>{c.floors_six_months.toLocaleString()} fl</strong></td>
                  <td>{c.trips_six_months.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel section">
        <div className="table-heading">
          <strong>Wear &amp; Tear Controller Configurator</strong>
          <span>Configurable Balancing Implementations (Strategies 1–5)</span>
        </div>
        <div className="wear-controls-grid">
          <div className="wear-card">
            <h3>Feature Toggle</h3>
            <div className="wear-field-row">
              <label>Wear Balancing Active</label>
              <select
                value={configForm.enabled ? "true" : "false"}
                onChange={(e) => setConfigForm({ ...configForm, enabled: e.target.value === "true" })}
              >
                <option value="true">ENABLED (Active)</option>
                <option value="false">DISABLED (Standard ETA)</option>
              </select>
            </div>
            <p style={{ margin: "6px 0 0", fontSize: "11px", color: "#7d899d" }}>
              When enabled, incoming hall calls are dispatched to balance mileage across E1–E4.
            </p>
          </div>

          <div className="wear-card" style={{ gridColumn: "span 2" }}>
            <h3>Balancing Strategy (Dropdown Menu)</h3>
            <div className="wear-field-row">
              <label>Active Implementation</label>
              <select
                style={{ width: "100%", maxWidth: "480px" }}
                value={configForm.strategy}
                onChange={(e) => setConfigForm({ ...configForm, strategy: e.target.value })}
              >
                <option value="BOTTOM_PRIORITY">1. Bottom Threshold Priority (Prioritize until 0.2% safety margin)</option>
                <option value="TOP_HALT">2. Top Threshold Halt (Pause leading car until 0.2% safety margin)</option>
                <option value="LEAST_RUN_FIRST">3. Least-Run Elevator Priority (Most worked unit rests naturally)</option>
                <option value="OFF_PEAK_REBALANCE">4. Off-Peak Rebalancing (Aggressive rebalancing during night window)</option>
                <option value="OUTLIER_CORRECTION">5. Statistical Outlier Correction (Prioritize low / Halt high outlier)</option>
              </select>
            </div>
            <p style={{ margin: "6px 0 0", fontSize: "11px", color: "#7d899d" }}>
              Choose among the 5 distinct wear mitigation approaches to keep mileage delta under 1.8% monthly and 4.0% over 6 months.
            </p>
          </div>

          <div className="wear-card">
            <h3>Threshold Safety Margins</h3>
            <div className="wear-field-row">
              <label>Monthly Target %</label>
              <input
                type="number"
                step="0.1"
                min="0.5"
                max="5.0"
                value={configForm.monthly_target_pct}
                onChange={(e) => setConfigForm({ ...configForm, monthly_target_pct: parseFloat(e.target.value) || 1.8 })}
              />
            </div>
            <div className="wear-field-row">
              <label>6-Month Target %</label>
              <input
                type="number"
                step="0.1"
                min="1.0"
                max="10.0"
                value={configForm.six_month_target_pct}
                onChange={(e) => setConfigForm({ ...configForm, six_month_target_pct: parseFloat(e.target.value) || 4.0 })}
              />
            </div>
            <div className="wear-field-row">
              <label>Safety Margin Buffer %</label>
              <input
                type="number"
                step="0.05"
                min="0.05"
                max="1.0"
                value={configForm.safety_margin_pct}
                onChange={(e) => setConfigForm({ ...configForm, safety_margin_pct: parseFloat(e.target.value) || 0.2 })}
              />
            </div>
          </div>

          <div className="wear-card">
            <h3>Off-Peak Schedule (Strategy 4)</h3>
            <div className="wear-field-row">
              <label>Off-Peak Start Hour (24h)</label>
              <input
                type="number"
                min="0"
                max="23"
                value={configForm.off_peak_start_hour}
                onChange={(e) => setConfigForm({ ...configForm, off_peak_start_hour: parseInt(e.target.value, 10) || 22 })}
              />
            </div>
            <div className="wear-field-row">
              <label>Off-Peak End Hour (24h)</label>
              <input
                type="number"
                min="0"
                max="23"
                value={configForm.off_peak_end_hour}
                onChange={(e) => setConfigForm({ ...configForm, off_peak_end_hour: parseInt(e.target.value, 10) || 5 })}
              />
            </div>
            <div style={{ marginTop: "14px" }}>
              <button className="button primary" disabled={savingConfig} onClick={() => { void saveConfig(); }}>
                {savingConfig ? "Saving..." : "Save Configuration"}
              </button>
            </div>
          </div>
        </div>
      </section>

      <section className="panel section">
        <div className="table-heading">
          <strong>Reproducible 6-Month Benchmark Simulator (Random Synthetic Load)</strong>
          <span>Deterministic Seeded Simulation with Multi-Car Time-Series Analytics</span>
        </div>
        <div className="hall-controls" style={{ marginTop: "14px" }}>
          <label>
            Simulation Duration
            <select value={benchDuration} onChange={(e) => setBenchDuration(parseInt(e.target.value, 10))}>
              <option value="6">6 Months (180 Days) · SRS Target &lt;= 4.0%</option>
              <option value="1">1 Month (30 Days) · SRS Target &lt;= 1.8%</option>
              <option value="3">3 Months (90 Days)</option>
            </select>
          </label>

          <label>
            Random Seed (Reproducible)
            <div style={{ display: "flex", gap: "6px" }}>
              <input
                type="number"
                value={benchSeed}
                onChange={(e) => setBenchSeed(parseInt(e.target.value, 10) || 1)}
                style={{ width: "90px" }}
              />
              <button
                className="button secondary"
                onClick={() => setBenchSeed(Math.floor(Math.random() * 90000) + 1000)}
                title="Generate new random seed"
              >
                🎲 Random
              </button>
            </div>
          </label>

          <label>
            Daily Intensity
            <select value={benchCallsPerDay} onChange={(e) => setBenchCallsPerDay(parseInt(e.target.value, 10))}>
              <option value="150">Normal Demand (~150 calls/day)</option>
              <option value="240">Standard CIBSE Peak (~240 calls/day)</option>
              <option value="360">High Traffic Burst (~360 calls/day)</option>
            </select>
          </label>

          <label style={{ display: "flex", alignItems: "center", gap: "8px", cursor: "pointer", textTransform: "none", fontSize: "12px", color: "#354156" }}>
            <input
              type="checkbox"
              checked={compareBaseline}
              onChange={(e) => setCompareBaseline(e.target.checked)}
              style={{ width: "auto" }}
            />
            Compare with Unmitigated Baseline
          </label>

          <button className="button primary" disabled={loadingBench} onClick={() => { void runBenchmark(); }}>
            {loadingBench ? "Simulating..." : "▶ Run Benchmark Simulation"}
          </button>
        </div>

        {benchmarkResult && (
          <div style={{ marginTop: "20px" }}>
            <div className="kpi-grid">
              <article className="kpi-card">
                <span>Simulation Scope</span>
                <strong>{benchmarkResult.simulated_days} Days</strong>
                <small>{benchmarkResult.total_calls_generated.toLocaleString()} calls · {benchmarkResult.total_floors_traversed.toLocaleString()} floors · Seed {benchmarkResult.seed}</small>
              </article>

              <article className="kpi-card">
                <span>Monthly Spread (Last 30 Days)</span>
                <strong>{benchmarkResult.monthly_spread_pct.toFixed(2)}%</strong>
                <small>
                  Delta: {benchmarkResult.monthly_delta_floors} fl ·{" "}
                  <span className={`wear-badge ${benchmarkResult.monthly_target_met ? "pass" : "fail"}`}>
                    {benchmarkResult.monthly_target_met ? "PASSED [<= 1.8%]" : "FAILED [> 1.8%]"}
                  </span>
                </small>
              </article>

              <article className="kpi-card">
                <span>6-Month Spread (Full Period)</span>
                <strong>{benchmarkResult.six_month_spread_pct.toFixed(2)}%</strong>
                <small>
                  Delta: {benchmarkResult.six_month_delta_floors} fl ·{" "}
                  <span className={`wear-badge ${benchmarkResult.six_month_target_met ? "pass" : "fail"}`}>
                    {benchmarkResult.six_month_target_met ? "PASSED [<= 4.0%]" : "FAILED [> 4.0%]"}
                  </span>
                </small>
              </article>

              {benchmarkResult.baseline_comparison && (
                <article className="kpi-card" style={{ gridColumn: "span 3", background: "#f8fafd", border: "1px solid #d3e4f7" }}>
                  <span>Unmitigated Baseline vs. Wear-Balanced Comparison</span>
                  <div style={{ display: "flex", gap: "24px", marginTop: "8px", alignItems: "center" }}>
                    <div>
                      <small style={{ color: "#7d899d" }}>WITHOUT BALANCING (UNMITIGATED):</small>
                      <div style={{ fontSize: "18px", fontWeight: 700, color: "#c83832" }}>
                        {benchmarkResult.baseline_comparison.unmitigated_six_month_spread_pct.toFixed(2)}% spread (FAILED)
                      </div>
                    </div>
                    <div style={{ fontSize: "24px", color: "#147aca" }}>➔</div>
                    <div>
                      <small style={{ color: "#7d899d" }}>WITH {benchmarkResult.strategy} BALANCING:</small>
                      <div style={{ fontSize: "18px", fontWeight: 700, color: "#1e8751" }}>
                        {benchmarkResult.six_month_spread_pct.toFixed(2)}% spread (PASSED)
                      </div>
                    </div>
                    <div style={{ marginLeft: "auto", textAlign: "right" }}>
                      <span className="wear-badge pass" style={{ fontSize: "12px", padding: "6px 12px" }}>
                        +{benchmarkResult.baseline_comparison.improvement_pct_points.toFixed(2)}% percentage points more even wear!
                      </span>
                    </div>
                  </div>
                </article>
              )}
            </div>

            <div className="table-wrap" style={{ marginTop: "16px" }}>
              <table className="operations-table">
                <thead>
                  <tr>
                    <th>Elevator</th>
                    <th>6-Month Distance</th>
                    <th>Completed Trips</th>
                    <th>Share of Total Fleet Mileage</th>
                    <th>Deviation from Mean</th>
                  </tr>
                </thead>
                <tbody>
                  {["E1", "E2", "E3", "E4"].map((cid) => {
                    const floors = benchmarkResult.car_floors[cid] ?? 0;
                    const trips = benchmarkResult.car_trips[cid] ?? 0;
                    const totalFloors = Object.values(benchmarkResult.car_floors).reduce((a, b) => a + b, 0);
                    const meanFloors = totalFloors / 4.0;
                    const sharePct = totalFloors > 0 ? (floors / totalFloors) * 100 : 25;
                    const devPct = meanFloors > 0 ? ((floors - meanFloors) / meanFloors) * 100 : 0;
                    return (
                      <tr key={cid}>
                        <td><strong>{cid}</strong></td>
                        <td><strong>{floors.toLocaleString()} floors</strong></td>
                        <td>{trips.toLocaleString()} trips</td>
                        <td>{sharePct.toFixed(2)}% (Target: ~25.0%)</td>
                        <td style={{ color: Math.abs(devPct) <= 1.0 ? "#1e8751" : "#d97706" }}>
                          {devPct >= 0 ? `+${devPct.toFixed(2)}%` : `${devPct.toFixed(2)}%`}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <WearTearChart points={benchmarkResult.time_series} />
          </div>
        )}
      </section>
    </>
  );
}


type SelectGroup = { group: string; options: string[] };

function SearchSelect({
  label,
  value,
  onChange,
  groups,
  placeholder,
  disabled = false,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  groups: SelectGroup[];
  placeholder?: string;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onDocClick = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  const query = value.trim().toLowerCase();
  const filteredGroups = useMemo(
    () =>
      groups
        .map((g) => ({
          group: g.group,
          options: g.options.filter((opt) => !query || opt.toLowerCase().includes(query)),
        }))
        .filter((g) => g.options.length > 0),
    [groups, query],
  );
  const flatOptions = useMemo(() => filteredGroups.flatMap((g) => g.options), [filteredGroups]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (disabled) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setActiveIdx((idx) => (flatOptions.length ? (idx + 1) % flatOptions.length : 0));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      setActiveIdx((idx) => (flatOptions.length ? (idx - 1 + flatOptions.length) % flatOptions.length : 0));
    } else if (event.key === "Enter" && open && flatOptions[activeIdx]) {
      event.preventDefault();
      onChange(flatOptions[activeIdx]);
      setOpen(false);
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  };

  let optionCursor = -1;
  return (
    <div className="search-select" ref={containerRef}>
      <span>{label}</span>
      <div className="search-select-box">
        <input
          role="combobox"
          aria-expanded={open}
          aria-autocomplete="list"
          disabled={disabled}
          value={value}
          placeholder={placeholder}
          onFocus={() => { if (!disabled) { setOpen(true); setActiveIdx(0); } }}
          onChange={(e) => { onChange(e.target.value); setOpen(true); setActiveIdx(0); }}
          onKeyDown={handleKeyDown}
        />
        {value && !disabled && (
          <button type="button" className="search-select-clear" aria-label={`Clear ${label}`} onClick={() => { onChange(""); setOpen(true); }}>×</button>
        )}
        <button type="button" className="search-select-toggle" disabled={disabled} aria-label={`Toggle ${label} options`} onClick={() => setOpen((prev) => !prev)}>▾</button>
      </div>
      {open && !disabled && (
        <div className="search-select-menu" role="listbox">
          {filteredGroups.length === 0 ? (
            <div className="search-select-empty">No preset match — custom value will be used</div>
          ) : (
            filteredGroups.map((group) => (
              <div key={group.group}>
                <div className="search-select-group">{group.group}</div>
                {group.options.map((opt) => {
                  optionCursor += 1;
                  const idx = optionCursor;
                  return (
                    <button
                      type="button"
                      role="option"
                      aria-selected={value === opt}
                      key={`${group.group}-${opt}`}
                      className={`search-select-option ${idx === activeIdx ? "active" : ""}`}
                      onMouseDown={(e) => { e.preventDefault(); onChange(opt); setOpen(false); }}
                    >
                      {opt}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

type AuditTab = "audit" | "auth" | "incidents" | "alerts" | "commands";

function Audit({ token, role }: { token: string; role: string }) {
  const [tab, setTab] = useState<AuditTab>("audit");
  const [logs, setLogs] = useState<Record<string, unknown>[]>([]);
  const [stubEvents, setStubEvents] = useState<StubEvent[]>([]);
  const [knownCards, setKnownCards] = useState<string[]>(["ADMIN-CARD-0001"]);
  const [integrity, setIntegrity] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [actorFilter, setActorFilter] = useState("");
  const [carFilter, setCarFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [startFilter, setStartFilter] = useState("");
  const [endFilter, setEndFilter] = useState("");

  useEffect(() => {
    if (!token || role !== "ADMIN") return;
    let alive = true;
    void authorizedApi<{ items: { credential_id: string }[] }>("/api/v1/acs/credentials?limit=200", token)
      .then((res) => {
        if (alive && Array.isArray(res.items)) {
          const ids = Array.from(new Set(res.items.map((item) => item.credential_id).filter(Boolean)));
          if (ids.length > 0) setKnownCards(ids);
        }
      })
      .catch(() => { /* keep default card suggestions */ });
    return () => { alive = false; };
  }, [token, role]);

  const actorGroups: SelectGroup[] = useMemo(() => [
    ...(tab === "audit" ? [{ group: "System & Roles", options: ["SYSTEM", "ADMIN", "TECHNICIAN"] }] : []),
    { group: "Registered Credentials", options: knownCards },
  ], [tab, knownCards]);

  const targetGroups: SelectGroup[] = useMemo(() => [
    { group: "Elevators", options: ["E1", "E2", "E3", "E4", "E5"] },
    ...(tab === "audit" ? [{ group: "System & Scopes", options: ["SREMS", "HALL_PANEL", "BUILDING"] }] : []),
  ], [tab]);

  const typeGroups: SelectGroup[] = useMemo(() => {
    if (tab === "audit") {
      return [
        { group: "Access & Auth", options: ["ADMIN_LOGIN", "ADMIN_LOGOUT", "ACCESS_DENIAL", "ONE_TAP_VIOLATION"] },
        { group: "Credentials", options: ["CREDENTIAL_ISSUED", "CREDENTIAL_SUSPENDED", "CREDENTIAL_REINSTATED", "CREDENTIAL_REVOKED", "CREDENTIAL_REPLACED", "VISITOR_PASS_ISSUED", "VISITOR_PASS_EXPIRED"] },
        { group: "Fleet & Maintenance", options: ["MAINTENANCE_ENTER", "DIAGNOSTIC_QUERY", "RETURN_TO_SERVICE", "CALL_CANCELLED_BY_ADMIN", "E5_SERVICE_RUN_START", "E5_SERVICE_RUN_END"] },
        { group: "Incidents & Rescue", options: ["EMERGENCY_OVERRIDE", "RESCUE_CAR_DISPATCHED", "RESCUE_CAR_BOARDED", "RESCUE_CAR_RELEASED", "INCIDENT_ACKNOWLEDGED", "INCIDENT_RESOLVED", "INCIDENT_CLOSED", "FALSE_ALARM_CLOSED"] },
        { group: "System & Governance", options: ["SYSTEM_STARTUP", "SYSTEM_SHUTDOWN", "INTEGRITY_CHECK_RUN", "REPORT_EXPORTED", "MALFORMED_MSG_REJECTED", "COMMAND_CONFLICT_REJECTED", "UNAUTHORIZED_CMD_REJECTED", "POLICY_CHANGED"] },
      ];
    }
    if (tab === "auth") {
      return [
        { group: "Card & Session", options: ["UNREGISTERED_CARD", "CARD_SUSPENDED", "CARD_REVOKED", "CARD_EXPIRED", "NO_ACTIVE_CREDENTIAL", "SESSION_INVALID", "SESSION_EXPIRED"] },
        { group: "Access & Rules", options: ["UNAUTHORIZED_FLOOR", "E5_B1_FORBIDDEN", "ONE_TAP_LIMIT_EXCEEDED", "TOKEN_WINDOW_EXPIRED", "UNAUTHORIZED_ROLE", "DASHBOARD_ACCESS_DENIED", "PEER_ADMIN_REQUIRED", "CONFIRMATION_REQUIRED"] },
        { group: "System & Commands", options: ["DB_FAIL_CLOSED", "MALFORMED_MSG_REJECTED", "UNAUTHORIZED_CMD_REJECTED", "COMMAND_CONFLICT_REJECTED", "INVALID_STATE_TRANSITION", "CREDENTIAL_ALREADY_EXISTS", "REPLACEMENT_ID_IN_USE"] },
      ];
    }
    if (tab === "incidents") {
      return [
        { group: "Emergency", options: ["FIRE", "MEDICAL_EMERGENCY", "POWER_FAILURE", "STUCK_CAR"] },
        { group: "Operational", options: ["HELP_REQUEST", "DOOR_OBSTRUCTION", "COMMUNICATION_FAILURE", "REPEATED_EMPTY_CALLS"] },
      ];
    }
    if (tab === "commands") {
      return [
        { group: "Passenger & Service", options: ["HALL_CALL", "HALL_CALL_CANCEL", "CAR_CALL", "E5_SERVICE_RUN", "DOOR_COMMAND"] },
        { group: "Safety & Fleet", options: ["RESCUE_DISPATCH", "RESCUE_CANCEL", "RESCUE_BOARDING", "MEDICAL_RUN", "ISOLATE", "RETURN_TO_SERVICE", "IN_FLIGHT_OPERATION"] },
      ];
    }
    return [
      { group: "Alert Types", options: ["REPEATED_FAILURES", "CLONED_CREDENTIAL", "CARD_SCANNING", "LOST_CARD_USAGE", "REVOKED_CARD_USAGE", "DOOR_OBSTRUCTION"] },
    ];
  }, [tab]);

  const statusGroups: SelectGroup[] = useMemo(() => {
    if (tab === "auth") return [{ group: "Outcome", options: ["ALLOWED", "DENIED"] }];
    if (tab === "incidents") return [{ group: "Incident Status", options: ["OPEN", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED", "CLOSED"] }];
    if (tab === "alerts") return [{ group: "Alert Status", options: ["OPEN", "ACKNOWLEDGED", "RESOLVED"] }];
    if (tab === "commands") return [{ group: "Command Status (COM-3)", options: ["ACKNOWLEDGED", "REJECTED", "TIMED_OUT", "UNKNOWN"] }];
    return [];
  }, [tab]);

  const switchTab = (nextTab: AuditTab) => {
    setTab(nextTab);
    setTypeFilter("");
    setStatusFilter("");
  };

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
      } else if (tab === "commands") {
        endpoint = "/api/v1/acs/logs/commands";
        if (statusFilter.trim()) params.set("status", statusFilter.trim());
        if (typeFilter.trim()) params.set("command_type", typeFilter.trim());
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
      {integrity && <p className={isWarningOrError(integrity) ? "error-banner" : "authorization-feedback"} role={isWarningOrError(integrity) ? "alert" : undefined}>{integrity}</p>}{error && <p className="error-banner" role="alert">{error}</p>}
      <section className="panel section">
        <div className="hall-controls">
          <button className={`button ${tab === "audit" ? "primary" : "secondary"}`} onClick={() => switchTab("audit")}>Audit Chain</button>
          <button className={`button ${tab === "auth" ? "primary" : "secondary"}`} onClick={() => switchTab("auth")}>Authorization Events</button>
          <button className={`button ${tab === "incidents" ? "primary" : "secondary"}`} onClick={() => switchTab("incidents")}>Incidents (DB)</button>
          <button className={`button ${tab === "alerts" ? "primary" : "secondary"}`} onClick={() => switchTab("alerts")}>Security Alerts (DB)</button>
          <button className={`button ${tab === "commands" ? "primary" : "secondary"}`} onClick={() => switchTab("commands")}>Command Status</button>
        </div>
        <div className="hall-controls" style={{ marginTop: "12px" }}>
          <SearchSelect label={tab === "audit" ? "Actor / Card" : "Credential"} value={actorFilter} onChange={setActorFilter} groups={actorGroups} placeholder="Search or type ID..." disabled={tab === "incidents" || tab === "commands"} />
          <SearchSelect label={tab === "audit" ? "Target / Car" : "Elevator"} value={carFilter} onChange={setCarFilter} groups={targetGroups} placeholder="E1..E5 or target..." disabled={tab === "alerts"} />
          <SearchSelect label={tab === "audit" ? "Action Type" : tab === "auth" ? "Denial Reason" : tab === "incidents" ? "Emergency Type" : tab === "commands" ? "Command Type" : "Alert Type"} value={typeFilter} onChange={setTypeFilter} groups={typeGroups} placeholder="Search type..." />
          <SearchSelect label={tab === "auth" ? "Outcome" : "Status"} value={statusFilter} onChange={setStatusFilter} groups={statusGroups} placeholder={tab === "audit" ? "N/A for Audit Chain" : "Select status..."} disabled={tab === "audit"} />
          <label>Start time (local)<input type="datetime-local" step={1} value={startFilter} onChange={(e) => setStartFilter(e.target.value)} /></label>
          <label>End time (local)<input type="datetime-local" step={1} value={endFilter} onChange={(e) => setEndFilter(e.target.value)} /></label>
          <button className="button primary" onClick={() => { void refresh(); }}>Search logs</button>
        </div>
      </section>
      <section className="panel section"><div className="table-heading"><strong>{tab === "audit" ? "ACS audit records" : tab === "auth" ? "Access authorization events" : tab === "incidents" ? "Persisted incidents" : tab === "commands" ? "Elevator command statuses (COM-3)" : "Security alerts"} · database</strong><span>{logs.length} shown</span></div>{logs.map((item, i) => <pre className="event-row" key={i}>{JSON.stringify({ ...item, ...(typeof item.timestamp === "string" ? { timestamp: formatLocalTime(item.timestamp) } : {}), ...(typeof item.created_at === "string" ? { created_at: formatLocalTime(item.created_at) } : {}) })}</pre>)}{!logs.length && <EmptyState title="No matching records loaded" detail="Adjust filters or verify PostgreSQL connectivity." />}</section>
      <section className="panel section"><div className="table-heading"><strong>Simulator event trace · not tamper-evident</strong><span>{stubEvents.length} shown</span></div>{stubEvents.map((event, i) => <p className="event-row" key={i}>{formatLocalTime(event.timestamp)} · {event.event_type} · {event.elevator_id ?? "Building"} · {event.detail}</p>)}</section>
    </>}
  </>;
}

function App() {
  const [view, setView] = useState<View>("overview");
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(true);
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
    }, 500);
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
  const page =
    view === "overview" ? <Overview fleet={fleet} summary={summary} token={token} /> :
    view === "simulation" ? <Simulation fleet={fleet} onFleetRefresh={onFleetRefresh} /> :
    view === "wear_tear" ? <WearTearView token={token} role={role} onFleetRefresh={onFleetRefresh} /> :
    view === "elevators" ? <Elevators fleet={fleet} refresh={onFleetRefresh} token={token} role={role} /> :
    view === "credentials" ? <Credentials token={token} role={role} /> :
    view === "security" ? <Security token={token} role={role} refreshFleet={onFleetRefresh} /> :
    view === "analytics" ? <Analytics summary={summary} token={token} role={role} /> :
    <Audit token={token} role={role} />;
  return <div className="app-shell">
    <aside className={`sidebar ${sidebarCollapsed ? "collapsed" : ""}`}>
      <div className="brand">
        <button
          className="brand-toggle"
          onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
          title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          <span className="brand-mark">S</span>
        </button>
        {!sidebarCollapsed && (
          <div className="brand-text">
            <strong>SREMS</strong>
            <span>Operations</span>
          </div>
        )}
        <button
          className="sidebar-toggle"
          onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
          title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {sidebarCollapsed ? "»" : "«"}
        </button>
      </div>
      <nav aria-label="Primary navigation">
        {navigation.map((item) => (
          <button
            className={`nav-item ${item.id === view ? "active" : ""}`}
            key={item.id}
            onClick={() => setView(item.id)}
            title={item.label}
          >
            {sidebarCollapsed ? (
              <span className="nav-icon">{item.icon}</span>
            ) : (
              <span className="nav-label">{item.label}</span>
            )}
          </button>
        ))}
      </nav>
      <div className="sidebar-footer">
        <span className="status-dot" />
        {!sidebarCollapsed && (
          <span className="sidebar-footer-text">
            {error ? "API unavailable" : fleet ? "Test stub connected" : "Connecting"}
          </span>
        )}
      </div>
    </aside>
    <main className="content"><header className="topbar"><div><p className="eyebrow">Building operations</p><div className="breadcrumb">SREMS / {active.label}</div></div><div className="operator">{token ? <><span className="avatar">{role.slice(0, 1)}</span><span><strong>{role}</strong><small>ACS session</small></span><button className="button secondary" onClick={() => { void logout(); }}>Log out</button></> : <><label className="login-field">Dashboard card<input aria-label="Dashboard card" value={credential} onChange={(event) => setCredential(event.target.value)} /></label><button className="button secondary" onClick={() => { void login(); }}>Sign in</button></>}</div></header>{summary?.backlog_exceeded && <div className="error-banner" role="alert">Request backlog exceeded: {summary.active_requests} active requests (limit {summary.backlog_limit ?? 100}, +{summary.backlog_count ?? 0} queued above limit).</div>}{(summary?.active_incidents ?? 0) > 0 && <div className="error-banner" role="alert">Critical alert: {summary?.active_incidents} active incident(s) require review.</div>}{loginError && <div className="error-banner" role="alert">{loginError}</div>}{error && <div className="error-banner" role="alert">{error}</div>}<div className="page-content">{page}</div></main>
  </div>;
}

export default App;
