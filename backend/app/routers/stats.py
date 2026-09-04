"""
Public, non-identifying numbers for the sign-in screen.

Everything here is counted from the database at request time. Nothing is
rounded up, padded, or invented — a login page that opens with a fabricated
"50,000 homes" is the same lie as a fabricated forecast, and this project has
been careful about the second one.

Deliberately excluded: anything that identifies a household. Counts and spans
only, no addresses, no per-household figures, no device names. It is reachable
without a session by design, so it must be safe to show a stranger.

If a number cannot be computed it is omitted rather than defaulted to zero.
"Zero readings" and "we could not count the readings" are different claims.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from app.db import get_db

logger = logging.getLogger("kroven.stats")
router = APIRouter()


def _count(db, table: str) -> int | None:
    try:
        r = db.table(table).select("*", count="exact").limit(1).execute()
        return r.count
    except Exception as e:
        logger.warning("count(%s) failed: %s", table, type(e).__name__)
        return None


@router.get("")
async def public_stats():
    db = get_db()
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    readings = _count(db, "energy_readings")
    if readings is not None:
        out["readings_logged"] = readings

    signals = _count(db, "observations")
    if signals is not None:
        out["signals_captured"] = signals

    households = _count(db, "households")
    if households is not None:
        out["households"] = households

    devices = _count(db, "devices")
    if devices is not None:
        out["devices_connected"] = devices

    # How long the pool has actually been collecting, from the oldest reading.
    try:
        first = (
            db.table("energy_readings").select("recorded_at")
            .order("recorded_at").limit(1).execute().data
        )
        if first:
            started = datetime.fromisoformat(
                str(first[0]["recorded_at"]).replace("Z", "+00:00"))
            out["collecting_since"] = started.date().isoformat()
            out["days_collecting"] = max(
                1, (datetime.now(timezone.utc) - started).days)
    except Exception as e:
        logger.warning("collecting_since failed: %s", type(e).__name__)

    return out
