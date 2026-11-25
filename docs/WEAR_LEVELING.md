# Wear-and-tear balancing (instructor NFR, project phase 2)

> "Your elevator controller algorithm [shall] balance the wear and tear of each unit as evenly
> as practical. The difference between the distances (e.g., number of floors) accumulated by
> all units [shall] be kept under 2 % a month (between the minimum and maximum distances) and
> at most 5 % in 6 months. 'Trip counter' will be reset every 6 months."
> — Prof. Sungdeok Cha, 17 Nov 2026

This document records how SREMS meets that requirement, the evidence, and what still needs
the instructor's confirmation. Code: `simulation/wear.py`, `simulation/operational.py`
(`_assign_request`, `_advance_locked`), `simulation/wear_benchmark.py`, `api/wear.py`,
`frontend/src/Wear.tsx`. Tests: `tests/test_wear_leveling.py`.

## 1. Requirements as implemented (proposed SRS text: WEAR-1 .. WEAR-6)

| ID | Requirement | Where |
|---|---|---|
| WEAR-1 | **Trip counters.** For each car SREMS counts floors travelled (one per floor passed; B1↔1 is one floor), motor starts and door cycles, for the current calendar month, the current 6-month period and the car's lifetime. Counters are persisted and survive a restart. | `WearTracker.record_*`, `_advance_locked`, `_serve_stop`, `elevator_wear_counters` |
| WEAR-2 | **Monthly balance.** For the residential group E1–E4, `(max − min) / max` of the floors travelled in each calendar month shall be **< 2 %**. | `WearTracker._window_rows` |
| WEAR-3 | **Balancing dispatch** (amends FR-1.4/FR-1.5). Candidate cost = predicted service time + usage penalty; ties → fewer floors this month → lower ID. | `_assign_request`, `WearTracker.usage_penalties` |
| WEAR-4 | **Month close.** At 00:00 UTC on the 1st, SREMS closes the month (per-car floors, spread, verdict), resets month counters and logs `WEAR_WINDOW_CLOSED` or `WEAR_LIMIT_EXCEEDED`. | `_roll_wear_windows` |
| WEAR-5 | **6-month period.** Spread over each 6-month period shall be **≤ 5 %**; at period end the result is recorded and the period trip counters are **reset** (`TRIP_COUNTER_RESET`). Lifetime counters are never reset. | `WearTracker.advance_to` |
| WEAR-6 | **Visibility.** Admin and Technician see live counters, month/period spread and closed windows; Admin can run an accelerated multi-month proof run. | `GET /api/v1/operational/wear`, Wear & tear page |

### Interpretation choices (to confirm, see OR-21)

1. **Distance = floors travelled** (the instructor's example). Starts and door cycles are recorded too because
   they drive brake, rope-bending and door wear, but they are not part of the pass/fail test.
2. **Spread = (max − min) / max.** Dividing by the maximum is the reading "difference between the minimum and
   maximum distances"; the benchmark results below are far inside the limit under any denominator.
3. **"All units" = the residential dispatch group E1–E4.** E5 is the only car that may serve freight calls and
   may not serve ordinary hall calls (FR-1.4, FR-1.13). Its distance depends only on freight demand, so it
   cannot be balanced against E1–E4 without breaking FR-1.13. E5 is counted and reported as its own group.
4. **Month = calendar month (UTC); period = 6 calendar months** starting from the month the counters were
   first created (commissioning), stored in the database so a restart does not move it.
5. The verdict is taken on **closed** windows. Early in a month a few trips move the month-to-date spread a
   lot; the dashboard labels that value "on track / above limit now" rather than pass/fail.

## 2. Algorithm

For a hall call, every eligible car `i` of the call's group gets

```
cost_i    = ETA_i + penalty_i
penalty_i = min( P_max ,  G · (d_i − d_min) / (L · max(d_max, R)) )
```

* `ETA_i` — predicted service time from FR-1.4 (route walked leg by leg, 1 s per floor, 2 s per stop).
* `d_i` — floors travelled by car `i` this month; `d_min`, `d_max` over the **eligible** candidates of the group.
* `L` = monthly limit (2 %), `G` = 60 s gain, `P_max` = 15 s cap, `R` = 3,000 floors (noise floor early in a month).

A car that is a full monthly limit ahead of the least-used car pays `G` seconds; the cap guarantees that
balancing never delays a passenger by more than 15 s compared with the fastest car, which protects PERF-9.
**Busy rule:** the penalty is paused while the group has more active hall calls than eligible cars. In a rush
every car is needed; steering calls to the least-used car only filled it (it then had to pass pickups) and
raised waits. Measured with `python -m simulation.dispatch_benchmark` (55 scenarios, 5 seeds) and a 6-month
`wear_benchmark` with outages: see "Dispatch tuning" below.
A car coming back from maintenance has the lowest counter, so it is preferred until it catches up; cars out
of service are not used as the reference, so they do not push penalties to the cap for everyone else.
Balancing uses the **month** counter only; month-level balance implies period balance (next section).

We deliberately did **not** move idle cars just to add distance ("levelling runs"): it wastes energy and
creates wear that serves nobody, which is the opposite of the requirement's intent.

Also new in dispatch (needed for realistic distances): car calls are served in travel order (collective
control) instead of first-come-first-served, and hall calls are inserted on any committed leg that passes the
floor in the requested direction, not just the first leg. This removes zig-zag travel.

### Why monthly balance implies the 6-month limit

Let `M_{i,m}` be car `i`'s floors in month `m` and `P_i = Σ_m M_{i,m}`. If every month satisfies
`max_m − min_m < 0.02 · max_m`, then

```
max_i P_i − min_j P_j  ≤  Σ_m (max_m − min_m)  <  0.02 · Σ_m max_m
max_i P_i              ≥  Σ_m min_m            >  0.98 · Σ_m max_m
⇒ period spread < 0.02 / 0.98 ≈ 2.04 %  ≤ 5 %
```

So WEAR-2 holding every month is sufficient for WEAR-5; the 5 % limit only bites when a month fails.

## 3. Evidence (python -m simulation.wear_benchmark)

Traffic: 1,500 residents (600 flats × 2.5, SRS CAP-1), ≈ 5,800 journeys per weekday with a 07–09 down-peak and
17–20 up-peak, full journeys (hall call → boarding → destination), 25 % of lobby trips to B1, 3 % freight on
E5. Seeded (BEN-2). "Outages" = one 8-hour maintenance visit per car per month plus one 48-hour breakdown.

| Run | Months | Outages | Worst monthly spread E1–E4 | 6-month spread | Peak wait mean / p95 |
|---|---|---|---|---|---|
| Original dispatcher (balancing OFF) | 1 | no | **24.86 %** ✗ | – | 8.8 s / 25 s |
| Wear-balancing ON | 1 | no | **0.037 %** ✓ | – | 8.9 s / 25 s |
| Original dispatcher (balancing OFF) | 1 | yes | **23.04 %** ✗ | – | 8.9 s / 25 s |
| Wear-balancing ON | 1 | yes | **0.038 %** ✓ | – | 9.8 s / 28 s |
| Original dispatcher (balancing OFF) | 6 | yes | **24.13 %** ✗ (every month 17–24 %) | **22.41 %** ✗ | 8.9 s / 25 s |
| Wear-balancing ON, first version (30 s cap) | 6 | yes | **0.038 %** ✓ (all 6 months) | **0.009 %** ✓ | 9.3 s / 26 s |
| **Wear-balancing ON, current (busy rule, 15 s cap)** | 6 | yes | **0.037 %** ✓ (all 6 months) | **0.011 %** ✓ | 9.0 s / 25 s |

The full 6-month JSON (current settings and balancing OFF, per-month rows) is in `docs/wear_report.json`.
How the current settings were chosen, with passenger figures over 110 scenarios: `docs/DISPATCH_TUNING.md`. With the original tie-break
(E1 before E2 …) E1 travels about a third more than E4 every month. Wait times are those of the software test
stub (1 floor/s, no acceleration), so only the *difference* between runs is meaningful.

## 4. Tests

| Test | Covers |
|---|---|
| VT-33 `test_balancing_keeps_spread_small_where_baseline_drifts` | 2-day realistic run: ON < 1 %, OFF > 5 %, PERF-9 met, repeatable |
| VT-34 `test_trip_counter_resets_every_six_months`, `test_idle_fast_forward_still_resets_counters_after_six_months` | WEAR-4/5 windows and the 6-month reset |
| VT-35 `test_counters_survive_restart_and_missed_month_is_closed` | WEAR-1/6 persistence; a month that ended while SREMS was down is closed at start-up |
| VT-36 `test_dispatch_prefers_less_travelled_car…`, `test_balancing_never_costs_more_than_the_penalty_cap`, `test_balancing_off_restores_original_lowest_id_tie_break` | WEAR-3 |
| plus | counters per floor/start/door, strict `< 2 %`, calendar maths, collective ordering, incremental DB sync |

## 5. Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SREMS_WEAR_BALANCING` | `1` | `0` reproduces the original dispatcher (for A/B demos) |
| `SREMS_WEAR_MONTHLY_LIMIT_PCT` | `2.0` | WEAR-2 |
| `SREMS_WEAR_PERIOD_LIMIT_PCT` | `5.0` | WEAR-5 |
| `SREMS_WEAR_PERIOD_MONTHS` | `6` | trip-counter reset interval |
| `SREMS_WEAR_GAIN_SECONDS` / `SREMS_WEAR_MAX_PENALTY_SECONDS` | `60` / `30` | penalty gain and cap |
| `SREMS_WEAR_REFERENCE_FLOORS` | `3000` | early-month noise floor |

## 6. Limits and open points (OR-21)

* If a car is out of service for a large part of the **end** of a month, no dispatch rule can make it catch
  up before the month closes (the remaining demand is too small). The month then fails and the event log and
  dashboard say so; the next month starts balanced again. Ask the instructor whether declared maintenance
  time may be excluded, or whether empty levelling runs are acceptable in that case.
* E5 excluded from the E1–E4 comparison (FR-1.13) — confirm.
* Denominator of the 2 % (max vs mean), and UTC vs building-local month boundaries — confirm.

## 7. Demo script (Friday)

1. `.\start.ps1`, sign in with `ADMIN-CARD-0001`, open **Wear & tear**.
2. Show live counters moving while you place hall calls in **Simulator (stub)**.
3. Click **Load shipped 6-month report**: balancing ON keeps every month ≈ 0.04 % and the 6-month period
   ≈ 0.01 %; the original dispatcher is ≈ 24 % every month.
4. Optional live proof: **Start accelerated run** with 1 month (≈ 1.5 min with comparison).
