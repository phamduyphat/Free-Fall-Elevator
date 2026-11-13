import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

type View = "overview" | "simulation" | "elevators" | "credentials" | "security" | "analytics" | "audit";
type ElevatorStatus = {
  elevator_id: string;
  state: "UNKNOWN";
  current_floor: number | null;
  direction: "UP" | "DOWN" | "IDLE" | "UNKNOWN";
  available: boolean | null;
  message: string;
};
type FleetResponse = { elevators: ElevatorStatus[]; source: string };

const navigation: { id: View; label: string; icon: string }[] = [
  { id: "overview", label: "Overview", icon: "O" },
  { id: "simulation", label: "Live simulation", icon: "S" },
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
  return <div className="elevator-grid">{(fleet?.elevators ?? []).map((elevator) => (
    <article className="elevator-card" key={elevator.elevator_id}>
      <div className="card-top"><span className="elevator-id">{elevator.elevator_id}</span><span className="badge unknown">Unknown</span></div>
      <div className="floor-display">—<span>floor</span></div>
      <div className="card-details"><span>Direction</span><strong>Unknown</strong></div>
      <p className="card-note">{elevator.message}</p>
    </article>
  ))}</div>;
}

function Overview({ fleet }: { fleet: FleetResponse | null }) {
  return <>
    <section className="hero-card"><div><p className="eyebrow">Live operations</p><h2>Elevator fleet status</h2><p className="muted">Operational telemetry will appear here when the elevator state source is connected.</p></div><div className="hero-metric"><strong>{fleet?.elevators.length ?? "—"}</strong><span>cars monitored</span></div></section>
    <section className="summary-grid">
      <article className="summary-card"><span className="summary-icon blue">~</span><div><span>Active calls</span><strong>—</strong><small>Awaiting telemetry</small></div></article>
      <article className="summary-card"><span className="summary-icon amber">!</span><div><span>Open alerts</span><strong>—</strong><small>Awaiting telemetry</small></div></article>
      <article className="summary-card"><span className="summary-icon green">+</span><div><span>Availability</span><strong>—</strong><small>Awaiting telemetry</small></div></article>
    </section>
    <section className="section"><div className="section-heading"><div><p className="eyebrow">Fleet</p><h2>Elevator snapshot</h2></div><span className="refresh-label">Initial snapshot</span></div><FleetCards fleet={fleet} /></section>
    <section className="section panel"><div className="section-heading"><div><p className="eyebrow">Activity</p><h2>Recent operations</h2></div></div><EmptyState title="No operational events available" detail="Connect the dispatch and telemetry sources to see recent activity." /></section>
  </>;
}

function Elevators({ fleet }: { fleet: FleetResponse | null }) {
  return <><PageHeader eyebrow="Fleet management" title="Elevators" detail="Monitor current position, availability, and maintenance state." action={<button className="button secondary" disabled>Refresh telemetry</button>} /><FleetCards fleet={fleet} /><section className="section panel"><div className="section-heading"><div><p className="eyebrow">Coverage</p><h2>Service matrix</h2></div></div><div className="matrix">{["Car", "Residential", "Freight", "Maintenance"].map((heading) => <strong key={heading}>{heading}</strong>)}{["E1", "E2", "E3", "E4", "E5"].flatMap((id) => [<span key={`${id}-id`}>{id}</span>, <span className="matrix-value" key={`${id}-r`}>—</span>, <span className="matrix-value" key={`${id}-f`}>—</span>, <span className="matrix-value" key={`${id}-m`}>—</span>])}</div></section></>;
}

type SimCar = { id: string; floor: number; direction: "UP" | "DOWN"; passengers: number; destination: number; door: "OPEN" | "CLOSED"; emergency: boolean; maintenance: boolean };

const SIM_FLOORS = Array.from({ length: 31 }, (_, index) => index);
const SIM_START: SimCar[] = [
  { id: "E1", floor: 8, direction: "UP", passengers: 3, destination: 18, door: "CLOSED", emergency: false, maintenance: false },
  { id: "E2", floor: 17, direction: "DOWN", passengers: 1, destination: 1, door: "CLOSED", emergency: false, maintenance: false },
  { id: "E3", floor: 12, direction: "UP", passengers: 5, destination: 25, door: "CLOSED", emergency: false, maintenance: false },
  { id: "E4", floor: 26, direction: "DOWN", passengers: 0, destination: 3, door: "CLOSED", emergency: false, maintenance: false },
  { id: "E5", floor: 1, direction: "UP", passengers: 2, destination: 15, door: "CLOSED", emergency: false, maintenance: false },
];

function Simulation() {
  const [cars, setCars] = useState<SimCar[]>(SIM_START);
  const [running, setRunning] = useState(true);
  const [selectedId, setSelectedId] = useState("E1");
  const [cardId, setCardId] = useState("ADMIN-CARD-0001");
  const [announcement, setAnnouncement] = useState("Simulation ready. Select an elevator to operate its panel.");
  const [hallFloor, setHallFloor] = useState("1");
  const [hallRequest, setHallRequest] = useState<string | null>(null);
  const selectedCar = cars.find((car) => car.id === selectedId) ?? cars[0];

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      setCars((current) => current.map((car) => {
        if (car.emergency || car.maintenance || car.door === "OPEN") return car;
        const nextFloor = car.floor + (car.direction === "UP" ? 1 : -1);
        if (nextFloor >= 30 || nextFloor <= 0 || nextFloor === car.destination) {
          const nextDirection = car.direction === "UP" ? "DOWN" : "UP";
          return {
            ...car,
            floor: Math.max(0, Math.min(30, nextFloor)),
            direction: nextDirection,
            destination: nextDirection === "UP" ? 30 : 0,
            passengers: nextFloor === car.destination ? (car.passengers + 2) % 7 : car.passengers,
          };
        }
        return { ...car, floor: nextFloor };
      }));
    }, 1400);
    return () => window.clearInterval(timer);
  }, [running]);

  const updateSelected = (update: Partial<SimCar>, message: string) => {
    setCars((current) => current.map((car) => car.id === selectedId ? { ...car, ...update } : car));
    setAnnouncement(message);
  };

  const selectDestination = (floor: number) => {
    if (selectedCar.maintenance || selectedCar.emergency) {
      setAnnouncement(`${selectedId} is unavailable. Destination request rejected.`);
      return;
    }
    updateSelected({ destination: floor, door: "CLOSED" }, `Destination ${floor === 0 ? "B1" : `floor ${floor}`} selected for ${selectedId}. Authorization granted.`);
  };

  const displayFloor = (floor: number) => floor === 0 ? "B1" : `F${floor}`;

  return <section className="simulation-page">
    <PageHeader eyebrow="Mode 1 · UI-1 to UI-10" title="Elevator operations" detail="Passenger controls, hall calls, fleet monitoring, and accessibility feedback." action={<button className="button primary" onClick={() => setRunning((value) => !value)}>{running ? "Pause simulation" : "Resume simulation"}</button>} />
    <div className="simulation-notice"><strong>Demo simulation</strong><span>Positions and passengers are generated locally. They are not live building telemetry.</span><span className="srs-tag">SRS UI prototype</span></div>
    <div className="simulation-toolbar"><label>Selected car<select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{cars.map((car) => <option key={car.id}>{car.id}</option>)}</select></label><label>Card reader<input value={cardId} onChange={(event) => setCardId(event.target.value)} aria-label="Simulated card credential" /></label><span className="authorization-status">Authorization feedback: <strong>{cardId ? "Ready" : "Card required"}</strong></span></div>
    <div className="simulation-layout">
      <div className="shaft-panel">
        <div className="shaft-labels">{[...SIM_FLOORS].reverse().map((floor) => <span key={floor}>{floor === 0 ? "B1" : floor}</span>)}</div>
        <div className="shaft-grid">{[...SIM_FLOORS].reverse().map((floor) => <div className="floor-line" key={floor}><span>{floor === 0 ? "Basement 1" : `Floor ${floor}`}</span></div>)}
          {cars.map((car) => <div className={`sim-car ${car.emergency ? "emergency-car" : ""} ${car.maintenance ? "maintenance-car" : ""}`} key={car.id} style={{ bottom: `${(car.floor / 30) * 100}%`, left: `${(Number(car.id.slice(1)) - 1) * 20 + 1}%` }} title={`${car.id}: floor ${displayFloor(car.floor)}`}>
            <div className="sim-car-header"><strong>{car.id}</strong><span>{car.emergency ? "!" : car.direction === "UP" ? "^" : "v"}</span></div>
            <div className="passenger-cabin">{Array.from({ length: car.passengers }, (_, index) => <span className="passenger" key={index}>{index % 2 === 0 ? "●" : "○"}</span>)}</div>
            <small>{displayFloor(car.floor)}</small>
          </div>)}
        </div>
      </div>
      <div className="simulation-side">
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-5 · UI-8</p><h2>{selectedId} operating panel</h2></div><span className={`state-pill ${selectedCar.emergency ? "danger" : selectedCar.maintenance ? "warn" : "ok"}`}>{selectedCar.emergency ? "Emergency" : selectedCar.maintenance ? "Maintenance" : "In service"}</span></div><div className="car-state-grid"><div><span>Floor</span><strong>{displayFloor(selectedCar.floor)}</strong></div><div><span>Direction</span><strong>{selectedCar.direction}</strong></div><div><span>Door</span><strong>{selectedCar.door}</strong></div><div><span>Load</span><strong>{selectedCar.passengers * 75} kg</strong></div></div><p className={`authorization-feedback ${selectedCar.emergency ? "danger-text" : ""}`} aria-live="polite">{announcement}</p><div className="control-row"><button className="button secondary" onClick={() => updateSelected({ door: "OPEN" }, `${selectedId} door-open command accepted.`)}>Door open</button><button className="button secondary" onClick={() => updateSelected({ door: "CLOSED" }, `${selectedId} door-close command accepted.`)}>Door close</button><button className="button emergency-button" onClick={() => updateSelected({ emergency: true }, `Emergency/help activated in ${selectedId}. Staff alert raised.`)}>Emergency / help</button><button className="button medical-button" onClick={() => setAnnouncement(`Medical assistance requested in ${selectedId}.`)}>Medical</button></div><div className="destination-panel"><strong>Destination keys</strong><div className="destination-keys"><button onClick={() => selectDestination(0)}>B1</button>{Array.from({ length: 30 }, (_, index) => index + 1).map((floor) => <button key={floor} onClick={() => selectDestination(floor)}>{floor}</button>)}</div></div></div>
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-9 · FR-1.7</p><h2>Hall panel</h2></div></div><div className="hall-controls"><label>Call floor<select value={hallFloor} onChange={(event) => setHallFloor(event.target.value)}>{SIM_FLOORS.map((floor) => <option key={floor} value={floor}>{floor === 0 ? "B1" : `Floor ${floor}`}</option>)}</select></label><button className="button secondary" onClick={() => setHallRequest(`Up call from ${hallFloor === "0" ? "B1" : `floor ${hallFloor}`} assigned to E1.`)}>^ Up</button><button className="button secondary" onClick={() => setHallRequest(`Down call from ${hallFloor === "0" ? "B1" : `floor ${hallFloor}`} assigned to E2.`)}>v Down</button><button className="button secondary freight-button" onClick={() => setHallRequest(`Freight request from floor ${hallFloor} assigned to E5.`)}>E5 Freight</button></div>{hallRequest && <div className="request-feedback" aria-live="polite"><strong>Request status:</strong> {hallRequest} <span>Arrival announcement enabled.</span></div>}</div>
        <div className="panel"><div className="section-heading"><div><p className="eyebrow">UI-3 · UI-4 · UI-10</p><h2>Fleet state and alerts</h2></div></div><div className="sim-car-list">{cars.map((car) => <div className="sim-car-row" key={car.id}><span className="elevator-id">{car.id}</span><span className="sim-direction">{car.emergency ? "! Emergency" : car.direction === "UP" ? "^ Up" : "v Down"}</span><strong>{displayFloor(car.floor)}</strong><span>{car.passengers} passengers · {car.door}</span><span className="weight">{car.maintenance ? "MNT" : `${car.passengers * 75} kg`}</span></div>)}</div></div>
        <div className="panel simulation-legend"><p className="eyebrow">Accessibility and legend</p><div><span className="legend-car" /> Elevator car</div><div><span className="legend-passenger">●</span> Passenger</div><div><span className="legend-line" /> Floor level</div><p className="muted dark" aria-live="polite">{announcement}</p></div>
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

  useEffect(() => {
    let activeRequest = true;
    loadFleet().then((data) => { if (activeRequest) setFleet(data); }).catch((reason: unknown) => {
      if (activeRequest) setError(reason instanceof Error ? reason.message : "Unable to load fleet status.");
    });
    return () => { activeRequest = false; };
  }, []);

  const page = view === "overview" ? <Overview fleet={fleet} /> : view === "simulation" ? <Simulation /> : view === "elevators" ? <Elevators fleet={fleet} /> : view === "credentials" ? <Credentials /> : view === "security" ? <Security /> : view === "analytics" ? <Analytics /> : <Audit />;
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark">S</span><div><strong>SREMS</strong><span>Operations</span></div></div><nav aria-label="Primary navigation">{navigation.map((item) => <button className={`nav-item ${item.id === view ? "active" : ""}`} key={item.id} onClick={() => setView(item.id)}><span className="nav-icon">{item.icon}</span>{item.label}</button>)}</nav><div className="sidebar-footer"><span className="status-dot" /> API connected</div></aside>
    <main className="content"><header className="topbar"><div><p className="eyebrow">Building operations</p><div className="breadcrumb">SREMS / {active.label}</div></div><div className="operator"><span className="avatar">A</span><span><strong>Administrator</strong><small>ADMIN</small></span></div></header>{error && <div className="error-banner" role="alert">{error}</div>}<div className="page-content">{page}</div></main>
  </div>;
}

export default App;
