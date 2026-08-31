"""
GET /api/activity?household_id=...

Current occupancy/activity state for a household, or — while no sensor is
connected — clearly-flagged examples of what it will show.

The response never contains a name that was hardcoded here or stored as a
sample. See app/activity.py for how the subject is resolved.
"""

from fastapi import APIRouter, HTTPException

from app.activity import build_state
from app.db import get_db

router = APIRouter()


@router.get("")
def get_activity(household_id: str | None = None):
    if not household_id:
        raise HTTPException(status_code=400, detail="household_id is required")

    try:
        db = get_db()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"database unavailable: {type(e).__name__}") from e

    profile = None
    try:
        rows = (
            db.table("household_profiles")
            .select("*")
            .eq("household_id", household_id)
            .limit(1)
            .execute()
            .data
        )
        profile = rows[0] if rows else None
    except Exception:
        profile = None

    return build_state(db, household_id, profile)
