// SREMS scenario player: plays back recordings made by `python -m simulation.story_recorder`
// (window.SREMS_STORIES in stories.js). Left: the tower in cross-section at the recorded
// simulated time. Right: which module acted, what it decided and why, and the test's pass
// criteria ticking off as the run reaches them. Plain JavaScript, no build step, works from
// file:// as well as from the Vite dev server (/player/index.html).
"use strict";

const DATA = window.SREMS_STORIES;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fl = (f) => (f === null || f === undefined ? "—" : f === -1 ? "B1" : String(f));
const level = (f) => (f === -1 ? 0 : f);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const ease = (u) => u * u * (3 - 2 * u);
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const LANES = [
  { key: "stub", name: "Equipment & people", c: "var(--stub)", hex: "#56626d" },
  { key: "dispatch", name: "Dispatch", c: "var(--dispatch)", hex: "#1f55a6" },
  { key: "emergency", name: "Emergency", c: "var(--emergency)", hex: "#b42318" },
  { key: "maintenance", name: "Maintenance", c: "var(--maintenance)", hex: "#8a5a00" },
  { key: "audit", name: "Audit & analytics", c: "var(--audit)", hex: "#1f7a55" },
];
const LANE = Object.fromEntries(LANES.map((l) => [l.key, l]));
const STATE = {
  S: { label: "In service", hex: "#46535f" }, O: { label: "Service run", hex: "#1f55a6" },
  M: { label: "Maintenance", hex: "#8a5a00" }, F: { label: "Fault", hex: "#a8480c" },
  E: { label: "Emergency", hex: "#b42318" }, T: { label: "Stale", hex: "#5f6b76" },
};
const TYPE_LABEL = {
  Action: "action", HallCall: "hall call", HallCallMerged: "merged press", Assignment: "assignment", Arrival: "arrival",
  CarCall: "destination", Requeued: "requeued", IncidentOpened: "incident", Escalation: "escalation", IncidentAction: "incident",
  Availability: "availability", ResponderRun: "medical run", ResponderArrival: "responder floor", RescueDispatch: "rescue",
  RescueArrival: "rescue", RescueBoarding: "rescue", RescueRelease: "rescue", Obstruction: "door sensor", DoorCommand: "door",
  Reconciled: "reconciliation", FaultCleared: "fault", Cancelled: "cancel", NoBoarding: "empty call", BoardingEvidence: "evidence",
  WearWindow: "month close", Command: "interface command", Readings: "readings", PassengerBoard: "boarding", PassengerExit: "leaving",
};

// ------------------------------------------------------------------------------- state
let story = null;
let visible = [];       // indices into story.events shown in this mode
let cursor = -1;        // index into visible
let t = 0;
let playing = false;
let speed = 2;
let lastFrameAt = 0;
let holdUntil = 0;
let shownCriteria = new Set();

// ------------------------------------------------------------------------------- geometry
// Shaft layout (width SW): guide rail | cab (CW) | guide rail | counterweight.
const G = { W: 774, FH: 26, T0: 118, X0: 216, SW: 100, GAP: 12, LX: 44 };
G.H = G.T0 + 31 * G.FH + 18;
G.CW = 70;
G.CH = G.FH - 4;
const shaftX = (i) => G.X0 + i * (G.SW + G.GAP);
const cabX = (i) => shaftX(i) + 9;
const levelTop = (li) => G.T0 + (30 - li) * G.FH;   // top edge of level index li (B1 = 0)
const floorMid = (f) => levelTop(level(f)) + G.FH / 2;
const slabY = (f) => levelTop(level(f)) + G.FH;

// Drawing inks (kept in step with the tokens in player.css).
const C = {
  ink: "#18222d", ink2: "#46535f", ink3: "#5f6b76", rule: "#cbd3d8", poche: "#b4bec5", well: "#e9edef",
  sheet: "#fafbfb", wait: "#d98200", dispatch: "#1f55a6", emergency: "#b42318", maint: "#8a5a00",
  audit: "#1f7a55", person: "#3b4754", led: "#ff9e1b", ledOff: "#211809", display: "#17120b", fault: "#a8480c",
};

// ------------------------------------------------------------------------------- seven segments
const SEG = {
  "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg", "6": "acdefg", "7": "abc",
  "8": "abcdefg", "9": "abcdfg", "b": "cdefg", "-": "g", " ": "",
};
function digit(x, y, ch) {
  const w = 12, h = 21, k = 2.5, on = SEG[ch] ?? "";
  const seg = {
    a: [x + k, y, w - 2 * k, k], d: [x + k, y + h - k, w - 2 * k, k], g: [x + k, y + h / 2 - k / 2, w - 2 * k, k],
    f: [x, y + k * 0.6, k, h / 2 - k * 0.9], b: [x + w - k, y + k * 0.6, k, h / 2 - k * 0.9],
    e: [x, y + h / 2 + k * 0.3, k, h / 2 - k * 0.9], c: [x + w - k, y + h / 2 + k * 0.3, k, h / 2 - k * 0.9],
  };
  return Object.entries(seg).map(([s, [sx, sy, sw, sh]]) =>
    `<rect x="${sx.toFixed(1)}" y="${sy.toFixed(1)}" width="${sw.toFixed(1)}" height="${sh.toFixed(1)}" rx="1" fill="${on.includes(s) ? C.led : C.ledOff}"/>`).join("");
}
function ledText(floor) {
  if (floor === -1) return "b1";
  const s = String(floor);
  return s.length === 1 ? ` ${s}` : s;
}

// ------------------------------------------------------------------------------- frames
function frameAt(i) { return story.frames[clamp(i, 0, story.frames.length - 1)]; }

// Monotone cubic through the recorded floors: a car eases out of a stop, runs at steady
// speed past floors it does not stop at, and eases into the floor it stops at, never overshooting.
function carLevel(ci, time) {
  const i = Math.floor(time), u = time - i;
  const p = (k) => level(frameAt(k).c[ci][0]);
  const p0 = p(i - 1), p1 = p(i), p2 = p(i + 1), p3 = p(i + 2);
  if (p1 === p2) return p1;
  const tan = (a, b, c) => ((b === a || b === c) ? 0 : (c - a) / 2);
  const m1 = tan(p0, p1, p2), m2 = tan(p1, p2, p3);
  const u2 = u * u, u3 = u2 * u;
  return (2 * u3 - 3 * u2 + 1) * p1 + (u3 - 2 * u2 + u) * m1 + (-2 * u3 + 3 * u2) * p2 + (u3 - u2) * m2;
}

function doorOpen(ci, time) {
  const i = Math.floor(time), u = time - i;
  const d = (k) => frameAt(k).c[ci][2];
  const now = d(i), next = d(i + 1), before = d(i - 1);
  const open = (s) => s === "O" || s === "X";
  if (now === "X") return 0.4;
  if (!open(now)) return 0;
  let o = 1;
  if (!open(before)) o = Math.min(o, ease(clamp(u / 0.6, 0, 1)));
  if (!open(next)) o = Math.min(o, 1 - ease(clamp((u - 0.35) / 0.6, 0, 1)));
  return o;
}

function carMoving(time) {
  const a = frameAt(Math.floor(time)), b = frameAt(Math.floor(time) + 2);
  return a.c.some((c, i) => c[0] !== b.c[i][0]);
}

// ------------------------------------------------------------------------------- drawing: static layer
function buildTower() {
  const svg = $("#tower");
  svg.setAttribute("viewBox", `0 0 ${G.W} ${G.H}`);
  const p = [];
  p.push(`<defs>
    <pattern id="concrete" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="5" height="5" fill="#d5dce0"/><line x1="0" y1="0" x2="0" y2="5" stroke="${C.ink3}" stroke-width=".8"/></pattern>
    <pattern id="earth" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="9" stroke="${C.ink3}" stroke-width=".7"/><line x1="4.5" y1="0" x2="4.5" y2="3" stroke="${C.ink3}" stroke-width=".7"/></pattern>
    <pattern id="steel" width="6" height="4" patternUnits="userSpaceOnUse"><rect width="6" height="4" fill="#dfe5e9"/><line x1="0" y1="3.5" x2="6" y2="3.5" stroke="${C.ink3}" stroke-width=".5"/></pattern>
    <pattern id="hazard" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="#e8b23a"/><rect width="3" height="6" fill="${C.ink}"/></pattern>
    <linearGradient id="door" x1="0" x2="1"><stop offset="0" stop-color="#cfd7dd"/><stop offset=".5" stop-color="#f1f4f6"/><stop offset="1" stop-color="#d3dbe0"/></linearGradient>
    <linearGradient id="lit" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff3d6"/><stop offset="1" stop-color="#ffd98c"/></linearGradient>
  </defs>`);
  const top = G.T0, bottom = slabY(-1);
  const coreL = G.X0 - 5, right = shaftX(4) + G.SW, coreR = right + 7;

  // ground below B1
  p.push(`<rect x="${G.LX - 6}" y="${bottom + 2}" width="${coreR - G.LX + 6}" height="14" fill="url(#earth)"/>`);
  p.push(`<line x1="${G.LX - 6}" x2="${coreR}" y1="${bottom + 2}" y2="${bottom + 2}" stroke="${C.ink}" stroke-width="1"/>`);

  // landings: slabs cut in section (solid poché), level datum and label on the left
  for (let li = 0; li <= 30; li += 1) {
    const f = li === 0 ? -1 : li;
    const y = slabY(f);
    const main = f === 1 || f === -1;
    p.push(`<rect x="${G.LX - 6}" y="${y - 1.5}" width="${coreL - G.LX + 6}" height="${main ? 3.6 : 2.6}" fill="${main ? C.ink2 : C.poche}"/>`);
    p.push(`<text x="${G.LX - 12}" y="${y - G.FH / 2 + 4}" text-anchor="end" class="t-level${main ? " main" : ""}">${fl(f)}</text>`);
  }
  p.push(`<rect x="${G.LX - 6}" y="${top - 3}" width="${coreR - G.LX + 6}" height="3" fill="${C.poche}"/>`);
  p.push(`<text x="${G.LX + 4}" y="${slabY(1) - 7}" class="t-note strong">Lobby</text>`);
  p.push(`<text x="${G.LX + 4}" y="${slabY(-1) - 7}" class="t-note">Parking</text>`);

  // machine room on the roof
  p.push(`<rect x="${coreL}" y="${top - 60}" width="${coreR - coreL}" height="57" fill="${C.sheet}" stroke="${C.ink}" stroke-width="1.1"/>`);
  p.push(`<rect x="${coreL}" y="${top - 64}" width="${coreR - coreL}" height="4" fill="${C.poche}" stroke="${C.ink}" stroke-width=".6"/>`);
  p.push(`<text x="${coreL - 8}" y="${top - 34}" text-anchor="end" class="t-note">Machine</text><text x="${coreL - 8}" y="${top - 21}" text-anchor="end" class="t-note">room</text>`);

  // core walls (hatched concrete) and shaft voids
  p.push(`<rect x="${coreL}" y="${top - 3}" width="5" height="${bottom - top + 5}" fill="url(#concrete)" stroke="${C.ink}" stroke-width=".7"/>`);
  p.push(`<rect x="${right}" y="${top - 3}" width="7" height="${bottom - top + 5}" fill="url(#concrete)" stroke="${C.ink}" stroke-width=".7"/>`);
  DATA.cars.forEach((id, i) => {
    const x = shaftX(i);
    p.push(`<rect x="${x}" y="${top}" width="${G.SW}" height="${bottom - top + 12}" fill="${C.well}"/>`);
    // guide rails
    p.push(`<path d="M${x + 5},${top}V${bottom + 12}M${x + 9 + G.CW + 4},${top}V${bottom + 12}" stroke="${C.ink3}" stroke-width=".9" opacity=".7"/>`);
    // landing sills
    for (let li = 0; li <= 30; li += 1) {
      const y = slabY(li === 0 ? -1 : li);
      p.push(`<line x1="${x}" x2="${x + 7}" y1="${y}" y2="${y}" stroke="${C.ink3}" stroke-width="1.2"/>`);
    }
    // traction machine: motor, drive sheave over the cab, deflector over the counterweight
    p.push(`<rect x="${x + 8}" y="${top - 47}" width="24" height="24" fill="${C.sheet}" stroke="${C.ink}" stroke-width="1"/>`);
    p.push(`<path d="M${x + 12},${top - 41}h16M${x + 12},${top - 35}h16M${x + 12},${top - 29}h16" stroke="${C.ink3}" stroke-width=".8"/>`);
    p.push(`<circle cx="${x + G.SW - 8}" cy="${top - 11}" r="4.5" fill="${C.sheet}" stroke="${C.ink}" stroke-width="1"/>`);
    const sx = cabX(i) + G.CW / 2 + 11;
    p.push(`<path d="M${sx + 11},${top - 30} L${x + G.SW - 8 + 4.5},${top - 11}" stroke="${C.ink2}" stroke-width=".8"/>`);
    // pit buffers
    p.push(`<rect x="${cabX(i) + G.CW / 2 - 5}" y="${bottom + 3}" width="10" height="8" fill="${C.sheet}" stroke="${C.ink}" stroke-width=".8"/>`);
    p.push(`<rect x="${x + G.SW - 12}" y="${bottom + 3}" width="8" height="8" fill="${C.sheet}" stroke="${C.ink}" stroke-width=".8"/>`);
    if (i < 4) p.push(`<rect x="${x + G.SW}" y="${top - 3}" width="${G.GAP}" height="${bottom - top + 17}" fill="url(#concrete)" stroke="${C.ink}" stroke-width=".7"/>`);
    if (id === "E5") p.push(`<text x="${x + G.SW / 2}" y="${bottom + 30}" text-anchor="middle" class="t-note">freight</text>`);
  });
  p.push(`<g id="dyn"></g>`);
  svg.innerHTML = p.join("");
}

// ------------------------------------------------------------------------------- drawing: dynamic layer
function person(x, y, opacity = 1) {
  if (opacity <= 0.01) return "";
  return `<g opacity="${opacity.toFixed(2)}" fill="${C.person}"><circle cx="${x.toFixed(1)}" cy="${(y - 13).toFixed(1)}" r="2.8"/>` +
    `<path d="M${(x - 3.3).toFixed(1)},${y.toFixed(1)}v-6.5q0,-3.3 3.3,-3.3q3.3,0 3.3,3.3v6.5z"/></g>`;
}

function hallPanel(floor, calls) {
  const x = G.X0 - 20, y = floorMid(floor);
  const up = calls.find((c) => c.d === "U"), down = calls.find((c) => c.d === "D"), freight = calls.find((c) => c.d === "F");
  const waiting = (c) => c && (c.status === "Q" || c.status === "R");
  const fill = (c) => (!c ? "none" : waiting(c) ? C.wait : C.dispatch);
  const stroke = (c) => (!c ? C.ink3 : waiting(c) ? C.wait : C.dispatch);
  const blink = (c) => (waiting(c) && !reduceMotion ? ' class="blink"' : "");
  const out = [`<rect x="${x - 5.5}" y="${y - 10}" width="11" height="20" rx="1.5" fill="${C.sheet}" stroke="${C.ink3}" stroke-width=".7"/>`];
  if (floor !== 30) out.push(`<path d="M${x - 3.2},${y - 2} L${x},${y - 7.2} L${x + 3.2},${y - 2} Z" fill="${fill(up)}" stroke="${stroke(up)}" stroke-width=".7"${blink(up)}/>`);
  if (floor !== -1) out.push(`<path d="M${x - 3.2},${y + 2} L${x},${y + 7.2} L${x + 3.2},${y + 2} Z" fill="${fill(down)}" stroke="${stroke(down)}" stroke-width=".7"${blink(down)}/>`);
  if (freight) out.push(`<g${blink(freight)}><rect x="${x - 21}" y="${y - 6}" width="11" height="12" rx="1.5" fill="${fill(freight)}"/><text x="${x - 15.5}" y="${y + 3.6}" text-anchor="middle" font-size="9" font-weight="700" fill="${C.sheet}">F</text></g>`);
  if (calls.length) {
    const tip = calls.map((c) => `${fl(floor)} ${c.d === "U" ? "up" : c.d === "D" ? "down" : "freight"}${c.car ? `, assigned to ${c.car}` : ", waiting for a car"}`).join("; ");
    out.push(`<rect x="${x - 7}" y="${y - 11}" width="14" height="22" fill="transparent"><title>Hall call ${esc(tip)}</title></rect>`);
  }
  return out.join("");
}

function renderTower() {
  const i = Math.floor(t);
  const f = frameAt(i), prev = frameAt(i - 1);
  const u = t - i;
  const out = [];
  const focus = cursor >= 0 ? story.events[visible[cursor]] : null;
  const fire = f.i.some((x) => x[0] === "FIRE" || x[0] === "POWER_FAILURE");
  if (fire) {
    const a = reduceMotion ? 0.1 : 0.07 + 0.04 * Math.sin(t * 3);
    out.push(`<rect x="${G.LX - 6}" y="${G.T0}" width="${G.X0 - G.LX + 1}" height="${31 * G.FH}" fill="${C.emergency}" opacity="${a.toFixed(3)}"/>`);
  }
  const banner = $("#banner");
  banner.hidden = !fire;
  if (fire) banner.textContent = "Fire alarm: every car held where it is, no lobby recall (EMR-3.6)";

  // hall call panels on every landing
  const byFloor = new Map();
  for (const [floor, d, status, car] of f.h) {
    if (!byFloor.has(floor)) byFloor.set(floor, []);
    byFloor.get(floor).push({ d, status, car });
  }
  for (let li = 0; li <= 30; li += 1) {
    const floor = li === 0 ? -1 : li;
    out.push(hallPanel(floor, byFloor.get(floor) ?? []));
  }

  // people: queued on the landing, walking into the car, walking away after arriving
  const prevState = new Map(prev.p.map((q) => [q[0], q]));
  const slots = new Map();
  const carCentre = (car) => cabX(DATA.cars.indexOf(car)) + G.CW / 2;
  const queueX = G.X0 - 42;
  for (const [pid, st, floor, car] of f.p) {
    const was = prevState.get(pid);
    if (st === "w") {
      const n = slots.get(floor) ?? 0;
      slots.set(floor, n + 1);
      if (n < 12) out.push(person(queueX - n * 10.5, slabY(floor) - 1.5));
      else if (n === 12) out.push(`<text x="${G.LX + 2}" y="${floorMid(floor) + 4}" class="t-note">+${f.p.filter((q) => q[1] === "w" && q[2] === floor).length - 12}</text>`);
    } else if (st === "r" && was && was[1] === "w" && u < 0.95) {
      const k = ease(u / 0.95);
      const x1 = carCentre(car);
      out.push(person(queueX + (x1 - queueX) * k, slabY(was[2]) - 1.5, 1 - clamp((k - 0.8) / 0.2, 0, 1)));
    } else if (st === "x") {
      const first = !was || was[1] !== "x";
      const age = first ? u : 1 + u;
      const x0 = car ? carCentre(car) : G.X0 - 20;
      const k = ease(clamp(age / 1.7, 0, 1));
      out.push(person(x0 + (G.LX + 6 - x0) * k, slabY(floor) - 1.5, 1 - clamp((age - 1.1) / 0.8, 0, 1)));
    }
  }

  // cars, counterweights, ropes, sheaves, position indicators
  DATA.cars.forEach((id, ci) => {
    const [floor, dir, door, avail, stops, flags, , riders] = f.c[ci];
    const x = shaftX(ci);
    const li = carLevel(ci, t);
    const cx = cabX(ci), cw = G.CW, ch = G.CH;
    const y = levelTop(li) + (G.FH - ch) - 2.5;
    const cwY = G.T0 + 4 + (li / 30) * (31 * G.FH - 52);
    const ropeX = cx + cw / 2;
    const sx = ropeX + 11, sy = G.T0 - 30;
    out.push(`<path d="M${ropeX - 1.5},${sy}V${y - 3}M${ropeX + 1.5},${sy}V${y - 3}" stroke="${C.ink2}" stroke-width=".8"/>`);
    out.push(`<path d="M${x + G.SW - 8 + 3.6},${G.T0 - 11}V${cwY}" stroke="${C.ink2}" stroke-width=".8"/>`);
    const ang = reduceMotion ? 0 : li * 125;
    out.push(`<g transform="translate(${sx},${sy}) rotate(${ang.toFixed(1)})"><circle r="11" fill="${C.sheet}" stroke="${C.ink}" stroke-width="1.2"/><circle r="2.6" fill="${C.ink}"/><path d="M0,-9V9M-9,0H9M-6.4,-6.4L6.4,6.4M-6.4,6.4L6.4,-6.4" stroke="${C.ink3}" stroke-width=".8"/></g>`);
    const moving = Math.abs(carLevel(ci, t + 0.05) - li) > 0.004;
    out.push(`<circle cx="${x + 20}" cy="${G.T0 - 52}" r="2.2" fill="${moving ? C.audit : C.rule}"><title>Drive ${moving ? "running" : "stopped"}</title></circle>`);
    // counterweight
    out.push(`<rect x="${x + G.SW - 13}" y="${cwY.toFixed(1)}" width="10" height="44" fill="url(#steel)" stroke="${C.ink}" stroke-width=".8"/>`);
    // stop markers in the wall strip left of the shaft
    for (const [sf, kind] of stops) {
      const my = floorMid(sf), mx = x - 6;
      if (kind === "H") out.push(`<circle cx="${mx}" cy="${my}" r="3.6" fill="${C.sheet}" stroke="${C.dispatch}" stroke-width="1.7"/>`);
      else if (kind === "C" || kind === "V") out.push(`<circle cx="${mx}" cy="${my}" r="3.4" fill="${C.audit}" stroke="${C.sheet}" stroke-width=".8"/>`);
      else out.push(`<path d="M${mx - 4},${my}H${mx + 4}M${mx},${my - 4}V${my + 4}" stroke="${C.emergency}" stroke-width="2.4"/>`);
    }
    // car: sling, cab, sliding doors (lit inside when open)
    const o = doorOpen(ci, t);
    const st = STATE[avail] ?? STATE.S;
    const stale = avail === "T";
    const edge = stale ? C.ink3 : avail === "S" || avail === "O" ? C.ink : st.hex;
    const isFocus = focus && focus.car === id;
    out.push(`<g opacity="${stale ? 0.55 : 1}">`);
    if (isFocus) out.push(`<rect x="${cx - 6}" y="${y - 8}" width="${cw + 12}" height="${ch + 12}" fill="none" stroke="${laneOf(focus).hex}" stroke-width="1.3" stroke-dasharray="4 3"/>`);
    if (avail === "E" || flags.includes("m")) {
      out.push(`<rect x="${cx - 3.5}" y="${y - 5.5}" width="${cw + 7}" height="${ch + 8}" fill="none" stroke="${C.emergency}" stroke-width="1.4"${reduceMotion ? "" : ' class="alarm"'}/>`);
    }
    out.push(`<path d="M${cx - 1},${y + ch + 1}V${y - 3}H${cx + cw + 1}V${y + ch + 1}" fill="none" stroke="${C.ink}" stroke-width="1.3"/>`);
    out.push(`<rect x="${ropeX - 3}" y="${y - 5}" width="6" height="3" fill="${C.ink}"/>`);
    out.push(`<rect x="${cx}" y="${y}" width="${cw}" height="${ch}" fill="${C.sheet}" stroke="${edge}" stroke-width="${avail === "S" || avail === "O" ? 1.1 : 2}"${stale ? ' stroke-dasharray="3 2"' : ""}/>`);
    const inner = cw - 8, half = inner / 2, slide = half * o;
    if (o > 0.02) {
      const gap = inner * o;
      const ix = cx + cw / 2 - gap / 2;
      out.push(`<rect x="${ix.toFixed(2)}" y="${y + 2}" width="${gap.toFixed(2)}" height="${ch - 4}" fill="url(#lit)"/>`);
      const shown = Math.min(riders, Math.max(0, Math.floor((gap - 4) / 6)));
      for (let k = 0; k < shown; k += 1) {
        const px = ix + 5 + k * 6;
        out.push(`<circle cx="${px.toFixed(1)}" cy="${y + 7}" r="2" fill="${C.person}"/><rect x="${(px - 2.2).toFixed(1)}" y="${y + 9.4}" width="4.4" height="${ch - 11.4}" rx="1.6" fill="${C.person}"/>`);
      }
    }
    out.push(`<rect x="${cx + 4}" y="${y + 2}" width="${(half - slide).toFixed(2)}" height="${ch - 4}" fill="url(#door)" stroke="${C.ink3}" stroke-width=".5"/>`);
    out.push(`<rect x="${(cx + 4 + half + slide).toFixed(2)}" y="${y + 2}" width="${(half - slide).toFixed(2)}" height="${ch - 4}" fill="url(#door)" stroke="${C.ink3}" stroke-width=".5"/>`);
    if (avail === "M") out.push(`<rect x="${cx + 1}" y="${y + 1}" width="${cw - 2}" height="3.5" fill="url(#hazard)"/>`);
    if (door === "X") out.push(`<rect x="${cx + cw / 2 - 5}" y="${y + ch - 9}" width="10" height="7" fill="url(#hazard)" stroke="${C.ink}" stroke-width=".6"><title>Something is blocking the door</title></rect>`);
    if (o < 0.3) {
      out.push(`<text x="${cx + 10}" y="${y + ch / 2 + 3.8}" text-anchor="middle" class="t-car">${id}</text>`);
      if (riders > 0) out.push(`<g><circle cx="${cx + cw - 10}" cy="${y + ch / 2}" r="6.2" fill="${C.ink}"/><text x="${cx + cw - 10}" y="${y + ch / 2 + 3.3}" text-anchor="middle" font-size="8.5" font-weight="700" fill="${C.sheet}">${riders}</text></g>`);
    }
    if (flags.includes("m")) out.push(`<g><rect x="${cx + cw / 2 - 7}" y="${y + ch / 2 - 7}" width="14" height="14" rx="2" fill="${C.sheet}" stroke="${C.emergency}"/><path d="M${cx + cw / 2 - 4.5},${y + ch / 2}h9M${cx + cw / 2},${y + ch / 2 - 4.5}v9" stroke="${C.emergency}" stroke-width="2.6"/></g>`);
    if (flags.includes("r")) out.push(`<circle cx="${cx + cw / 2}" cy="${y - 10}" r="3" fill="${C.dispatch}"${reduceMotion ? "" : ' class="blink"'}><title>Rescue car</title></circle>`);
    if (stale) out.push(`<text x="${cx + cw + 6}" y="${y + 14}" font-size="13" font-weight="700" fill="${C.ink2}">?</text>`);
    if (flags.includes("f")) out.push(`<text x="${cx + cw + 6}" y="${y + 14}" font-size="13" font-weight="700" fill="${C.fault}">!</text>`);
    out.push(`</g>`);
    // position indicator above the shaft: floor, direction, door; then car name and state in words
    const ix = x + 6, iy = 8;
    out.push(`<rect x="${x + 2}" y="${iy - 4}" width="${G.SW - 4}" height="29" rx="2" fill="${C.display}"/>`);
    const txt = ledText(floor);
    out.push(digit(ix + 6, iy, txt[0]), digit(ix + 22, iy, txt[1]));
    const ax = ix + 56, ay = iy + 10.5;
    out.push(`<path d="M${ax - 6},${ay - 1.5} L${ax},${ay - 9.5} L${ax + 6},${ay - 1.5} Z" fill="${dir === "U" ? C.led : C.ledOff}"/>`);
    out.push(`<path d="M${ax - 6},${ay + 1.5} L${ax},${ay + 9.5} L${ax + 6},${ay + 1.5} Z" fill="${dir === "D" ? C.led : C.ledOff}"/>`);
    out.push(`<path d="M${ix + 72},${iy + 5}h4v11h-4zM${ix + 79},${iy + 5}h4v11h-4z" fill="${door === "C" ? C.ledOff : door === "X" ? "#ff5a4a" : C.led}"/>`);
    out.push(`<text x="${x + 3}" y="51" class="t-head">${id}</text>`);
    out.push(`<text x="${x + G.SW - 3}" y="51" text-anchor="end" class="t-state" fill="${st.hex}">${st.label}</text>`);
  });
  $("#dyn").innerHTML = out.join("");

  const base = new Date(story.base);
  const now = new Date(base.getTime() + t * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  $("#clock-time").textContent = `${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:${pad(now.getUTCSeconds())}`;
  $("#clock-date").textContent = now.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
  $("#clock-t").textContent = `${t.toFixed(1)} s`;
  $("#scrub").value = t;
  $("#t-now").textContent = `${t.toFixed(0)} s`;
  $("#t-step").textContent = visible.length ? `Step ${Math.max(0, cursor + 1)} of ${visible.length}` : "";
}

// ------------------------------------------------------------------------------- explanation
function laneOf(e) { return LANE[e.lane] ?? LANE.stub; }

function buildBus() {
  $("#bus").innerHTML = LANES.map((l) => `<div class="node" data-lane="${l.key}" style="--c:${l.c}"><i></i><span>${esc(l.name)}</span></div>`).join("");
}

function nodeX(key) {
  const node = $(`.node[data-lane="${key}"]`);
  const track = $("#bus").getBoundingClientRect();
  const r = node.getBoundingClientRect();
  return r.left + r.width / 2 - track.left;
}

function renderBus(e, animate) {
  $$("#bus .node").forEach((n) => {
    n.classList.toggle("on", Boolean(e) && n.dataset.lane === e.lane);
    n.classList.toggle("to", Boolean(e) && (e.to ?? []).includes(n.dataset.lane) && n.dataset.lane !== e.lane);
  });
  $$("#bus .pulse").forEach((p) => p.remove());
  if (!e || !animate || reduceMotion) return;
  for (const to of (e.to ?? []).filter((k) => k !== e.lane && LANE[k])) {
    const dot = document.createElement("span");
    dot.className = "pulse";
    dot.style.setProperty("--c", laneOf(e).c);
    dot.style.left = `${nodeX(e.lane)}px`;
    $("#bus").append(dot);
    requestAnimationFrame(() => requestAnimationFrame(() => { dot.style.left = `${nodeX(to)}px`; }));
    setTimeout(() => { dot.style.opacity = "0"; }, 900);
    setTimeout(() => dot.remove(), 1300);
  }
}

function whyDecision(w) {
  const rows = [...w.candidates].sort((a, b) => a.cost - b.cost || a.car.localeCompare(b.car));
  const max = Math.max(30, ...rows.map((r) => r.cost));
  const fastest = [...w.candidates].sort((a, b) => a.eta - b.eta)[0];
  const winner = w.candidates.find((r) => r.car === w.chosen);
  const target = w.call_type === "RESCUE" ? `rescue to floor ${fl(w.floor)}` : `${fl(w.floor)}${w.direction === "UP" ? " up" : w.direction === "DOWN" ? " down" : ""}${w.call_type === "FREIGHT" ? " (freight)" : ""}`;
  const html = rows.map((r) => {
    const win = r.car === w.chosen;
    const where = `${fl(r.floor)}${r.direction === "UP" ? "↑" : r.direction === "DOWN" ? "↓" : ""}${r.stops ? `, ${r.stops} stop${r.stops > 1 ? "s" : ""}` : ""}`;
    const val = r.penalty >= 0.05 ? `${r.eta} s + ${r.penalty.toFixed(1)} s = ${r.cost.toFixed(1)} s` : `${r.eta} s`;
    return `<div class="cand${win ? " win" : ""}" title="${esc(r.car)} at ${esc(where)}">
      <span class="id">${esc(r.car)}</span>
      <span class="bar"><b style="width:${(r.eta / max) * 100}%"></b><em style="left:${(r.eta / max) * 100}%;width:${(r.penalty / max) * 100}%"></em></span>
      <span class="val">${win ? "chosen, " : ""}${val}</span></div>`;
  }).join("") + (w.excluded ?? []).map((x) => `<div class="cand out"><span class="id">${esc(x.car)}</span><span class="bar"></span><span class="val">${esc(x.reason)}</span></div>`).join("");
  let note = "";
  if (w.busy_gate) {
    note = `The group is busy, so the wear penalty is off for this call: every car is needed now and the least-used one would only fill up (WEAR-3 busy rule). Fastest predicted service wins.`;
  } else if (winner && fastest && fastest.car !== winner.car && fastest.eta < winner.eta) {
    note = `${esc(winner.car)} is ${winner.eta - fastest.eta} s slower than ${esc(fastest.car)} but has travelled fewer floors this month, so ${esc(fastest.car)}'s cost carries a larger wear penalty (WEAR-3).`;
  } else if (winner && rows.some((r) => r.car !== winner.car && r.penalty >= w.cap - 0.01 && r.eta < winner.eta)) {
    note = `The penalty is capped at ${w.cap} s, so a car that is much faster still wins: balancing never costs a passenger more than ${w.cap} s (WEAR-3, PERF-9).`;
  } else if (w.call_type === "RESCUE") {
    note = "Rescue car: an eligible car chosen by ETA; never the affected car and never E5 (EMR-3.12).";
  } else if (winner && winner.index === 0 && winner.stops > 0) {
    note = `${esc(winner.car)} already passes floor ${fl(w.floor)} going the right way, so the stop is inserted on the way, before its other stops.`;
  } else if (winner) {
    note = `Lowest predicted service time wins (FR-1.4). Distance alone is never the rule.`;
  }
  return `<h4>Cost to serve ${esc(target)}: predicted time${w.balancing && !w.busy_gate ? " + wear penalty" : w.busy_gate ? " (wear penalty paused: group busy)" : ""}</h4>${html}
    <div class="why-key"><span><i style="background:var(--dispatch)"></i>predicted service time</span>${w.balancing ? `<span><i style="background:repeating-linear-gradient(135deg,var(--key) 0 2px,transparent 2px 5px);box-shadow:inset 0 0 0 1px var(--key)"></i>wear penalty, at most ${w.cap} s</span>` : ""}</div>
    ${note ? `<p class="why-note">${note}</p>` : ""}`;
}

function whyLifecycle(w, e) {
  const strip = w.states.map((s) => {
    const cls = s === w.current ? "cur" : s === w.previous ? "prev" : "";
    return `<span class="${cls}">${esc(s.replaceAll("_", " ").toLowerCase())}</span>`;
  }).join("<em>›</em>");
  const fields = w.fields ? `<dl class="fields">${Object.entries(w.fields).map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>` : "";
  return `<h4>${esc(w.machine)} state${w.note ? ` (${esc(w.note)})` : ""}</h4><div class="strip" style="--c:${laneOf(e).c}">${strip}</div>${fields}`;
}

function whyRoute(w) {
  const chips = w.stops.map(([f, k], i) => `<span class="${i === w.highlight ? "hl" : ""}">${k === "H" ? "pick up " : ""}${fl(f)}</span>`).join("<i>›</i>");
  return `<h4>${esc(w.car)}'s committed route from floor ${fl(w.from)}</h4><div class="route"><span>${fl(w.from)}</span><i>›</i>${chips}</div>`;
}

function whyChecks(w) {
  return `<h4>${esc(w.title)}</h4><ul class="checks">${w.items.map((it) => `<li class="${it.ok ? "" : "no"}">${esc(it.text)}</li>`).join("")}</ul>`;
}

function whyWear(w) {
  const rows = (w.rows ?? []).filter((r) => r.wear_group === "RESIDENTIAL");
  return rows.map((r) => `<h4>Month closed: ${esc(String(r.window_start).slice(0, 7))}, residential group</h4>
    <dl class="fields">${Object.entries(r.per_car_floors).map(([c, v]) => `<div><dt>${esc(c)}</dt><dd>${Number(v).toLocaleString("en-US")} floors</dd></div>`).join("")}
    <div><dt>spread</dt><dd>${Number(r.spread_pct).toFixed(2)} %</dd></div><div><dt>limit</dt><dd>&lt; ${r.limit_pct} %</dd></div>
    <div><dt>verdict</dt><dd style="color:${r.within_limit ? "var(--ok)" : "var(--bad)"}">${r.within_limit ? "✓ within limit" : "✗ exceeded"}</dd></div></dl>`).join("");
}

function whyCommand(w) {
  const colors = { ACKNOWLEDGED: "var(--ok)", REJECTED: "var(--bad)", TIMED_OUT: "var(--key)", UNKNOWN: "var(--key)" };
  return `<h4>Interface command status (COM-3)</h4><div class="strip">${["ACKNOWLEDGED", "REJECTED", "TIMED_OUT", "UNKNOWN"].map((s) =>
    `<span class="${s === w.status ? "cur" : ""}" style="--c:${colors[s]}">${s.replace("_", " ").toLowerCase()}</span>`).join("")}</div>
    <p class="why-note">${esc(w.type)}: ${esc(w.detail)}</p>`;
}

function renderEventCard(e) {
  const box = $("#event");
  if (!e) {
    box.style.setProperty("--c", "var(--rule)");
    box.innerHTML = `<p class="empty">Press play or step with the arrow keys. Each step shows which module acted and why.</p>`;
    return;
  }
  const lane = laneOf(e);
  box.style.setProperty("--c", lane.c);
  let why = "";
  const w = e.why;
  if (w) {
    if (w.kind === "decision") why = whyDecision(w);
    else if (w.kind === "lifecycle") why = whyLifecycle(w, e);
    else if (w.kind === "route") why = whyRoute(w);
    else if (w.kind === "checks") why = whyChecks(w);
    else if (w.kind === "wear") why = whyWear(w);
    else if (w.kind === "command") why = whyCommand(w);
  }
  const code = e.code && DATA.code[e.code] ? `<details class="code"${e.why ? "" : " open"}><summary>Code that ran</summary>${DATA.code[e.code].map((c) =>
    `<div class="fn"><code>${esc(c.fn)}()</code><span class="loc">${esc(c.file)}:${c.line}</span><p>${esc(c.why)}</p></div>`).join("")}</details>` : "";
  const result = e.result ? `<div class="ev-result ${e.result.ok ? "yes" : "no"}">${e.result.ok ? "Accepted" : `Refused: ${esc(e.result.text)}`}</div>` : "";
  box.innerHTML = `<div class="ev-head"><span>Step ${cursor + 1} of ${visible.length}</span><span class="lane">${esc(lane.name)}</span><span>${esc(e.who ?? TYPE_LABEL[e.type] ?? e.type)}</span>${e.car ? `<span>${esc(e.car)}</span>` : ""}<span class="t">t = ${e.t} s</span></div>
    <p class="ev-text">${esc(e.text)}</p>${result}${why ? `<div class="why">${why}</div>` : ""}${code}`;
}

const KPI_ROWS = [
  ["avg_wait", "Average wait", "s", "low"], ["p95_wait", "95th-percentile wait", "s", "low"],
  ["max_wait", "Longest wait", "s", "low"], ["over_60", "Waited more than 60 s", "people", "low"],
  ["avg_journey", "Average journey (wait + ride)", "s", "low"], ["floors_total", "Floors travelled (energy, wear)", "", "low"],
  ["delivered", "Delivered", "", "high"],
];

function renderCompare() {
  const box = $("#compare");
  const cmp = story.comparison;
  box.hidden = !cmp;
  if (!cmp) return;
  const pol = cmp.policies;
  const fmt = (v, unit) => (v === null || v === undefined ? "—" : `${typeof v === "number" ? v.toLocaleString("en-US") : esc(v)}${unit && unit !== "people" ? ` ${unit}` : ""}`);
  const rows = KPI_ROWS.map(([k, label, unit, dir]) => {
    const vals = pol.map((p) => p.kpis[k]);
    const nums = vals.filter((v) => typeof v === "number");
    const best = nums.length ? (dir === "low" ? Math.min(...nums) : Math.max(...nums)) : null;
    const allSame = nums.length && nums.every((v) => v === nums[0]);
    return `<tr><th scope="row">${label}</th>${vals.map((v) => `<td class="${!allSame && v === best ? "best" : ""}">${fmt(v, unit)}${k === "delivered" ? ` of ${pol[0].kpis.people}` : ""}${!allSame && v === best ? '<span class="sr"> (best)</span>' : ""}</td>`).join("")}</tr>`;
  }).join("");
  box.innerHTML = `<h2>How good is this dispatch? <span class="verdict">Whole run, same people, three rules</span></h2>
    <table class="compare-table"><thead><tr><th scope="col"><span class="sr">Measure</span></th>${pol.map((p) => `<th scope="col" title="${esc(p.note)}">${esc(p.label)}</th>`).join("")}</tr></thead>
    <tbody>${rows}</tbody></table>
    <p class="compare-key">Underlined: the best value in the row. The playback above is the SREMS run; the other two rules were run on the same people with the same seed.</p>
    <ul class="findings">${cmp.verdict.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>`;
}

function nextStory() {
  const k = DATA.stories.indexOf(story);
  return DATA.stories[(k + 1) % DATA.stories.length];
}

function renderCaption() {
  const shownIdx = cursor >= 0 ? visible[cursor] : -1;
  if (t >= story.until - 1e-6 && cursor === visible.length - 1) {
    const nxt = nextStory();
    const met = story.criteria.filter((c) => c.passed_at !== null).length;
    const head = met === story.criteria.length
      ? `Accepted. ${esc(story.custom ? story.title : story.vt.join(" and "))}: all ${story.criteria.length} criteria were met by the simulator.`
      : `Not accepted. ${met} of ${story.criteria.length} criteria met; the ones marked ✗ failed in this run.`;
    $("#moment").innerHTML = `<span class="done${met === story.criteria.length ? "" : " fail"}">${head}</span>
      <span class="done-actions"><button type="button" data-act="replay">Replay</button>
      <button type="button" class="next" data-act="next" data-key="${esc(nxt.key)}">Next test: ${esc(nxt.vt[0])}, ${esc(nxt.title)}</button></span>`;
    return;
  }
  const keys = story.events.filter((e) => e.caption);
  let cap = null;
  for (let k = cursor; k >= 0; k -= 1) {
    const ev = story.events[visible[k]];
    if (ev.caption) { cap = ev; break; }
  }
  const n = cap ? keys.indexOf(cap) + 1 : 0;
  const atKey = cap && cap.i === shownIdx;
  $("#moment").innerHTML = cap
    ? `${atKey ? `<span class="rev" title="Key moment ${n} of ${keys.length}"><b>${n}</b></span>` : `<span class="rev past">Key moment ${n} of ${keys.length}</span><br>`}${esc(cap.caption)}`
    : `<span class="hint">Press play. The run stops at each of its ${keys.length} key moments so you can read what happened.</span>`;
}

const TICK = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.2 6.4l2.5 2.4 5-5.4"/></svg>';
const CROSS = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6"/></svg>';

function renderCriteria(animate) {
  let passed = 0, lastAt = 0;
  $("#criteria").innerHTML = story.criteria.map((c, k) => {
    const ok = c.passed_at !== null && c.passed_at <= t + 1e-6;
    const bad = c.passed_at === null && c.failed_at !== null && c.failed_at <= t + 1e-6;
    if (ok) { passed += 1; lastAt = Math.max(lastAt, c.passed_at); }
    const just = ok && animate && !shownCriteria.has(k);
    if (ok) shownCriteria.add(k);
    let when = "", cls = ok ? "pass" : "";
    if (ok) when = c.kind === "always" ? "held for the whole run" : c.kind === "final" ? "checked at the end of the run" : `met at t = ${c.passed_at} s`;
    else if (bad) { when = c.kind === "always" ? `broken at t = ${c.failed_at} s` : "not met at the end of the run"; cls = "fail"; }
    else if (c.kind === "always") { when = "holding so far, checked every second"; cls = "hold"; }
    else if (c.kind === "final") { when = "checked when the run ends"; cls = "hold"; }
    return `<li class="${cls}${just ? " just" : ""}"><span class="box">${ok ? TICK : bad ? CROSS : ""}</span>
      <span><span class="sr">${ok ? "Met: " : bad ? "Failed: " : "Not met yet: "}</span>${esc(c.text)}<br><span class="ref">${esc(c.ref)}</span>${when ? `, <span class="when">${when}</span>` : ""}</span></li>`;
  }).join("");
  for (let k = 0; k < story.criteria.length; k += 1) {
    const c = story.criteria[k];
    if (!(c.passed_at !== null && c.passed_at <= t + 1e-6)) shownCriteria.delete(k);
  }
  const all = passed === story.criteria.length;
  const failed = story.criteria.filter((c) => c.passed_at === null && c.failed_at !== null && c.failed_at <= t + 1e-6).length;
  const v = $("#verdict");
  v.className = `verdict${all ? " pass" : failed ? " fail" : ""}`;
  v.textContent = all ? "Accepted" : failed ? `${failed} failed` : `${passed} of ${story.criteria.length} met`;
  const when = new Date(DATA.generatedAt).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  $("#signoff").innerHTML = all
    ? `<b>Accepted</b> at t = ${lastAt} s. Checked by the simulator (<code>simulation/operational.py</code>), recorded ${esc(when)}.`
    : t >= story.until - 1e-6 && failed
      ? `<b class="no">Not accepted</b>: ${failed} of ${story.criteria.length} criteria failed in this run.`
      : `Awaiting ${story.criteria.length - passed} of ${story.criteria.length} criteria.`;
}

function renderReadings() {
  const f = frameAt(Math.floor(t));
  const [served, mean, p95, active] = f.m;
  const open = f.i.length;
  const cells = [
    ["Hall calls served", served],
    ["Average wait per hall call", mean === null ? "—" : `${mean}<small> s</small>`],
    ["95th percentile per call", p95 === null ? "—" : `${p95}<small> s</small>`],
    ["Active requests", active],
    ["Open incidents", open],
    ["People in the building", f.p.filter((p) => p[1] !== "x").length],
  ];
  $("#readings").innerHTML = cells.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
  const start = frameAt(0);
  const wear = story.key === "wear";
  const vals = DATA.cars.map((id, ci) => ({ id, v: wear ? f.c[ci][6] : f.c[ci][6] - start.c[ci][6] }));
  const max = Math.max(1, ...vals.map((x) => (wear ? x.v - Math.min(...vals.slice(0, 4).map((y) => y.v)) + 1 : x.v)));
  $("#trips").innerHTML = vals.map(({ id, v }) => {
    const w = wear ? ((v - Math.min(...vals.slice(0, 4).map((y) => y.v)) + 1) / max) * 100 : (v / max) * 100;
    return `<div class="trip"><span>${id}</span><span class="bar"><b style="width:${clamp(w, 0, 100)}%"></b></span><span>${wear ? v.toLocaleString("en-US") : `+${v}`}</span></div>`;
  }).join("");
  $("#trips-title").textContent = wear ? "Floors this month (bar: lead over the least-used car)" : "Floors travelled in this run";
}

function renderLog() {
  const items = [];
  for (let k = cursor; k >= 0 && items.length < 9; k -= 1) items.push(story.events[visible[k]]);
  $("#log").innerHTML = items.length ? items.map((e, n) => {
    const lane = laneOf(e);
    return `<li class="${n === 0 ? "cur" : ""}" style="--c:${lane.c}" data-ev="${e.i}"><span class="tt">${e.t} s</span><span class="ln">${esc(lane.name.split(" ")[0])}</span><span>${esc(e.text)}</span></li>`;
  }).join("") : '<li class="empty">Nothing yet.</li>';
}

function renderAll(animate = false) {
  const e = cursor >= 0 ? story.events[visible[cursor]] : null;
  renderCaption();
  renderBus(e, animate);
  renderEventCard(e);
  renderCriteria(animate);
  renderReadings();
  renderLog();
  renderTower();
}

// ------------------------------------------------------------------------------- story selection
function computeVisible() {
  const minor = $("#show-minor").checked;
  const current = cursor >= 0 ? visible[cursor] : -1;
  visible = [];
  story.events.forEach((e, k) => { if (minor || !e.minor) visible.push(k); });
  cursor = -1;
  for (let k = 0; k < visible.length; k += 1) if (visible[k] <= current) cursor = k;
  buildMarks();
}

function buildMarks() {
  const until = Math.max(1, story.until);
  const ev = visible.map((k) => story.events[k]).map((e) => {
    const left = (e.t / until) * 100;
    return e.caption ? `<i class="key" style="left:${left}%" title="${esc(e.caption)}"></i>` : `<i style="left:${left}%;--c:${laneOf(e).c}"></i>`;
  });
  const passes = story.criteria.filter((c) => c.passed_at !== null && c.kind !== "always")
    .map((c) => `<i class="pass" style="left:${(c.passed_at / until) * 100}%" title="${esc(c.text)}"></i>`);
  $("#marks").innerHTML = ev.join("") + passes.join("");
}

function selectStory(key, { keepTime = false } = {}) {
  pause();
  story = DATA.stories.find((s) => s.key === key) ?? DATA.stories[0];
  try { history.replaceState(null, "", `#${story.key}`); } catch { /* file:// */ }
  const tag = story.custom ? "Your test" : story.vt.join(", ");
  document.title = `${tag} ${story.title}: SREMS validation tests`;
  $("#cur-vt").textContent = tag;
  $("#tb-test").textContent = story.custom ? story.title : tag;
  $("#edit-test").hidden = !story.custom;
  renderCompare();
  $("#cur-title").textContent = story.title;
  $("#tagline").textContent = story.tagline + (story.note ? ` ${story.note}` : "");
  $("#reqs").innerHTML = `<b>Requirements covered:</b> ${story.requirements.map(esc).join(", ")}`;
  $("#scrub").max = story.until;
  $("#t-end").textContent = `${story.until} s`;
  $("#source").textContent = story.custom
    ? `Your scenario, recorded from the real simulator${story.recordedAt ? `, ${new Date(story.recordedAt).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` : ""}`
    : story.liveRecorded
    ? `Recorded just now by the running backend`
    : `Recorded from the real simulator, ${new Date(DATA.generatedAt).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}`;
  if (!keepTime) { t = 0; cursor = -1; }
  shownCriteria = new Set();
  computeVisible();
  renderAll(false);
  $$("#picker-list tr.row").forEach((r) => r.setAttribute("aria-current", String(r.dataset.key === story.key)));
}

function buildPicker() {
  const GROUP = {
    "Mode 1": "Mode 1: normal operation",
    "Instructor NFR": "Non-functional: balanced wear and performance",
    Performance: "Non-functional: balanced wear and performance",
    "Mode 3": "Mode 3: emergency and safety",
    "Mode 4": "Mode 4: maintenance and reliability",
  };
  const groups = new Map();
  for (const s of DATA.stories) {
    const raw = s.mode.split(" · ")[0];
    const g = s.custom ? "Your tests" : GROUP[raw] ?? raw;
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(s);
  }
  $("#picker-list").innerHTML = [...groups.entries()].map(([g, list]) => `<tr class="group"><th colspan="5" scope="colgroup">${esc(g)}</th></tr>${list.map((s) => {
    const n = DATA.stories.indexOf(s) + 1;
    const met = s.criteria.filter((c) => c.passed_at !== null).length;
    return `<tr class="row" tabindex="0" data-key="${esc(s.key)}" aria-label="Run ${esc(s.vt.join(", "))}, ${esc(s.title)}">
      <td class="vt">${esc(s.custom ? "Yours" : s.vt.join(", "))}</td>
      <td><span class="name">${esc(s.title)}</span><span class="desc">${esc(s.tagline)}</span></td>
      <td class="num ${met === s.criteria.length ? "ok" : "no"}">${met} of ${s.criteria.length}</td>
      <td class="num len">${s.until} s</td>
      <td class="num">${n <= 9 ? `<kbd>${n}</kbd>` : ""}</td></tr>`;
  }).join("")}`).join("");
}

function openPicker() {
  $("#picker").hidden = false;
  $(".register").scrollTop = 0;
  ($("#picker-list tr.row[aria-current='true']") ?? $("#picker-list tr.row"))?.focus({ preventScroll: true });
}
function closePicker(focusPlay = false) { $("#picker").hidden = true; (focusPlay ? $("#play") : $("#open-picker")).focus(); }

// ------------------------------------------------------------------------------- playback
function setPlayIcon() {
  $("#play-icon").innerHTML = playing ? '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>' : '<path d="M7 5v14l12-7z"/>';
  $("#play").setAttribute("aria-label", playing ? "Pause" : "Play");
}

function play() {
  if (!story) return;
  if (t >= story.until - 1e-6) { t = 0; cursor = -1; shownCriteria = new Set(); }
  playing = true;
  holdUntil = 0;
  lastFrameAt = performance.now();
  setPlayIcon();
  requestAnimationFrame(loop);
}

function pause() { playing = false; setPlayIcon(); }

// Steps where a car arrives are shown once its doors have slid open (0.6 s after it stops),
// so the drawing matches the words "doors open".
const OPENS_DOORS = new Set(["Arrival", "ResponderArrival", "RescueArrival"]);
const evTime = (e) => Math.min(story.until, e.t + (OPENS_DOORS.has(e.type) ? 0.62 : 0));

function holdFor(e) {
  if (e.minor) return 0;
  return (e.caption ? 900 : 1050) / Math.sqrt(speed);
}

function loop(now) {
  if (!playing) return;
  const dt = Math.min(0.1, (now - lastFrameAt) / 1000);
  lastFrameAt = now;
  if (now < holdUntil) { renderTower(); requestAnimationFrame(loop); return; }
  const nextK = cursor + 1 < visible.length ? visible[cursor + 1] : null;
  const nextE = nextK === null ? null : story.events[nextK];
  if (nextE && evTime(nextE) <= t + 1e-9) {
    cursor += 1;
    renderAll(true);
    holdUntil = now + holdFor(nextE);
    if (nextE.caption && nextE.pause && $("#autopause").checked) { pause(); return; }
    requestAnimationFrame(loop);
    return;
  }
  const quiet = (!nextE || evTime(nextE) - t > 3) && !carMoving(t);
  const rate = speed * (quiet ? 6 : 1);
  const limit = nextE ? evTime(nextE) : story.until;
  t = Math.min(limit, t + dt * rate);
  if (!nextE && t >= story.until - 1e-9) {
    t = story.until;
    renderAll(true);
    pause();
    return;
  }
  renderTower();
  if (Math.floor(t) !== Math.floor(t - dt * rate)) { renderCriteria(true); renderReadings(); }
  requestAnimationFrame(loop);
}

function step(delta) {
  pause();
  cursor = clamp(cursor + delta, -1, visible.length - 1);
  t = cursor >= 0 ? evTime(story.events[visible[cursor]]) : 0;
  renderAll(delta > 0);
}

function seek(time) {
  pause();
  t = clamp(Number(time), 0, story.until);
  cursor = -1;
  for (let k = 0; k < visible.length; k += 1) if (evTime(story.events[visible[k]]) <= t + 1e-9) cursor = k;
  renderAll(false);
}

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 4000);
}

async function rerun() {
  if (story.custom && story.spec) { runCustom(story.spec); return; }
  const key = story.key;
  $("#rerun").disabled = true;
  try {
    const res = await fetch(`/api/v1/operational/stories/${encodeURIComponent(key)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const fresh = await res.json();
    fresh.liveRecorded = true;
    DATA.stories = DATA.stories.map((s) => (s.key === key ? fresh : s));
    selectStory(key);
    toast(`${fresh.vt.join(", ")} recorded again on the backend: ${fresh.criteria.filter((c) => c.passed_at !== null).length} of ${fresh.criteria.length} criteria met.`);
  } catch (err) {
    toast("The backend is not reachable from this page. Start it with start.ps1, then open the player from the dashboard.");
  } finally {
    $("#rerun").disabled = false;
  }
}

// ------------------------------------------------------------------------------- wiring
function init() {
  if (!DATA) {
    document.body.innerHTML = "<p style='padding:2rem'>stories.js is missing. Run <code>python -m simulation.story_recorder</code>.</p>";
    return;
  }
  for (const c of window.SREMS_CUSTOM ?? []) {
    if (!DATA.stories.some((x) => x.key === c.key)) DATA.stories.push({ ...c, custom: true });
  }
  buildTower();
  buildBus();
  buildPicker();
  if (location.protocol === "file:") { $("#rerun").disabled = true; $("#rerun").title = "Open the player from the dashboard to re-run scenarios on the backend"; }
  $("#play").addEventListener("click", () => (playing ? pause() : play()));
  $("#next").addEventListener("click", () => step(1));
  $("#prev").addEventListener("click", () => step(-1));
  $("#speed").addEventListener("change", (ev) => { speed = Number(ev.target.value); });
  $("#scrub").addEventListener("input", (ev) => seek(ev.target.value));
  $("#show-minor").addEventListener("change", () => { computeVisible(); renderAll(false); });
  $("#open-picker").addEventListener("click", openPicker);
  $("#close-picker").addEventListener("click", closePicker);
  const runRow = (row) => { selectStory(row.dataset.key); closePicker(true); play(); };
  $("#picker").addEventListener("click", (ev) => {
    const row = ev.target.closest("tr.row");
    if (row) runRow(row);
    else if (ev.target.id === "picker") closePicker();
  });
  $("#picker").addEventListener("keydown", (ev) => {
    const row = ev.target.closest("tr.row");
    if (row && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); runRow(row); }
    else if (row && (ev.key === "ArrowDown" || ev.key === "ArrowUp")) {
      ev.preventDefault();
      const rows = $$("#picker-list tr.row");
      rows[clamp(rows.indexOf(row) + (ev.key === "ArrowDown" ? 1 : -1), 0, rows.length - 1)].focus();
    }
  });
  $("#rerun").addEventListener("click", rerun);
  $("#moment").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-act]");
    if (!b) return;
    if (b.dataset.act === "replay") { seek(0); play(); }
    else { selectStory(b.dataset.key); play(); }
  });
  $("#log").addEventListener("click", (ev) => {
    const li = ev.target.closest("li[data-ev]");
    if (!li) return;
    pause();
    const k = visible.indexOf(Number(li.dataset.ev));
    if (k >= 0) { cursor = k; t = evTime(story.events[visible[k]]); renderAll(false); }
  });
  window.addEventListener("resize", () => renderBus(null, false));
  document.addEventListener("keydown", (ev) => {
    if (!$("#builder").hidden) return;   // the builder handles its own keys
    if (!$("#picker").hidden) {
      if (ev.key === "Escape") closePicker();
      return;
    }
    if (ev.target.closest("input[type=range], select, input[type=checkbox]") && ev.key !== " ") return;
    if (ev.target.closest("button, a, select") && (ev.key === " " || ev.key === "Enter")) return;
    if (ev.key === " ") { ev.preventDefault(); playing ? pause() : play(); }
    else if (ev.key === "ArrowRight") { ev.preventDefault(); step(1); }
    else if (ev.key === "ArrowLeft") { ev.preventDefault(); step(-1); }
    else if (ev.key === "s" || ev.key === "/") { ev.preventDefault(); openPicker(); }
    else if (/^[1-9]$/.test(ev.key) && DATA.stories[Number(ev.key) - 1]) selectStory(DATA.stories[Number(ev.key) - 1].key);
  });
  window.addEventListener("hashchange", () => {
    const key = location.hash.slice(1);
    if (key && key !== story?.key) selectStory(key);
  });
  const first = location.hash.slice(1);
  selectStory(first || DATA.stories[0].key);
  if (!first) openPicker();
}

init();
