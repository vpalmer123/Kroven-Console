"""
GET /api/dashboard?household_id=&lat=&lon=

Everything the operations view needs, in one call: the household's own usage
series, live grid load, and local conditions.

Every tile carries a `state`:
    live      real values, safe to render
    empty     the source exists but has nothing yet
    missing   no hardware or feed for this, and we say so

Nothing is ever filled with a placeholder number to make a tile look populated.
A dashboard of invented gauges is exactly the failure this codebase keeps
guarding against, and it is more obvious on a chart than in a sentence.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.activity import build_state as build_activity
from app.db import get_db
from app.ingest import baselines, usage_vs_typical
from app.signals import location_signals
from app.usage_stats import fetch as fetch_readings, summarise as summarise_usage

router = APIRouter()

MAX_POINTS = 96          # what a compact sparkline can actually resolve


def _series(rows: list[dict]) -> list[dict]:
    """Oldest-first points for charting, thinned to a drawable count."""
    points = []
    for r in rows:
        try:
            kwh = float(r.get("kwh_consumed"))
        except (TypeError, ValueError):
            continue
        ts = str(r.get("recorded_at") or "")
        if ts:
            points.append({"t": ts, "kwh": round(kwh, 6)})
    points.sort(key=lambda p: p["t"])

    if len(points) <= MAX_POINTS:
        return points
    step = len(points) / MAX_POINTS
    return [points[int(i * step)] for i in range(MAX_POINTS)]


@router.get("")
async def dashboard(household_id: str | None = None,
                    lat: float | None = None,
                    lon: float | None = None,
                    region: str | None = None):
    if not household_id:
        raise HTTPException(status_code=400, detail="household_id is required")

    db = get_db()
    tiles: dict = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # ---- their own usage -------------------------------------------------
    rows = fetch_readings(db, household_id)
    summary = summarise_usage(rows)
    if summary is None:
        tiles["usage"] = {
            "state": "empty",
            "note": ("No readings for this browser's session. Readings are tied to the "
                     "device you first used Kroven on, so a phone starts empty even "
                     "when another browser has data."),
        }
    else:
        tiles["usage"] = {
            "state": "live",
            "series": _series(rows),
            "total_kwh": round(summary.total_kwh, 3),
            "recent_24h_kwh": (round(summary.recent_24h_kwh, 3)
                               if summary.recent_24h_kwh is not None else None),
            "highest_kwh": round(summary.highest_kwh, 4),
            "highest_at": summary.highest_at,
            "lowest_kwh": round(summary.lowest_kwh, 4),
            "lowest_at": summary.lowest_at,
            "median_kwh": round(summary.median_kwh, 4),
            "readings": summary.readings,
            "non_zero": summary.non_zero,
            "hours_covered": round(summary.hours_covered, 1),
            "busiest_hour": summary.busiest_hour,
            "first_at": summary.first_at,
            "last_at": summary.last_at,
        }

    # ---- grid + local conditions ----------------------------------------
    if lat is not None and lon is not None:
        signals = await location_signals(lat, lon)
        by_key = {s["key"]: s for s in signals["signals"]}
        tiles["grid"] = by_key.get("grid_load", {"state": "missing"})
        tiles["weather"] = {
            "precip": by_key.get("precip"),
            "wind": by_key.get("wind"),
            "alerts": by_key.get("alerts"),
        }
        tiles["signals_generated_at"] = signals["generated_at"]
    else:
        tiles["grid"] = {"state": "missing", "note": "Location needed for grid data."}
        tiles["weather"] = {"state": "missing", "note": "Location needed for conditions."}

    # ---- rolling thresholds learned from this household's own devices ----
    tiles["baselines"] = baselines(db, household_id)
    tiles["vs_typical"] = usage_vs_typical(db, household_id)

    # ---- devices / occupancy --------------------------------------------
    try:
        prof_rows = (db.table("household_profiles").select("*")
                     .eq("household_id", household_id).limit(1).execute().data)
        profile = prof_rows[0] if prof_rows else None
    except Exception:
        profile = None

    activity = build_activity(db, household_id, profile)
    tiles["activity"] = {
        "state": "live" if activity.get("live") else "missing",
        "line": activity.get("line"),
        "devices": activity.get("devices_known", []),
        "note": None if activity.get("live") else "No occupancy sensor connected.",
    }

    # Assets we have no telemetry for. Named so the UI can show them as
    # unequipped rather than silently omitting them.
    assets = (profile or {}).get("assets") or {}
    tiles["unequipped"] = [
        {"name": "Solar", "reason": "no inverter feed"
         if (assets.get("solar") or {}).get("present") else "not reported"},
        {"name": "Battery", "reason": "no telemetry"
         if (assets.get("battery") or {}).get("present") else "not reported"},
        {"name": "EV", "reason": "no charger connected"},
    ]
    return tiles
