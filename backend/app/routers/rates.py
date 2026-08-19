"""
Rate/pricing layer — this is what turns a forecast into a dollar
recommendation ("charge now, not in an hour, rates spike then").

v1: static time-of-use (TOU) schedule you configure per utility.
Most residential utilities (PG&E included) publish fixed peak/off-peak
windows, so this covers the real case without needing a live pricing API.

v2 later: swap in a live rate API (e.g. utility's OpenEI/Green Button
data, or a service like WattTime) if you want real-time grid pricing
instead of a fixed schedule.
"""

from datetime import datetime, time
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

# Example PG&E-style residential TOU schedule — replace with the real
# schedule for your target utility/rate plan.
RATE_SCHEDULE = [
    {"start": time(0, 0), "end": time(15, 0), "rate_per_kwh": 0.32, "label": "off-peak"},
    {"start": time(15, 0), "end": time(21, 0), "rate_per_kwh": 0.52, "label": "peak"},
    {"start": time(21, 0), "end": time(23, 59), "rate_per_kwh": 0.32, "label": "off-peak"},
]


def rate_at(t: time) -> dict:
    for window in RATE_SCHEDULE:
        if window["start"] <= t < window["end"]:
            return window
    return RATE_SCHEDULE[0]


class RecommendationRequest(BaseModel):
    household_id: str
    device_battery_pct: float | None = None       # e.g. 5 (phone at 5%)
    kwh_needed: float = 0.5                        # rough estimate for a phone/laptop charge


class RecommendationResponse(BaseModel):
    action: str
    reasoning: str
    estimated_cost: float
    current_rate_label: str
    next_rate_change: str | None


@router.post("/recommend")
def recommend(req: RecommendationRequest):
    now = datetime.now()
    current = rate_at(now.time())

    # find the next window boundary from now
    upcoming = None
    for window in RATE_SCHEDULE:
        if window["start"] > now.time():
            upcoming = window
            break

    cost_now = round(req.kwh_needed * current["rate_per_kwh"], 2)

    if upcoming and upcoming["rate_per_kwh"] > current["rate_per_kwh"]:
        minutes_until = (
            datetime.combine(now.date(), upcoming["start"]) - now
        ).seconds // 60
        action = "Charge now"
        reasoning = (
            f"Rates jump to {upcoming['label']} "
            f"(${upcoming['rate_per_kwh']}/kWh) in about {minutes_until} min — "
            f"charging now at ${current['rate_per_kwh']}/kWh saves money."
        )
        next_change = f"{upcoming['label']} starts at {upcoming['start'].strftime('%-I:%M %p')}"
    else:
        action = "Charge now or wait — no rate spike coming up soon"
        reasoning = f"Currently in {current['label']} at ${current['rate_per_kwh']}/kWh."
        next_change = None

    return RecommendationResponse(
        action=action,
        reasoning=reasoning,
        estimated_cost=cost_now,
        current_rate_label=current["label"],
        next_rate_change=next_change,
    )
