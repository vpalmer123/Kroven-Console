"""
Ingest and retrieve raw energy readings. This is what your frontend's
data-upload box (the textarea/file-upload you already have in index.html)
should actually POST to, instead of just holding the pasted text in
window.__userData for a single session.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_db

router = APIRouter()


class EnergyReading(BaseModel):
    household_id: str
    recorded_at: datetime
    kwh_consumed: Optional[float] = None
    kwh_solar_generated: Optional[float] = None
    battery_soc_pct: Optional[float] = None
    grid_import_kwh: Optional[float] = None
    source: str = "manual"


@router.post("/readings")
def add_reading(reading: EnergyReading):
    db = get_db()
    payload = reading.model_dump(mode="json")
    result = db.table("energy_readings").insert(payload).execute()
    return {"inserted": len(result.data)}


@router.post("/readings/bulk")
def add_readings_bulk(readings: list[EnergyReading]):
    """Use this for CSV/paste uploads — one call instead of N round trips."""
    db = get_db()
    payload = [r.model_dump(mode="json") for r in readings]
    result = db.table("energy_readings").insert(payload).execute()
    return {"inserted": len(result.data)}


@router.get("/readings/{household_id}")
def get_readings(household_id: str, limit: int = 500):
    db = get_db()
    result = (
        db.table("energy_readings")
        .select("*")
        .eq("household_id", household_id)
        .order("recorded_at", desc=True)
        .limit(limit)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="No readings for this household yet")
    return result.data
