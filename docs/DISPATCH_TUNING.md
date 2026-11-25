# Dispatch tuning: how the current settings were chosen

The question: is SREMS dispatch as good as it can be made, without breaking the instructor's
wear requirement (WEAR-2: E1–E4 within 2 % every month; WEAR-5: within 5 % per 6 months)?

"Optimal" cannot be proven for an online lift-dispatch problem (future calls are unknown), so the
approach is empirical: every candidate change is measured on the same people, across many
scenarios and seeds, and kept only if it helps. Everything below is reproducible:

```powershell
python -m simulation.dispatch_benchmark --seeds 10 --out docs/dispatch_report.json
python -m simulation.wear_benchmark --months 6 --compare --outages --out docs/wear_report.json
```

## 1. What custom scenarios exposed

1. **Livelock with a full car.** A full car left someone behind; the repeated call was assigned
   straight back to the same car (it was standing at that floor), which reopened there forever.
   With one car 9,000 floors behind on its trip counter, only 31 of 60 people were delivered.
   **Fix: full-load bypass (FR-1.14, CAP-2).** When load data exists, a full car takes no new hall
   calls and passes its pickups; they are reassigned with their acceptance time kept (FR-1.18).
2. **Wear balancing hurts in a rush.** In a stress test (190 people in 3 minutes, a drive fault,
   one car 9,000 floors behind) the wear penalty sent the lagging car ten pickups at once; it
   filled up and had to pass them. Average wait 45.3 s against 25.7 s without balancing.
   **Fix: busy rule + 15 s cap (WEAR-3).** The penalty pauses while the group has more active
   hall calls than eligible cars, and is capped at 15 s instead of 30 s.

## 2. Dispatch benchmark (110 scenario runs per variant)

Seven traffic patterns (morning rush, evening return, lunch two-way, between floors, move-in day
with freight, stress test, light night traffic) × 10 seeds, plus four patterns with one car
9,000 floors behind. Same people for every variant. Full journeys (hall call → board → destination).

| Variant | Avg wait | Mean p95 | Worst p95 | People waiting > 60 s | Avg journey |
|---|---|---|---|---|---|
| **Current: busy rule, 15 s cap** | **11.31 s** | **33.0 s** | **124 s** | **463** | **31.2 s** |
| Previous: no busy rule, 30 s cap | 13.67 s | 39.0 s | 143 s | 689 | 34.0 s |
| Busy rule, 30 s cap | 12.52 s | 35.9 s | 124 s | 488 | 32.7 s |
| Busy rule, 20 s cap | 11.77 s | 34.0 s | 135 s | 494 | 31.9 s |
| Busy rule, 10 s cap | 11.17 s | 33.8 s | 146 s | 525 | 31.0 s |
| Busy rule at 0.5 calls per car | 11.48 s | 34.2 s | 136 s | 544 | 31.2 s |
| Busy rule at 2 calls per car | 11.63 s | 34.4 s | 126 s | 537 | 31.4 s |
| Current + overload-risk cost | 11.39 s | 33.9 s | 135 s | 485 | 31.2 s |
| *No wear penalty (reference, fails WEAR-2)* | *10.93 s* | *32.5 s* | *124 s* | *508* | *30.5 s* |
| *Nearest car (naive reference)* | *20.32 s* | *64.0 s* | *215 s* | *1,246* | *40.9 s* |

Against the previous settings: average wait −17 %, mean p95 −15 %, worst p95 −13 %, people waiting
over a minute −33 %. The current settings cost only 0.4 s of average wait against no balancing at
all, and leave fewer people waiting over a minute than no balancing. The 10 s cap is slightly
faster on average but has worse tails (146 s worst p95) and balances half as fast; the
overload-risk cost brought nothing once the busy rule was on, so it stays off. Full figures:
`docs/dispatch_report.json`.

Stress test from section 1, before and after: average wait 45.3 → 25.5 s, p95 147 → 79 s,
people waiting over 60 s 50 → 16.

## 3. The wear requirement still holds (6 months, maintenance outages)

| Run | Worst monthly spread E1–E4 | 6-month spread | Peak wait mean / p95 | All-day mean wait |
|---|---|---|---|---|
| Current settings | 0.037 % ✓ (all 6 months) | 0.011 % ✓ | 9.0 s / 25 s | 7.55 s |
| Previous settings | 0.038 % ✓ | 0.009 % ✓ | 9.3 s / 26 s | 7.73 s |
| No balancing | 24.1 % ✗ | 22.4 % ✗ | 8.9 s / 25 s | 7.42 s |

Balancing happens over thousands of quiet-hour trips each month, so pausing it during rushes and
halving its cap does not affect compliance, while passengers wait less.

## 4. What was not changed, and why

* **Moving idle cars to "home" floors** could cut waits further in peaks, but it adds empty
  travel (energy and wear) and the SRS does not ask for it. The custom-scenario report names the
  floors that waited longest so this can be decided with data later.
* **Destination dispatch** (choosing the car from the destination keyed in at the landing) is a
  different interface (SRS UI requirements) and out of scope for this phase.
* All results come from the labelled software test stub (1 floor/s, 2 s door dwell, SWI-2): the
  differences between variants are meaningful, the absolute seconds are not field measurements.
