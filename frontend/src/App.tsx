import { useEffect, useState } from "react";

type ElevatorStatus = {
  elevator_id: string;
  state: "UNKNOWN";
  current_floor: number | null;
  direction: "UP" | "DOWN" | "IDLE" | "UNKNOWN";
  available: boolean | null;
  message: string;
};

type FleetResponse = {
  elevators: ElevatorStatus[];
  source: string;
};

async function loadFleet(): Promise<FleetResponse> {
  const response = await fetch("/api/v1/fleet/status");
  if (!response.ok) {
    throw new Error(`Fleet request failed with status ${response.status}`);
  }
  return response.json() as Promise<FleetResponse>;
}

function App() {
  const [fleet, setFleet] = useState<FleetResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    loadFleet()
      .then((data) => {
        if (active) setFleet(data);
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(reason instanceof Error ? reason.message : "Unable to load fleet status.");
        }
      });

    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">S</span>
          <div>
            <strong>SREMS</strong>
            <span>Operations</span>
          </div>
        </div>
        <nav aria-label="Primary navigation">
          <a className="nav-item active" href="#overview">Overview</a>
          <a className="nav-item" href="#elevators">Elevators</a>
          <a className="nav-item" href="#credentials">Credentials</a>
          <a className="nav-item" href="#security">Security alerts</a>
          <a className="nav-item" href="#analytics">Analytics</a>
          <a className="nav-item" href="#audit">Audit log</a>
        </nav>
        <div className="sidebar-footer">
          <span className="status-dot" /> API connected
        </div>
      </aside>

      <main className="content">
        <header className="topbar">
          <div>
            <p className="eyebrow">Building operations</p>
            <h1>Fleet overview</h1>
          </div>
          <div className="operator">
            <span className="avatar">A</span>
            <span><strong>Administrator</strong><small>ADMIN</small></span>
          </div>
        </header>

        <section className="hero-card">
          <div>
            <p className="eyebrow">Live operations</p>
            <h2>Elevator fleet status</h2>
            <p className="muted">Operational telemetry will appear here when the elevator state source is connected.</p>
          </div>
          <div className="hero-metric"><strong>{fleet?.elevators.length ?? "—"}</strong><span>cars monitored</span></div>
        </section>

        {error && <div className="error-banner" role="alert">{error}</div>}
        <section className="section" id="elevators">
          <div className="section-heading">
            <div><p className="eyebrow">Fleet</p><h2>Elevators</h2></div>
            <span className="refresh-label">Initial snapshot</span>
          </div>
          <div className="elevator-grid">
            {(fleet?.elevators ?? []).map((elevator) => (
              <article className="elevator-card" key={elevator.elevator_id}>
                <div className="card-top"><span className="elevator-id">{elevator.elevator_id}</span><span className="badge unknown">Unknown</span></div>
                <div className="floor-display">—<span>floor</span></div>
                <div className="card-details"><span>Direction</span><strong>Unknown</strong></div>
                <p className="card-note">{elevator.message}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="summary-grid" id="overview">
          <article className="summary-card"><span className="summary-icon blue">⌁</span><div><span>Active calls</span><strong>—</strong><small>Awaiting telemetry</small></div></article>
          <article className="summary-card"><span className="summary-icon amber">!</span><div><span>Open alerts</span><strong>—</strong><small>Awaiting telemetry</small></div></article>
          <article className="summary-card"><span className="summary-icon green">↗</span><div><span>Availability</span><strong>—</strong><small>Awaiting telemetry</small></div></article>
        </section>
      </main>
    </div>
  );
}

export default App;
