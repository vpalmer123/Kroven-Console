"""
Rate/pricing layer — turns the clock into a cost recommendation.

Prices come from app.rate_data, which carries its own citation and effective
date. Nothing here invents a number: where the published tariff gives a range
(because Baseline Allowance is unknown to us), a range is what comes out.

The old version of this file hardcoded a flat $0.32/$0.52 with a 3pm peak and a
comment saying "Example PG&E-style ... replace with the real schedule". Those
were placeholders that had been reaching users as if they were real prices.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel

from app.rate_data import PEAK_END, PEAK_START, other_period, period_for, pricing_at

router = APIRouter()


class RecommendationRequest(BaseModel):
    household_id: str
    device_battery_pct: float | None = None
    kwh_needed: float | None = None   # None => no cost estimate, we don't guess


class RecommendationResponse(BaseModel):
    action: str
    reasoning: str
    current_period: str
    price_low: float
    price_high: float
    price_note: str
    next_change: str | None
    estimated_cost: str | None


def _next_boundary(now: datetime) -> tuple[str, str] | None:
    """When the price next changes, and what it changes to."""
    today = now.date()
    if now.time() < PEAK_START:
        return ("peak", datetime.combine(today, PEAK_START).strftime("%I:%M %p").lstrip("0"))
    if now.time() < PEAK_END:
        return ("off-peak", datetime.combine(today, PEAK_END).strftime("%I:%M %p").lstrip("0"))
    return ("peak", (datetime.combine(today, PEAK_START) + timedelta(days=1)).strftime("%I:%M %p").lstrip("0"))


@router.post("/recommend")
def recommend(req: RecommendationRequest) -> RecommendationResponse:
    now = datetime.now()
    current = pricing_at(now)
    alternate = other_period(now)

    change = _next_boundary(now)
    next_change = f"{change[0]} pricing starts at {change[1]}" if change else None

    if current.period == "off_peak":
        action = "Now is the cheaper window"
        reasoning = (
            f"Energy is in the lower-priced window ({current.range_text()}). "
            f"During the higher-priced window it runs {alternate.range_text()}."
        )
    else:
        action = "Now is the expensive window"
        reasoning = (
            f"Energy is in the higher-priced window ({current.range_text()}). "
            f"Outside it, energy runs {alternate.range_text()}."
        )

    estimated = current.cost_range(req.kwh_needed) if req.kwh_needed else None

    return RecommendationResponse(
        action=action,
        reasoning=reasoning,
        current_period="higher-priced" if current.period == "peak" else "lower-priced",
        price_low=current.low,
        price_high=current.high,
        price_note=(
            "A range because what you pay per kWh depends on your plan and how much "
            "you have already used this month, which we do not know."
        ),
        next_change=next_change,
        estimated_cost=estimated,
    )
