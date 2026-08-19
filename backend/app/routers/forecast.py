"""
This is your "weather for energy" endpoint. It:
  1. pulls recent readings for a household from the DB
  2. runs them through the LSTM (or naive fallback)
  3. stores the forecast (so repeat requests don't recompute — same
     reason weather apps feel instant)
  4. returns it with a confidence band, not a fake-precise number
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from app.db import get_db
from app.model import predict_next

router = APIRouter()


@router.get("/{household_id}")
def get_forecast(household_id: str, force_refresh: bool = False):
    db = get_db()

    if not force_refresh:
        cached = (
            db.table("forecasts")
            .select("*")
            .eq("household_id", household_id)
            .order("generated_at", desc=True)
            .limit(1)
            .execute()
        )
        if cached.data:
            latest = cached.data[0]
            generated_at = datetime.fromisoformat(latest["generated_at"])
            if datetime.now(timezone.utc) - generated_at < timedelta(minutes=15):
                return latest  # serve cached — this is the "instant" part

    readings = (
        db.table("energy_readings")
        .select("*")
        .eq("household_id", household_id)
        .order("recorded_at", desc=False)
        .limit(48)
        .execute()
    )
    if not readings.data:
        raise HTTPException(
            status_code=404,
            detail="No energy data yet for this household — upload some readings first.",
        )

    result = predict_next(readings.data)
    forecast_for = datetime.now(timezone.utc) + timedelta(hours=1)

    row = {
        "household_id": household_id,
        "forecast_for": forecast_for.isoformat(),
        "predicted_kwh": result["predicted_kwh"],
        "lower_bound": result["lower_bound"],
        "upper_bound": result["upper_bound"],
        "model_version": result["model_version"],
    }
    db.table("forecasts").insert(row).execute()
    return row
