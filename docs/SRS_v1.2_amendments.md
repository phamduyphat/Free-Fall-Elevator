# SRS v1.2 — proposed amendments (Group 4, phase 2)

Paste-ready text for the SRS. Changes come from the instructor's phase-2 NFR (17 Nov 2026) and
from the implementation review. Section numbers refer to SRS v1.1 (19 Oct 2026).

## 3.2.1 — amended functional requirements

**FR-1.4 — Direction-aware dispatch (amended).** For a normal residential hall call, the system
shall select an eligible residential elevator with the lowest *assignment cost*, where cost = predicted
service time + usage penalty (WEAR-3). Predicted service time considers the requesting direction,
current position, direction of travel, existing commitments, and availability; it shall not select by
absolute floor distance alone. A freight call is assigned to E5 only (FR-1.13).

**FR-1.5 — Dispatch tie-break (amended).** If two eligible cars have the same assignment cost within the
implementation precision, the system shall select the car with fewer floors travelled in the current month,
then the lower elevator identifier (E1 before E2, then E3, then E4).

**FR-1.24 — Collective service (new, Should).** A car call shall be served on the way when the car's
committed route already passes the destination floor; otherwise it is served after existing commitments.
A hall call may be added to any committed leg that passes its floor in the requested direction.

## 3.4 — new non-functional requirements (Must)

**WEAR-1 — Trip counters.** For each car, SREMS shall count floors travelled (one count per floor passed;
B1 to Floor 1 counts as one floor), motor starts, and door cycles, for the current calendar month, the
current 6-month period, and the car's lifetime. Counters shall be persisted (DAT-1) and survive a restart.

**WEAR-2 — Monthly balance.** For the residential dispatch group E1–E4, the spread
(max − min) / max of floors travelled in each calendar month shall be under 2 %.

**WEAR-3 — Balancing dispatch.** The usage penalty of FR-1.4 is
`min(P_max, G · (d_i − d_min) / (L · max(d_max, R)))`, where d is the month-to-date floors of the eligible
cars of the same group, L is the WEAR-2 limit, G = 60 s, P_max = 15 s, and R = 3,000 floors (configurable).
The penalty applies only while the group is not busy: active ordinary hall calls ≤ the number of eligible
cars of the group (configurable).
Balancing shall never add more than P_max to the predicted service time of the chosen car.

**WEAR-4 — Month close.** At the start of each calendar month SREMS shall record, per group, each car's
floors, the spread, the limit, and the verdict; reset the month counters; and log an event, flagged when
the limit was exceeded.

**WEAR-5 — 6-month period.** The spread of floors travelled over each 6-month period shall be at most 5 %.
At the end of each period SREMS shall record the result and reset the period trip counters. Lifetime
counters are never reset.

**WEAR-6 — Visibility.** Admins and Technicians shall see live counters, month-to-date and period-to-date
spread, and closed windows (UI-10, MNT-4.5). E5 is reported as its own group.

## Table 8 — new open requirement

| ID | Open topic | Resolution required |
|---|---|---|
| OR-21 | Wear-balance scope | Confirm with the instructor: (a) E5 forms its own group because only E5 serves freight calls (FR-1.13); (b) spread denominator is the maximum distance; (c) month boundaries in UTC or building-local time; (d) 6-month period anchored at commissioning; (e) whether a month in which a car was in declared maintenance near month end may be exempted, or empty levelling runs are acceptable. |

## Table 9 — new validation tests

| Test ID | Scenario | Pass criteria |
|---|---|---|
| VT-33 | Monthly balance (WEAR-2, WEAR-3) | One simulated month of residential traffic with full journeys: spread of E1–E4 < 2 %; PERF-9 and PERF-10 still met. The same seed with balancing off is reported for comparison. |
| VT-34 | 6-month balance and reset (WEAR-4, WEAR-5) | Six simulated months with maintenance outages: every month < 2 %, the period ≤ 5 %, period counters reset to 0 at period end, lifetime counters kept. |
| VT-35 | Counter persistence (WEAR-1) | Counters survive a restart; a month that ended while SREMS was down is closed at start-up with the last synced values. |
| VT-36 | Bounded penalty (WEAR-3) | With equal service time the less-used car is chosen; a car with a penalty never wins against a car that is more than P_max seconds faster; balancing off reproduces the v1.1 tie-break. |

## Table 10 — traceability (new row)

| Need ID | Stakeholder need | Use cases | Requirements | Tests |
|---|---|---|---|---|
| SN-12 | Building owner / Technician: even wear, predictable maintenance | UC-01, UC-06 | FR-1.4, FR-1.5, FR-1.24, WEAR-1 to WEAR-6 | VT-33 to VT-36 |

## Table 11 — NFR matrix (new rows)

| NFR ID | Attribute | Requirement | Metric and target | Verification | Tests |
|---|---|---|---|---|---|
| NFR-20 | Maintainability (wear) | WEAR-2 | (max − min)/max of monthly floors, E1–E4: < 2 % | 1-month benchmark | VT-33 |
| NFR-21 | Maintainability (wear) | WEAR-5 | Same over 6 months: ≤ 5 %; counters reset | 6-month benchmark | VT-34 |

## Other corrections found in review

* **PERF-8:** peak/off-peak windows are building-local clock times; add "local time of the building
  (configuration)". The prototype now applies `SREMS_BUILDING_UTC_OFFSET_HOURS` (7 for Hanoi).
* **VT-23:** traffic profiles shall include the destination (car call) of each passenger, otherwise
  travel distance and loaded runs are not exercised.
* **Door safety (EMR / FR door handling), found while recording the scenario player:** (1) the
  dwell timer could close the doors while an obstruction was still active; (2) when an obstruction
  cleared, the car could leave in the same second with its doors still open. The simulator now
  keeps the doors open while obstructed, closes them first and only then moves
  (`tests/test_operational_incidents.py`, `tests/test_story_recorder.py`). Proposed wording for
  the SRS: "A car shall not start moving until its doors are confirmed closed."
* **Full-load bypass (FR-1.14, CAP-2), found with a custom scenario:** without load data a full
  car could be reassigned the call of the person it had just left behind and reopen at that floor
  indefinitely. When load data exists, a car at rated load takes no new hall calls and passes its
  queued pickups, which are reassigned with their acceptance time kept (FR-1.18). Proposed
  wording: "When load data is available, a car at or above rated load shall not be assigned hall
  calls, and its pending hall calls shall be reassigned."
* **Wear balancing under heavy traffic (WEAR-3), found with a custom scenario and fixed:** in a
  stress scenario (190 people in 3 minutes, one car 9,000 floors behind) the penalty sent the
  lagging car ten pickups at once; average wait rose from 25.7 s (no balancing) to 45.3 s. The
  penalty now pauses while the group is busy, and its cap is 15 s instead of 30 s. Evidence:
  `docs/DISPATCH_TUNING.md`.
* **Revision history:** 1.2 — added WEAR-1..WEAR-6, FR-1.24, OR-21, VT-33..VT-36, SN-12, NFR-20/21;
  amended FR-1.4, FR-1.5, PERF-8, VT-23.
