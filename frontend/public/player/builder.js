// SREMS scenario builder: your own test. You choose the people (how many, from where, to where,
// when), the cars' starting state and any incidents; the backend runs it on the real simulator
// (POST /api/v1/operational/stories/custom) and the result plays here like any other test.
// Spec format: simulation/custom_scenario.py. Without a backend you can save the test as JSON and
// run `python -m simulation.custom_scenario my_test.json --player`.
"use strict";

const PLACES = [
  ["lobby", "Lobby (1)"], ["b1", "B1 parking"], ["random", "Any of 2–30"], ["lower", "Lower 2–15"],
  ["upper", "Upper 16–30"], ["any", "Anywhere, B1–30"], ["own", "Floors I type…"],
];
const INCIDENT_TYPES = [
  ["MEDICAL", "Medical emergency in the car"], ["HELP", "Help button pressed"], ["OBSTRUCTION", "Door blocked"],
  ["MAINTENANCE", "Taken out for maintenance"], ["FAULT", "Drive fault"],
];
const CAR_IDS = ["E1", "E2", "E3", "E4", "E5"];

const PRESETS = [
  { key: "morning", label: "Morning rush", hint: "Most people go down to the lobby or parking", start: "07:45:00", groups: [
    { label: "Leaving for work", count: 45, from: "random", to: "lobby", start: 0, over: 240 },
    { label: "Drivers to parking", count: 15, from: "random", to: "b1", start: 0, over: 240 },
    { label: "Coming back up", count: 6, from: "lobby", to: "random", start: 0, over: 240 }] },
  { key: "evening", label: "Evening return", hint: "Most people come up from the lobby and B1", start: "18:00:00", groups: [
    { label: "Home from work", count: 45, from: "lobby", to: "random", start: 0, over: 240 },
    { label: "Drivers from parking", count: 12, from: "b1", to: "random", start: 0, over: 240 },
    { label: "Going out", count: 8, from: "random", to: "lobby", start: 0, over: 240 }] },
  { key: "lunch", label: "Lunch, two-way", hint: "As many going down as coming up", start: "12:00:00", groups: [
    { label: "Going out", count: 25, from: "random", to: "lobby", start: 0, over: 300 },
    { label: "Coming back", count: 25, from: "lobby", to: "random", start: 60, over: 300 }] },
  { key: "interfloor", label: "Between floors", hint: "Neighbours visiting each other", start: "15:00:00", groups: [
    { label: "Visiting", count: 30, from: "random", to: "random", start: 0, over: 300 }] },
  { key: "movein", label: "Move-in day", hint: "Goods on E5 while residents travel", start: "10:00:00", groups: [
    { label: "Residents", count: 20, from: "random", to: "lobby", start: 0, over: 300 },
    { label: "Residents back", count: 10, from: "lobby", to: "random", start: 0, over: 300 },
    { label: "Furniture", count: 8, from: "lobby", to: "12, 18, 24", start: 0, over: 300, kind: "freight" }] },
  { key: "stress", label: "Stress test", hint: "190 people in 3 minutes and a drive fault", start: "08:00:00", groups: [
    { label: "Everyone leaves", count: 150, from: "random", to: "lobby", start: 0, over: 180 },
    { label: "Against the flow", count: 40, from: "lobby", to: "random", start: 0, over: 180 }],
    incidents: [{ type: "FAULT", car: "E3", at: 60, duration: 60 }] },
];

const B = { groups: [], cars: {}, incidents: [] };
const bq = (s) => document.querySelector(s);

function placeOf(v) {
  if (typeof v === "number") return { place: "own", own: String(v === -1 ? "B1" : v) };
  if (Array.isArray(v)) return { place: "own", own: v.map((x) => (x === -1 ? "B1" : x)).join(", ") };
  const key = String(v).toLowerCase();
  if (PLACES.some(([k]) => k === key)) return { place: key, own: "" };
  return { place: "own", own: String(v) };
}

function placeValue(place, own, where) {
  if (place !== "own") return place;
  const parts = own.split(/[,\s;]+/).filter(Boolean).map((x) => (x.toUpperCase() === "B1" ? -1 : Number(x)));
  if (!parts.length || parts.some((x) => !Number.isInteger(x) || (x !== -1 && (x < 1 || x > 30)))) {
    throw new Error(`${where}: type floors 1 to 30 or B1, separated by commas`);
  }
  return parts.length === 1 ? parts[0] : parts;
}

function defaults() {
  return {
    name: "My test", start: "08:00:00", seed: 7, targets: { avg_wait: 60, p95_wait: 120 },
    cars: { E1: { floor: 1 }, E2: { floor: 8 }, E3: { floor: 16 }, E4: { floor: 24 }, E5: { floor: 1 } },
    groups: PRESETS[0].groups, incidents: [],
  };
}

function load(spec) {
  const sp = { ...defaults(), ...spec };
  bq("#b-name").value = sp.name;
  const st = String(sp.start).match(/(\d{1,2}):(\d{2})(?::(\d{2}))?/);
  bq("#b-start").value = st ? `${st[1].padStart(2, "0")}:${st[2]}:${st[3] ?? "00"}` : "08:00:00";
  bq("#b-seed").value = sp.seed ?? 7;
  bq("#b-avg").value = sp.targets?.avg_wait ?? 60;
  bq("#b-p95").value = sp.targets?.p95_wait ?? 120;
  B.groups = (sp.groups ?? []).map((g) => ({
    label: g.label ?? "", count: g.count ?? 10, start: g.start ?? 0, over: g.over ?? 0, kind: g.kind ?? "resident",
    from: placeOf(g.from ?? "random"), to: placeOf(g.to ?? "lobby"),
  }));
  B.cars = {};
  for (const id of CAR_IDS) {
    const c = sp.cars?.[id] ?? {};
    B.cars[id] = { floor: c.floor ?? defaults().cars[id].floor, state: c.state ?? "IN_SERVICE",
      floors_month: c.floors_month ?? (id === "E5" ? 78000 : 465000) };
  }
  B.incidents = (sp.incidents ?? []).map((i) => ({ type: i.type, car: i.car, at: i.at ?? 30, duration: i.duration ?? 60 }));
  renderGroups(); renderCars(); renderIncidents(); setStatus("");
}

function opt(list, value) {
  return list.map(([k, label]) => `<option value="${esc(k)}"${k === value ? " selected" : ""}>${esc(label)}</option>`).join("");
}

function placeCell(g, side, k) {
  const p = g[side];
  return `<td class="place"><select data-g="${k}" data-f="${side}.place" aria-label="${side === "from" ? "Starting floor" : "Destination"}, group ${k + 1}">${opt(PLACES, p.place)}</select>
    <input data-g="${k}" data-f="${side}.own" value="${esc(p.own)}" placeholder="12, 18, 25" aria-label="Floors, group ${k + 1}"${p.place === "own" ? "" : " hidden"}></td>`;
}

function renderGroups() {
  bq("#b-groups tbody").innerHTML = B.groups.map((g, k) => `<tr>
      <td><input data-g="${k}" data-f="label" value="${esc(g.label)}" placeholder="Residents" aria-label="Name of group ${k + 1}"></td>
      <td class="num"><input data-g="${k}" data-f="count" type="number" min="1" max="600" value="${g.count}" aria-label="People in group ${k + 1}"></td>
      ${placeCell(g, "from", k)}${placeCell(g, "to", k)}
      <td class="num"><span class="unit"><input data-g="${k}" data-f="start" type="number" min="0" max="3600" value="${g.start}" aria-label="First person appears at, seconds"> s</span></td>
      <td class="num"><span class="unit"><input data-g="${k}" data-f="over" type="number" min="0" max="3600" value="${g.over}" aria-label="Spread over, seconds"> s</span></td>
      <td><select data-g="${k}" data-f="kind" aria-label="What group ${k + 1} carries">${opt([["resident", "Nothing (resident)"], ["freight", "Goods, uses E5"]], g.kind)}</select></td>
      <td><button type="button" class="remove" data-rm-g="${k}" aria-label="Remove group ${k + 1}">Remove</button></td></tr>`).join("");
  const total = B.groups.reduce((n, g) => n + (Number(g.count) || 0), 0);
  bq("#b-total").textContent = `${total} ${total === 1 ? "person" : "people"} in ${B.groups.length} ${B.groups.length === 1 ? "group" : "groups"}`;
}

function renderCars() {
  const floors = [[-1, "B1"], ...Array.from({ length: 30 }, (_, k) => [k + 1, String(k + 1)])];
  bq("#b-cars tbody").innerHTML = CAR_IDS.map((id) => {
    const c = B.cars[id];
    const fl = floors.filter(([f]) => !(id === "E5" && f === -1)).map(([f, l]) => `<option value="${f}"${f === Number(c.floor) ? " selected" : ""}>${l}</option>`).join("");
    return `<tr><th scope="row">${id}${id === "E5" ? " <small>freight</small>" : ""}</th>
      <td><select data-c="${id}" data-f="floor" aria-label="${id} starting floor">${fl}</select></td>
      <td><select data-c="${id}" data-f="state" aria-label="${id} state">${opt([["IN_SERVICE", "In service"], ["MAINTENANCE", "In maintenance"]], c.state)}</select></td>
      <td class="num"><input data-c="${id}" data-f="floors_month" type="number" min="0" step="1000" value="${c.floors_month}" aria-label="${id} floors travelled this month"></td></tr>`;
  }).join("");
}

function renderIncidents() {
  const body = bq("#b-incidents tbody");
  body.innerHTML = B.incidents.length ? B.incidents.map((i, k) => `<tr>
      <td><select data-i="${k}" data-f="type" aria-label="Incident ${k + 1}">${opt(INCIDENT_TYPES, i.type)}</select></td>
      <td><select data-i="${k}" data-f="car" aria-label="Car for incident ${k + 1}">${opt(CAR_IDS.map((c) => [c, c]), i.car)}</select></td>
      <td class="num"><span class="unit"><input data-i="${k}" data-f="at" type="number" min="0" max="3600" value="${i.at}" aria-label="Starts at, seconds"> s</span></td>
      <td class="num"><span class="unit"><input data-i="${k}" data-f="duration" type="number" min="5" max="3600" value="${i.duration}" aria-label="Lasts, seconds"> s</span></td>
      <td><button type="button" class="remove" data-rm-i="${k}" aria-label="Remove incident ${k + 1}">Remove</button></td></tr>`).join("")
    : `<tr class="none"><td colspan="5">No incidents: normal operation only.</td></tr>`;
}

function onEdit(ev) {
  const el = ev.target;
  const f = el.dataset.f;
  if (!f) return;
  const num = el.type === "number";
  const val = num ? (el.value === "" ? "" : Number(el.value)) : el.value;
  if (el.dataset.g !== undefined) {
    const g = B.groups[Number(el.dataset.g)];
    if (f.includes(".")) {
      const [side, part] = f.split(".");
      g[side][part] = val;
      if (part === "place") {
        el.parentElement.querySelector("input").hidden = val !== "own";
        if (val === "own") el.parentElement.querySelector("input").focus();
      }
    } else g[f] = val;
    if (f === "count") {
      const total = B.groups.reduce((n, x) => n + (Number(x.count) || 0), 0);
      bq("#b-total").textContent = `${total} ${total === 1 ? "person" : "people"} in ${B.groups.length} ${B.groups.length === 1 ? "group" : "groups"}`;
    }
  } else if (el.dataset.c) {
    B.cars[el.dataset.c][f] = f === "floor" ? Number(val) : val;
  } else if (el.dataset.i !== undefined) {
    B.incidents[Number(el.dataset.i)][f] = val;
  }
}

function collect() {
  const need = (v, where) => {
    if (v === "" || v === null || Number.isNaN(v)) throw new Error(`${where}: fill this in`);
    return v;
  };
  if (!B.groups.length) throw new Error("People: add at least one group");
  const spec = {
    name: bq("#b-name").value.trim() || "My test",
    start: bq("#b-start").value || "08:00:00",
    seed: need(Number(bq("#b-seed").value), "Random seed"),
    targets: { avg_wait: need(Number(bq("#b-avg").value), "Target average wait"), p95_wait: need(Number(bq("#b-p95").value), "Target 95th percentile") },
    cars: Object.fromEntries(CAR_IDS.map((id) => [id, { floor: B.cars[id].floor, state: B.cars[id].state, floors_month: need(B.cars[id].floors_month, `${id} floors this month`) }])),
    groups: B.groups.map((g, k) => {
      const where = `Group ${k + 1}${g.label ? ` (${g.label})` : ""}`;
      return {
        label: g.label, count: need(g.count, `${where}, how many`), kind: g.kind,
        from: placeValue(g.from.place, g.from.own, `${where}, from`), to: placeValue(g.to.place, g.to.own, `${where}, to`),
        start: need(g.start, `${where}, first at`), over: need(g.over, `${where}, spread over`),
      };
    }),
    incidents: B.incidents.map((i, k) => ({ type: i.type, car: i.car, at: need(i.at, `Incident ${k + 1}, at`), duration: need(i.duration, `Incident ${k + 1}, lasts`) })),
  };
  const total = spec.groups.reduce((n, g) => n + g.count, 0);
  if (total > 600) throw new Error(`People: ${total} in total; at most 600`);
  return spec;
}

function setStatus(text, kind = "") {
  const el = bq("#b-status");
  el.textContent = text;
  el.className = `b-status ${kind}`;
}

function openBuilder(spec) {
  load(spec ?? (() => { try { return JSON.parse(localStorage.getItem("srems.lastTest")); } catch { return null; } })() ?? defaults());
  bq("#builder").hidden = false;
  bq(".builder").scrollTop = 0;
  bq("#b-name").focus({ preventScroll: true });
}
function closeBuilder() { bq("#builder").hidden = true; bq("#new-test").focus(); }

async function runCustom(spec) {
  const button = bq("#b-run");
  button.disabled = true;
  $("#rerun").disabled = true;
  const people = spec.groups.reduce((n, g) => n + g.count, 0);
  setStatus(`Running ${people} people on the simulator, then the same people under two other dispatch rules…`, "busy");
  toast("Running your test on the simulator…");
  try { localStorage.setItem("srems.lastTest", JSON.stringify(spec)); } catch { /* storage unavailable */ }
  try {
    if (location.protocol === "file:") throw new TypeError("offline");
    const res = await fetch("/api/v1/operational/stories/custom", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(spec),
    });
    if (res.status === 422) {
      const body = await res.json().catch(() => ({}));
      throw new Error(`The backend did not accept this test: ${body.detail ?? "invalid scenario"}`);
    }
    if (!res.ok) throw new TypeError(`HTTP ${res.status}`);
    const rec = await res.json();
    rec.custom = true;
    DATA.stories = DATA.stories.filter((s) => s.key !== rec.key).concat(rec);
    buildPicker();
    closeBuilder();
    selectStory(rec.key);
    const met = rec.criteria.filter((c) => c.passed_at !== null).length;
    toast(`${rec.title}: ${met} of ${rec.criteria.length} criteria met. Comparison with the other dispatch rules is in the right-hand column.`);
    play();
  } catch (err) {
    if (err instanceof TypeError) {
      setStatus("Running a test needs the SREMS backend. Start it with start.ps1 and open the player from the dashboard; or save the test file and run: python -m simulation.custom_scenario my_test.json --player", "warn");
      if (bq("#builder").hidden) toast("The backend is not reachable from this page. Start it with start.ps1, then open the player from the dashboard.");
    } else {
      setStatus(err.message, "error");
    }
  } finally {
    button.disabled = false;
    $("#rerun").disabled = location.protocol === "file:";
  }
}

function initBuilder() {
  bq("#b-presets").innerHTML = PRESETS.map((p) => `<button type="button" class="preset" data-preset="${p.key}"><b>${esc(p.label)}</b><span>${esc(p.hint)}</span></button>`).join("");
  bq("#builder-form").addEventListener("input", onEdit);
  bq("#builder-form").addEventListener("change", onEdit);
  bq("#builder-form").addEventListener("click", (ev) => {
    const t = ev.target.closest("button");
    if (!t) return;
    if (t.dataset.close !== undefined) closeBuilder();
    else if (t.dataset.preset) {
      const p = PRESETS.find((x) => x.key === t.dataset.preset);
      const cur = (() => { try { return collect(); } catch { return defaults(); } })();
      load({ ...cur, name: p.label, start: p.start, groups: p.groups, incidents: p.incidents ?? [] });
      setStatus(`${p.label} loaded. Change any number, then run.`);
    } else if (t.dataset.rmG !== undefined) { B.groups.splice(Number(t.dataset.rmG), 1); renderGroups(); }
    else if (t.dataset.rmI !== undefined) { B.incidents.splice(Number(t.dataset.rmI), 1); renderIncidents(); }
  });
  bq("#b-add-group").addEventListener("click", () => {
    B.groups.push({ label: "", count: 10, start: 0, over: 120, kind: "resident", from: placeOf("random"), to: placeOf("lobby") });
    renderGroups();
    bq(`#b-groups tbody tr:last-child input`).focus();
  });
  bq("#b-add-incident").addEventListener("click", () => {
    B.incidents.push({ type: "MEDICAL", car: "E2", at: 60, duration: 60 });
    renderIncidents();
  });
  bq("#builder-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    try { runCustom(collect()); } catch (err) { setStatus(err.message, "error"); }
  });
  bq("#b-download").addEventListener("click", () => {
    try {
      const spec = collect();
      const url = URL.createObjectURL(new Blob([JSON.stringify(spec, null, 2)], { type: "application/json" }));
      const a = Object.assign(document.createElement("a"), { href: url, download: `${spec.name.replace(/[^\w-]+/g, "_").toLowerCase() || "my_test"}.json` });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setStatus("Saved. To run it without the dashboard: python -m simulation.custom_scenario <file> --player");
    } catch (err) { setStatus(err.message, "error"); }
  });
  bq("#b-load").addEventListener("change", async (ev) => {
    const file = ev.target.files?.[0];
    if (!file) return;
    try { load(JSON.parse(await file.text())); setStatus(`Loaded ${file.name}.`); }
    catch { setStatus(`${file.name} is not a test file (JSON).`, "error"); }
    ev.target.value = "";
  });
  bq("#new-test").addEventListener("click", () => openBuilder());
  $("#edit-test").addEventListener("click", () => openBuilder(story.spec));
  bq("#builder").addEventListener("click", (ev) => { if (ev.target.id === "builder") closeBuilder(); });
  document.addEventListener("keydown", (ev) => {
    if (!bq("#builder").hidden) {
      if (ev.key === "Escape") { ev.stopImmediatePropagation(); closeBuilder(); }
      return;
    }
    if ((ev.key === "n" || ev.key === "N") && !ev.target.closest("input, select, textarea") && bq("#picker").hidden) {
      ev.preventDefault();
      openBuilder();
    }
  }, true);
}

initBuilder();
