"""Recorded validation scenarios for the scenario player (``frontend/public/player``).

* ``GET /api/v1/operational/stories``        list of scenarios: key, VT IDs, title, mode.
* ``GET /api/v1/operational/stories/{key}``  record one scenario now and return it.
* ``POST /api/v1/operational/stories/custom`` run your own scenario (people, floors, timing,
  incidents) under three dispatch rules; returns the SREMS recording plus the comparison.
  Spec format: ``simulation/custom_scenario.py``. A spec error answers 422 with the field.

Each request runs the scenario on a fresh, private ``OperationalSimulator`` (no database, no
background thread), so it never touches the live ``operational_simulator`` and writes nothing
to PostgreSQL. A recording only comes back if every pass criterion of that scenario was met;
otherwise the endpoint answers 500 with the criterion that failed. The output is labelled as
software test-stub output (SWI-2) like the rest of ``/operational``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from simulation import custom_scenario, story_recorder

router = APIRouter(prefix="/operational/stories", tags=["scenario-player"])


@router.get("")
async def list_stories() -> dict:
    """Scenario metadata. Recording all twelve takes well under a second."""
    data = await run_in_threadpool(story_recorder.record_all)
    keep = ("key", "title", "vt", "mode", "tagline", "requirements", "until")
    return {"source": data["source"], "generatedAt": data["generatedAt"],
            "stories": [{**{k: s[k] for k in keep}, "criteria": len(s["criteria"])} for s in data["stories"]]}


@router.post("/custom")
async def run_custom(spec: dict) -> dict:
    """Run a user-defined scenario. Unlike the built-in tests, failing criteria are reported,
    not refused: finding out whether the operation holds up is the point."""
    try:
        return await run_in_threadpool(custom_scenario.compare, spec)
    except custom_scenario.SpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/{key}")
async def record_story(key: str) -> dict:
    try:
        story = await run_in_threadpool(story_recorder.record_one, key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown scenario '{key}'.")
    except AssertionError as exc:
        raise HTTPException(status_code=500, detail=f"Scenario recorded but a pass criterion failed: {exc}")
    return story
