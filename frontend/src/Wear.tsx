import { useCallback, useEffect, useState } from "react";
import { authorizedApi } from "./api.ts";

/* Wear-and-tear balancing (instructor NFR, phase 2): WEAR-1..WEAR-6, VT-33..VT-36. */

type CarWear = {
  elevator_id: string;
  group: string;
  floors_month: number;
  floors_period: number;
  floors_lifetime: number;
  starts_month: number;
  starts_period: number;
  starts_lifetime: number;
  doors_month: number;
  doors_period: number;
  doors_lifetime: number;
};
type WindowSummary = { min_floors: number; max_floors: number; spread_pct: number; limit_pct: number; within_limit: boolean };
type GroupSummary = {
  group: string;
  members: string[];
  balanced_by_dispatch: boolean;
  month_to_date: WindowSummary;
  period_to_date: WindowSummary;
};
type HistoryRow = {
  window_id: string;
  window_type: "MONTH" | "PERIOD";
  wear_group: string;
  window_start: string;
  window_end: string;
  per_car_floors: Record<string, number>;
  spread_pct: number;
  limit_pct: number;
  within_limit: boolean;
};
type WearStatus = {
  sim_time: string;
  month: { start: string; end: string; elapsed_fraction: number | null };
  period: { start: string; end: string; next_counter_reset: string };
  cars: CarWear[];
  groups: GroupSummary[];
  month_history: HistoryRow[];
  period_history: HistoryRow[];
  config: {
    monthly_limit_pct: number;
    period_limit_pct: number;
    period_months: number;
    balancing_enabled: boolean;
    gain_seconds: number;
    max_penalty_seconds: number;
  };
};
type WaitStat = { count: number; mean_seconds: number | null; p95_seconds: number | null };
type BenchRun = {
  label: string;
  balancing_enabled: boolean;
  months: number;
  start: string;
  journeys: number;
  wall_seconds: number;
  month_windows: HistoryRow[];
  period_windows: HistoryRow[];
  per_car_floors_total: Record<string, number>;
  per_car_starts_total: Record<string, number>;
  per_car_doors_total: Record<string, number>;
  wait: Record<string, WaitStat>;
  outages: { elevator_id: string; start: string; hours: number }[];
  verdict: {
    monthly_max_spread_pct: number;
    monthly_all_within_2pct: boolean;
    period_spreads_pct: number[];
    period_all_within_5pct: boolean | null;
    cumulative_spread_pct: number;
    perf9_peak_mean_ok: boolean;
    perf9_peak_p95_ok: boolean;
    perf10_offpeak_mean_ok: boolean;
  };
};
type BenchJob = {
  status: "IDLE" | "RUNNING" | "DONE" | "FAILED";
  progress?: number;
  message?: string;
  results?: BenchRun[];
  params?: { months: number; compare: boolean; outages: boolean; seed: number };
};

const RESIDENTIAL = ["E1", "E2", "E3", "E4"];
const fmtInt = (n: number | null | undefined): string => (n === null || n === undefined ? "—" : n.toLocaleString());
const fmtPct = (n: number | null | undefined, digits = 3): string => (n === null || n === undefined ? "—" : `${n.toFixed(digits)} %`);
const monthLabel = (iso: string): string => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso.slice(0, 7) : d.toLocaleDateString(undefined, { year: "numeric", month: "short", timeZone: "UTC" });
};
const dateLabel = (iso: string): string => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
};

/** Verdict chip: text + symbol, never colour alone (UI-5). */
function Verdict({ ok, label }: { ok: boolean | null; label?: string }) {
  if (ok === null) return <span className="badge unknown">— pending</span>;
  return <span className={`badge ${ok ? "ok" : "danger"}`}>{ok ? "✓ " : "✗ "}{label ?? (ok ? "Within limit" : "Limit exceeded")}</span>;
}

function FloorBars({ cars }: { cars: CarWear[] }) {
  const residential = cars.filter((c) => RESIDENTIAL.includes(c.elevator_id));
  const max = Math.max(1, ...residential.map((c) => c.floors_month));
  const min = Math.min(...residential.map((c) => c.floors_month));
  return <div className="wear-bars" role="list" aria-label="Floors travelled this month per residential car">
    {residential.map((car) => {
      const ahead = car.floors_month - min;
      return <div className="wear-bar-row" role="listitem" key={car.elevator_id}>
        <strong>{car.elevator_id}</strong>
        <div className="wear-bar-track"><div className="wear-bar-fill" style={{ width: `${(car.floors_month / max) * 100}%` }} /></div>
        <span className="wear-bar-value">{fmtInt(car.floors_month)} floors</span>
        <small>{ahead === 0 ? "least used" : `+${fmtInt(ahead)} vs least used`}</small>
      </div>;
    })}
  </div>;
}

function SpreadChart({ runs }: { runs: BenchRun[] }) {
  const months = Array.from(new Set(runs.flatMap((r) => r.month_windows.filter((m) => m.wear_group === "RESIDENTIAL").map((m) => m.window_start)))).sort();
  if (months.length === 0) return null;
  const maxVal = Math.max(2.5, ...runs.flatMap((r) => r.month_windows.map((m) => m.spread_pct)));
  const width = 720;
  const rowH = 22 * runs.length + 14;
  const height = months.length * rowH + 40;
  const left = 90;
  const scale = (v: number) => ((width - left - 110) * v) / maxVal;
  const limitX = left + scale(2);
  return <svg className="wear-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Monthly wear spread per run compared with the 2 percent limit">
    <line x1={limitX} x2={limitX} y1={10} y2={height - 24} className="wear-limit-line" />
    <text x={limitX + 4} y={20} className="wear-chart-note">2 % monthly limit</text>
    {months.map((month, mi) => <g key={month} transform={`translate(0, ${30 + mi * rowH})`}>
      <text x={0} y={14} className="wear-chart-label">{monthLabel(month)}</text>
      {runs.map((run, ri) => {
        const row = run.month_windows.find((m) => m.window_start === month && m.wear_group === "RESIDENTIAL");
        const value = row?.spread_pct ?? 0;
        return <g key={run.label} transform={`translate(0, ${ri * 22})`}>
          <rect x={left} y={2} width={Math.max(2, scale(value))} height={16} rx={3} className={run.balancing_enabled ? "wear-bar-on" : "wear-bar-off"} />
          <text x={Math.max(left + Math.max(2, scale(value)), limitX) + 8} y={14} className="wear-chart-value">{value.toFixed(3)} % {run.balancing_enabled ? "(balancing)" : "(original)"}</text>
        </g>;
      })}
    </g>)}
  </svg>;
}

function BenchResults({ runs }: { runs: BenchRun[] }) {
  return <>
    <div className="wear-run-grid">
      {runs.map((run) => {
        const peak = run.wait.PEAK ?? { mean_seconds: null, p95_seconds: null, count: 0 };
        const off = run.wait.OFF_PEAK ?? { mean_seconds: null, p95_seconds: null, count: 0 };
        return <article className="kpi-card wear-run" key={run.label}>
          <span>{run.label}</span>
          <strong>{fmtPct(run.verdict.monthly_max_spread_pct)}</strong>
          <small>worst monthly spread, E1–E4 ({run.months} month{run.months > 1 ? "s" : ""}, {fmtInt(run.journeys)} journeys)</small>
          <div className="wear-verdicts">
            <Verdict ok={run.verdict.monthly_all_within_2pct} label={run.verdict.monthly_all_within_2pct ? "Every month < 2 %" : "Monthly limit missed"} />
            <Verdict ok={run.verdict.period_all_within_5pct} label={run.verdict.period_all_within_5pct === null ? undefined : run.verdict.period_all_within_5pct ? "6-month ≤ 5 %" : "6-month limit missed"} />
            <Verdict ok={run.verdict.perf9_peak_mean_ok && run.verdict.perf9_peak_p95_ok && run.verdict.perf10_offpeak_mean_ok} label="PERF-9/10 wait targets" />
          </div>
          <dl className="wear-dl">
            <dt>Peak wait (mean / p95)</dt><dd>{peak.mean_seconds ?? "—"} s / {peak.p95_seconds ?? "—"} s</dd>
            <dt>Off-peak wait (mean)</dt><dd>{off.mean_seconds ?? "—"} s</dd>
            <dt>Floors E1 / E2 / E3 / E4</dt><dd>{RESIDENTIAL.map((e) => fmtInt(run.per_car_floors_total[e])).join(" / ")}</dd>
            <dt>6-month spread</dt><dd>{run.verdict.period_spreads_pct.length ? run.verdict.period_spreads_pct.map((p) => `${p.toFixed(3)} %`).join(", ") : "period not finished"}</dd>
            <dt>Maintenance outages</dt><dd>{run.outages.length ? `${run.outages.length} injected` : "none"}</dd>
          </dl>
        </article>;
      })}
    </div>
    <div className="wear-chart-block"><div className="section-heading"><div><p className="eyebrow">WEAR-2 · per calendar month</p><h2>Monthly spread vs limit</h2></div></div><SpreadChart runs={runs} /></div>
  </>;
}

export function WearPanel({ token, role }: { token: string; role: string }) {
  const isStaff = Boolean(token) && (role === "ADMIN" || role === "TECHNICIAN");
  const isAdmin = Boolean(token) && role === "ADMIN";
  const [status, setStatus] = useState<WearStatus | null>(null);
  const [error, setError] = useState("");
  const [job, setJob] = useState<BenchJob>({ status: "IDLE" });
  const [months, setMonths] = useState(1);
  const [compare, setCompare] = useState(true);
  const [outages, setOutages] = useState(true);
  const [report, setReport] = useState<BenchRun[] | null>(null);

  const refresh = useCallback(async () => {
    if (!isStaff) return;
    try {
      setStatus(await authorizedApi<WearStatus>("/api/v1/operational/wear", token));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load trip counters");
    }
  }, [isStaff, token]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 2000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const pollJob = useCallback(async () => {
    if (!isAdmin) return;
    try {
      setJob(await authorizedApi<BenchJob>("/api/v1/operational/wear/benchmark", token));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to read benchmark status");
    }
  }, [isAdmin, token]);

  useEffect(() => { void pollJob(); }, [pollJob]);
  useEffect(() => {
    if (job.status !== "RUNNING") return;
    const timer = window.setInterval(() => { void pollJob(); }, 1000);
    return () => window.clearInterval(timer);
  }, [job.status, pollJob]);

  const startJob = async () => {
    setError("");
    try {
      setJob(await authorizedApi<BenchJob>("/api/v1/operational/wear/benchmark", token, { months, compare, outages }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to start the accelerated run");
    }
  };

  const loadReport = async () => {
    setError("");
    try {
      const data = await authorizedApi<{ results: BenchRun[] }>("/api/v1/operational/wear/report", token);
      setReport(data.results);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load the shipped report");
    }
  };

  const residential = status?.groups.find((g) => g.group === "RESIDENTIAL");
  const freight = status?.groups.find((g) => g.group === "FREIGHT");
  const monthHistory = (status?.month_history ?? []).filter((r) => r.wear_group === "RESIDENTIAL").slice().reverse();

  return <>
    <div className="page-header"><div><p className="eyebrow">Instructor NFR · phase 2 · WEAR-1..WEAR-6</p><h1>Wear &amp; tear balancing</h1>
      <p className="page-detail">Trip counters count the floors each car travels (plus motor starts and door cycles). Dispatch keeps the residential cars E1–E4 within 2 % of each other every calendar month and within 5 % over each 6-month period; the trip counters reset every 6 months.</p></div>
      {isStaff && <button className="button secondary" onClick={() => { void refresh(); }}>Refresh</button>}
    </div>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {!isStaff ? <section className="panel"><div className="empty-state"><span className="empty-icon">—</span><strong>Admin or Technician sign-in required</strong><p>Trip counters are maintenance data (VIEW_FLEET_STATUS). Sign in with an Admin or Technician card in the header.</p></div></section> : <>
      <div className="kpi-grid">
        <article className="kpi-card"><span>Month-to-date spread, E1–E4</span><strong>{fmtPct(residential?.month_to_date.spread_pct)}</strong><small>limit &lt; {status?.config.monthly_limit_pct ?? 2} % at month end</small><div className="wear-verdicts"><Verdict ok={residential ? residential.month_to_date.within_limit : null} label={residential?.month_to_date.within_limit ? "On track" : "Above limit now"} /></div></article>
        <article className="kpi-card"><span>6-month spread, E1–E4</span><strong>{fmtPct(residential?.period_to_date.spread_pct)}</strong><small>limit ≤ {status?.config.period_limit_pct ?? 5} % per {status?.config.period_months ?? 6}-month period</small><div className="wear-verdicts"><Verdict ok={residential ? residential.period_to_date.within_limit : null} /></div></article>
        <article className="kpi-card"><span>Next trip-counter reset</span><strong className="wear-date">{status ? dateLabel(status.period.next_counter_reset) : "—"}</strong><small>period started {status ? dateLabel(status.period.start) : "—"} · month {status?.month.elapsed_fraction != null ? `${Math.round(status.month.elapsed_fraction * 100)} %` : "—"} elapsed</small></article>
      </div>
      <p className="wear-note">Early in a month the counters are small, so a few trips move the month-to-date spread a lot; the requirement is judged on the closed month. Dispatch balancing is <strong>{status?.config.balancing_enabled ? "ON" : "OFF"}</strong>: at most {status?.config.max_penalty_seconds ?? 15} s is added to a busier car's predicted service time, so passengers are never kept waiting long for the sake of balance.</p>
      <section className="section panel"><div className="section-heading"><div><p className="eyebrow">WEAR-1 · live trip counters</p><h2>Floors travelled this month</h2></div><span className="refresh-label">{status ? `simulated time ${new Date(status.sim_time).toLocaleString()}` : ""}</span></div>
        {status && <FloorBars cars={status.cars} />}
        <div className="table-wrap"><table className="operations-table"><thead><tr><th>Car</th><th>Group</th><th>Floors · month</th><th>Floors · 6-month</th><th>Floors · lifetime</th><th>Motor starts · month</th><th>Door cycles · month</th></tr></thead>
          <tbody>{(status?.cars ?? []).map((c) => <tr key={c.elevator_id}><td><strong>{c.elevator_id}</strong></td><td>{c.group === "FREIGHT" ? "Freight (own group)" : "Residential"}</td><td>{fmtInt(c.floors_month)}</td><td>{fmtInt(c.floors_period)}</td><td>{fmtInt(c.floors_lifetime)}</td><td>{fmtInt(c.starts_month)}</td><td>{fmtInt(c.doors_month)}</td></tr>)}</tbody></table></div>
        {freight && <p className="wear-note">E5 is the only car allowed to take freight calls and may not take ordinary hall calls (FR-1.4, FR-1.13), so it cannot be balanced against E1–E4 without breaking those rules. It is counted and reported as its own group.</p>}
      </section>
      <section className="section panel"><div className="section-heading"><div><p className="eyebrow">WEAR-2 · WEAR-5 · closed windows</p><h2>Monthly results</h2></div></div>
        {monthHistory.length === 0 ? <div className="empty-state"><strong>No month has closed yet</strong><p>Results appear here on the first day of the next month. Use the accelerated run below to see whole months now.</p></div> :
          <div className="table-wrap"><table className="operations-table"><thead><tr><th>Month</th>{RESIDENTIAL.map((e) => <th key={e}>{e}</th>)}<th>Spread</th><th>Verdict</th></tr></thead>
            <tbody>{monthHistory.map((r) => <tr key={r.window_id}><td>{monthLabel(r.window_start)}</td>{RESIDENTIAL.map((e) => <td key={e}>{fmtInt(r.per_car_floors[e])}</td>)}<td>{fmtPct(r.spread_pct)}</td><td><Verdict ok={r.within_limit} /></td></tr>)}</tbody></table></div>}
      </section>
      {isAdmin && <section className="section panel"><div className="section-heading"><div><p className="eyebrow">VT-33 · accelerated proof run</p><h2>Simulate months of traffic now</h2></div>
        <button className="button secondary" onClick={() => { void loadReport(); }}>Load shipped 6-month report</button></div>
        <p className="wear-note">Runs on a separate simulator instance with complete passenger journeys (hall call → boarding → destination), CIBSE-style daily demand for 1,500 residents, and optional maintenance outages. The live fleet is not touched and nothing is written to the database. About 40 s per simulated month per run.</p>
        <div className="hall-controls wear-controls">
          <label>Months<select value={months} onChange={(e) => setMonths(Number(e.target.value))}>{[1, 2, 3, 4, 5, 6].map((m) => <option key={m} value={m}>{m}</option>)}</select></label>
          <label className="wear-check"><input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} />Compare with original dispatcher</label>
          <label className="wear-check"><input type="checkbox" checked={outages} onChange={(e) => setOutages(e.target.checked)} />Inject maintenance outages</label>
          <button className="button primary" disabled={job.status === "RUNNING"} onClick={() => { void startJob(); }}>{job.status === "RUNNING" ? "Running…" : "Start accelerated run"}</button>
        </div>
        {job.status === "RUNNING" && <div className="wear-progress"><div className="wear-progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round((job.progress ?? 0) * 100)} aria-label="Accelerated run progress"><div className="wear-progress-fill" style={{ width: `${(job.progress ?? 0) * 100}%` }} /></div><small aria-live="polite">{Math.round((job.progress ?? 0) * 100)} % · {job.message}</small></div>}
        {job.status === "FAILED" && <p className="error-banner" role="alert">Accelerated run failed: {job.message}</p>}
        {job.status === "DONE" && job.results && <BenchResults runs={job.results} />}
        {report && job.status !== "RUNNING" && <><h3 className="wear-subhead">Shipped report (docs/wear_report.json)</h3><BenchResults runs={report} /></>}
      </section>}
    </>}
  </>;
}
