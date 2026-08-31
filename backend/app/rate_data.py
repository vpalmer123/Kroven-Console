"""
Verified retail electricity pricing.

Every number in here was read off the published tariff sheet cited below — not
estimated, not remembered. Nothing gets shown to a user unless it carries this
provenance, so if you add a plan you must add its source with it.

Source: "Residential rate plan pricing", Pacific Gas and Electric Company.
        https://www.pge.com/assets/pge/docs/account/rate-plans/residential-electric-rate-plan-pricing.pdf
        Prices stated on that sheet as "effective March 1, 2026".

Two honest limits, which callers must surface rather than paper over:

  1. TIER. Residential pricing is split by Baseline Allowance — energy under the
     allowance is cheaper than energy above it. The allowance depends on the
     customer's territory, heating source and season, none of which we know. So
     a single "your rate" does not exist; we can only give the pair.

  2. PLAN. These are E-TOU-C numbers, the common default time-of-use plan.
     A customer on a tiered, EV or Electric Home plan pays differently, and we
     have no way to detect which plan someone is on.

The sheet itself notes prices are rounded to the nearest cent and exclude taxes
and local surcharges.
"""

from datetime import date, time
from typing import Literal, NamedTuple

SOURCE_NAME = "Pacific Gas and Electric Company published residential rate sheet"
SOURCE_URL = (
    "https://www.pge.com/assets/pge/docs/account/rate-plans/"
    "residential-electric-rate-plan-pricing.pdf"
)
SOURCE_EFFECTIVE = "March 1, 2026"
PLAN_CODE = "E-TOU-C"

Season = Literal["summer", "winter"]
Period = Literal["peak", "off_peak"]

# Peak applies every day, 4pm-9pm, on this plan.
PEAK_START = time(16, 0)
PEAK_END = time(21, 0)

# dollars per kWh, by season -> period -> baseline tier
PRICING: dict[Season, dict[Period, dict[str, float]]] = {
    "summer": {
        "off_peak": {"below_baseline": 0.32, "above_baseline": 0.40},
        "peak": {"below_baseline": 0.44, "above_baseline": 0.52},
    },
    "winter": {
        "off_peak": {"below_baseline": 0.29, "above_baseline": 0.37},
        "peak": {"below_baseline": 0.32, "above_baseline": 0.40},
    },
}


class Pricing(NamedTuple):
    season: Season
    period: Period
    low: float           # below-baseline price
    high: float          # above-baseline price
    peak_start: time
    peak_end: time
    source: str
    source_url: str
    effective: str
    plan: str

    def range_text(self) -> str:
        return f"${self.low:.2f}-${self.high:.2f}/kWh"

    def cost_range(self, kwh: float) -> str:
        return f"${kwh * self.low:.2f}-${kwh * self.high:.2f}"


def season_for(day: date) -> Season:
    """Summer runs June 1 - Sept 30; winter is the rest of the year."""
    return "summer" if 6 <= day.month <= 9 else "winter"


def period_for(t: time) -> Period:
    return "peak" if PEAK_START <= t < PEAK_END else "off_peak"


def pricing_at(when) -> Pricing:
    season = season_for(when.date())
    period = period_for(when.time())
    band = PRICING[season][period]
    return Pricing(
        season=season,
        period=period,
        low=band["below_baseline"],
        high=band["above_baseline"],
        peak_start=PEAK_START,
        peak_end=PEAK_END,
        source=SOURCE_NAME,
        source_url=SOURCE_URL,
        effective=SOURCE_EFFECTIVE,
        plan=PLAN_CODE,
    )


def other_period(when) -> Pricing:
    """The pricing for the opposite period in the same season, for comparisons."""
    season = season_for(when.date())
    period = "off_peak" if period_for(when.time()) == "peak" else "peak"
    band = PRICING[season][period]
    return Pricing(
        season=season,
        period=period,
        low=band["below_baseline"],
        high=band["above_baseline"],
        peak_start=PEAK_START,
        peak_end=PEAK_END,
        source=SOURCE_NAME,
        source_url=SOURCE_URL,
        effective=SOURCE_EFFECTIVE,
        plan=PLAN_CODE,
    )
