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
type ApiResult = { allowed?: boolean; message?: string; detail?: string; request?: HallCall; merged?: boolean };

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
  { id: "security", label: "Security alerts", icon: "!" },
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
      <p className="card-note">{car.message}</p>
    </article>
  ))}</div>;
}

function Overview({ fleet }: { fleet: FleetResponse | null }) {
  return <>
    <section className="hero-card"><div><p className="eyebrow">Live operations</p><h2>Elevator fleet status</h2><p className="muted">Displaying a software-only test stub. This is not physical elevator telemetry.</p></div><div className="hero-metric"><strong>{fleet?.elevators.length ?? "—"}</strong><span>cars monitored</span></div></section>
    <section className="summary-grid">
      <article className="summary-card"><span className="summary-icon blue">~</span><div><span>Active calls</span><strong>—</strong><small>Awaiting telemetry</small></div></article>
      <article className="summary-card"><span className="summary-icon amber">!</span><div><span>Open alerts</span><strong>—</strong><small>Awaiting telemetry</small></div></article>
      <article className="summary-card"><span className="summary-icon green">+</span><div><span>Availability</span><strong>—</strong><small>Awaiting telemetry</small></div></article>
    </section>
    <section className="section"><div className="section-heading"><div><p className="eyebrow">Fleet</p><h2>Elevator snapshot</h2></div><span className="refresh-label">Software test-stub snapshot</span></div><FleetCards fleet={fleet} /></section>
    <section className="section panel"><div className="section-heading"><div><p className="eyebrow">Activity</p><h2>Recent operations</h2></div></div><EmptyState title="No operational events available" detail="Connect the dispatch and telemetry sources to see recent activity." /></section>
  </>;
}

function Elevators({ fleet }: { fleet: FleetResponse | null }) {
  return <><PageHeader eyebrow="Fleet management" title="Elevators" detail="Monitor current position, availability, and maintenance state." action={<button className="button secondary" disabled>Refresh telemetry</button>} /><FleetCards fleet={fleet} /><section className="section panel"><div className="section-heading"><div><p className="eyebrow">Coverage</p><h2>Service matrix</h2></div></div><div className="matrix">{["Car", "Residential", "Freight", "Maintenance"].map((heading) => <strong key={heading}>{heading}</strong>)}{["E1", "E2", "E3", "E4", "E5"].flatMap((id) => [<span key={`${id}-id`}>{id}</span>, <span className="matrix-value" key={`${id}-r`}>—</span>, <span className="matrix-value" key={`${id}-f`}>—</span>, <span className="matrix-value" key={`${id}-m`}>—</span>])}</div></section></>;
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
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-8 · SEC-2.6</p><h2>{selectedId} operating panel</h2></div><span className={`state-pill ${selectedCar?.available ? "ok" : "warn"}`}>{selectedCar?.state.replaceAll("_", " ") ?? "Loading"}</span></div><div className="car-state-grid"><div><span>Floor</span><strong>{floorLabel(selectedCar?.current_floor ?? null)}</strong></div><div><span>Direction</span><strong>{selectedCar?.direction ?? "—"}</strong></div><div><span>Door</span><strong>{selectedCar?.door_state ?? "—"}</strong></div><div><span>Next stop</span><strong>{floorLabel(selectedCar?.target_floor ?? null)}</strong></div></div><p className="authorization-feedback" aria-live="polite">{announcement}</p><div className="control-row"><button className="button secondary" disabled title="Door control is not integrated with equipment">Door open</button><button className="button secondary" disabled title="Door control is not integrated with equipment">Door close</button><button className="button emergency-button" disabled title="Emergency workflows are not yet implemented end-to-end">Emergency / help</button><button className="button medical-button" disabled title="Medical emergency workflows are not yet implemented end-to-end">Medical</button></div><div className="destination-panel"><strong>Destination keys</strong><div className="destination-keys"><button disabled={busy || !selectedCar?.available || selectedId === "E5"} onClick={() => selectDestination(-1)}>B1</button>{Array.from({ length: 30 }, (_, index) => index + 1).map((floor) => <button disabled={busy || !selectedCar?.available} key={floor} onClick={() => selectDestination(floor)}>{floor}</button>)}</div></div></div>
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-9 · FR-1.3 to FR-1.7</p><h2>Hall panel</h2></div></div><div className="hall-controls"><label>Call floor<select value={hallFloor} onChange={(event) => setHallFloor(event.target.value)}>{SIM_FLOORS.map((floor) => <option key={floor} value={floor}>{floor === -1 ? "B1" : `Floor ${floor}`}</option>)}</select></label><button className="button secondary" disabled={busy || hallFloor === "30"} onClick={() => hallCall("ORDINARY", "UP")}>↑ Up</button><button className="button secondary" disabled={busy || hallFloor === "-1"} onClick={() => hallCall("ORDINARY", "DOWN")}>↓ Down</button><button className="button secondary freight-button" disabled={busy || hallFloor === "-1"} onClick={() => hallCall("FREIGHT")}>E5 Freight</button></div>{hallRequest && <div className="request-feedback" aria-live="polite"><strong>Request status: </strong>{hallRequest.status} · {hallRequest.assigned_car ?? "Unassigned"} · wait {hallRequest.wait_seconds ?? "—"}s</div>}</div>
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-3 · monitoring</p><h2>Fleet state and calls</h2></div></div><div className="sim-car-list">{cars.map((car) => <div className="sim-car-row" key={car.elevator_id}><span className="elevator-id">{car.elevator_id}</span><span className="sim-direction">{car.direction}</span><strong>{floorLabel(car.current_floor)}</strong><span>{car.state}</span><span className="weight">{car.pending_stops} stops</span></div>)}</div><p className="muted dark">{calls.filter((request) => request.status !== "COMPLETED").length} active hall requests. Data resets when the application restarts.</p></div>
      </div>
    </div>
  </section>;
}

function Credentials() {
  return <><PageHeader eyebrow="Access control" title="Credentials" detail="Manage cardholders, access scopes, and credential lifecycle." action={<button className="button primary" disabled>Issue credential</button>} /><div className="toolbar"><input aria-label="Search credentials" placeholder="Search by name or credential ID" disabled /><select aria-label="Credential status" disabled><option>All statuses</option></select></div><section className="panel"><div className="table-heading"><strong>Cardholders</strong><span>0 records</span></div><EmptyState title="Credential records are not connected" detail="The API will populate this table from the credentials and users tables." /></section></>;
}

function Security() {
  return <><PageHeader eyebrow="Access control" title="Security alerts" detail="Review authentication anomalies and active incidents." action={<button className="button secondary" disabled>Export alerts</button>} /><div className="summary-grid compact"><article className="summary-card"><span className="summary-icon red">!</span><div><span>Open alerts</span><strong>—</strong><small>Awaiting telemetry</small></div></article><article className="summary-card"><span className="summary-icon amber">!</span><div><span>Critical alerts</span><strong>—</strong><small>Awaiting telemetry</small></div></article></div><section className="panel section"><div className="table-heading"><strong>Alert queue</strong><span>0 records</span></div><EmptyState title="No alert records available" detail="Security alerts will appear here once the ACS event source is connected." /></section></>;
}

function Analytics() {
  const [profile, setProfile] = useState("All traffic");
  return <><PageHeader eyebrow="Mode 5" title="Analytics" detail="Read-only operational KPIs and downloadable reports." action={<button className="button secondary" disabled>Export report</button>} /><div className="filter-bar"><label>Time window<input type="date" disabled /></label><label>Traffic profile<select value={profile} onChange={(event) => setProfile(event.target.value)}><option>All traffic</option><option>Peak</option><option>Off-peak</option><option>Stress</option></select></label></div><div className="kpi-grid">{["Mean wait time", "P95 wait time", "Travel time", "Availability", "Drop rate", "Security denials"].map((label) => <article className="kpi-card" key={label}><span>{label}</span><strong>—</strong><small>Awaiting database metrics</small></article>)}</div><section className="panel section"><div className="section-heading"><div><p className="eyebrow">Distribution</p><h2>Trip and utilization trends</h2></div></div><EmptyState title="Analytics data is not connected" detail={`Selected profile: ${profile}. Metrics will be loaded from the on-demand analytics queries.`} /></section></>;
}

function Audit() {
  return <><PageHeader eyebrow="Governance" title="Audit log" detail="Review the tamper-evident activity ledger." action={<button className="button secondary" disabled>Verify integrity</button>} /><div className="toolbar"><input aria-label="Search audit log" placeholder="Search by actor, action, or target" disabled /><select aria-label="Audit action" disabled><option>All actions</option></select></div><section className="panel"><div className="table-heading"><strong>Ledger entries</strong><span>0 records</span></div><EmptyState title="Audit records are not connected" detail="Ledger entries will be read from the append-only audit log chain." /></section></>;
}

function App() {
  const [view, setView] = useState<View>("overview");
  const [fleet, setFleet] = useState<FleetResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const active = useMemo(() => navigation.find((item) => item.id === view) ?? navigation[0], [view]);

  const onFleetRefresh = useCallback(async () => {
    try {
      setFleet(await loadFleet());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load fleet status.");
    }
  }, []);

  useEffect(() => { void onFleetRefresh(); }, [onFleetRefresh]);

  const page = view === "overview" ? <Overview fleet={fleet} /> : view === "simulation" ? <Simulation fleet={fleet} onFleetRefresh={onFleetRefresh} /> : view === "elevators" ? <Elevators fleet={fleet} /> : view === "credentials" ? <Credentials /> : view === "security" ? <Security /> : view === "analytics" ? <Analytics /> : <Audit />;
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark">S</span><div><strong>SREMS</strong><span>Operations</span></div></div><nav aria-label="Primary navigation">{navigation.map((item) => <button className={`nav-item ${item.id === view ? "active" : ""}`} key={item.id} onClick={() => setView(item.id)}><span className="nav-icon">{item.icon}</span>{item.label}</button>)}</nav><div className="sidebar-footer"><span className="status-dot" /> {error ? "API unavailable" : fleet ? "Test stub connected" : "Connecting"}</div></aside>
    <main className="content"><header className="topbar"><div><p className="eyebrow">Building operations</p><div className="breadcrumb">SREMS / {active.label}</div></div><div className="operator"><span className="avatar">A</span><span><strong>Administrator</strong><small>ADMIN</small></span></div></header>{error && <div className="error-banner" role="alert">{error}</div>}<div className="page-content">{page}</div></main>
  </div>;
}

export default App;
