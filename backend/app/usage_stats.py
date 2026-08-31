"""
Summaries of a household's own logged readings.

These are the person's own measurements, so unlike pricing they can be quoted
back with numbers. Every figure here is computed from rows in energy_readings —
nothing is modelled, estimated or filled in.

The "forecast" here is explicitly a range taken from observed history, not a
prediction from a trained model. No model has ever been trained on this data,
so calling it a forecast would be a lie; it is described as what their usage has
actually been running at.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class UsageSummary:
    readings: int
    non_zero: int
    total_kwh: float
    first_at: str
    last_at: str
    highest_kwh: float
    highest_at: str
    lowest_kwh: float
    lowest_at: str
    mean_kwh: float
    median_kwh: float
    hours_covered: float
    recent_24h_kwh: float | None
    busiest_hour: str | None

    def describe(self) -> str:
        lines = [
            f"Log covers {self.first_at} to {self.last_at} "
            f"(about {self.hours_covered:.0f} hours), {self.readings} readings, "
            f"{self.non_zero} of them above zero.",
            f"Total energy across that WHOLE period: {self.total_kwh:.3f} kWh. "
            f"This is the all-time total for the log, NOT a daily figure.",
        ]
        if self.recent_24h_kwh is not None:
            lines.append(
                f"Of that, the last 24 hours alone: {self.recent_24h_kwh:.3f} kWh."
            )
        else:
            lines.append("Nothing logged in the last 24 hours.")
        lines += [
            f"Highest single reading: {self.highest_kwh:.4f} kWh at {self.highest_at}.",
            f"Lowest non-zero reading: {self.lowest_kwh:.4f} kWh at {self.lowest_at}.",
            f"Typical non-zero reading: {self.median_kwh:.4f} kWh median, "
            f"{self.mean_kwh:.4f} kWh mean.",
            f"Sampling: readings are 5-minute intervals where the device reported "
            f"power, and hourly totals elsewhere. Do NOT describe them as any other "
            f"interval - never say '15-minute' or invent a cadence.",
        ]
        if self.busiest_hour:
            lines.append(f"Hour of day carrying the most energy so far: {self.busiest_hour}.")
        lines.append(
            "All of the above are measurements from their own device. No model has "
            "been trained on this data, so never call any of it a forecast or "
            "prediction. If asked what comes next, give the observed range and say "
            "it is what has happened, not a projection."
        )
        return chr(10).join(lines)


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def summarise(rows: list[dict]) -> UsageSummary | None:
    """Build a summary, or None when there is nothing worth summarising."""
    if not rows:
        return None

    points = []
    for r in rows:
        when = _parse(r.get("recorded_at"))
        try:
            kwh = float(r.get("kwh_consumed"))
        except (TypeError, ValueError):
            continue
        if when is not None:
            points.append((when, kwh))

    if not points:
        return None
    points.sort(key=lambda p: p[0])

    non_zero = [p for p in points if p[1] > 0]
    if not non_zero:
        return None

    hi = max(non_zero, key=lambda p: p[1])
    lo = min(non_zero, key=lambda p: p[1])
    span_hours = (points[-1][0] - points[0][0]).total_seconds() / 3600

    now = datetime.now(timezone.utc)
    last_24 = [p for p in points if (now - p[0]) <= timedelta(hours=24)]
    recent = sum(p[1] for p in last_24) if last_24 else None

    # Which hour of the day carries the most energy across the whole log.
    by_hour: dict[int, float] = {}
    for when, kwh in non_zero:
        by_hour[when.hour] = by_hour.get(when.hour, 0.0) + kwh
    busiest = None
    if by_hour:
        h = max(by_hour, key=by_hour.get)
        busiest = f"{h:02d}:00 UTC"

    fmt = "%b %d %H:%M UTC"
    return UsageSummary(
        readings=len(points),
        non_zero=len(non_zero),
        total_kwh=sum(p[1] for p in points),
        first_at=points[0][0].strftime(fmt),
        last_at=points[-1][0].strftime(fmt),
        highest_kwh=hi[1],
        highest_at=hi[0].strftime(fmt),
        lowest_kwh=lo[1],
        lowest_at=lo[0].strftime(fmt),
        mean_kwh=statistics.fmean(p[1] for p in non_zero),
        median_kwh=statistics.median(p[1] for p in non_zero),
        hours_covered=span_hours,
        recent_24h_kwh=recent,
        busiest_hour=busiest,
    )


def fetch(db, household_id: str, limit: int = 2000,
          signal_type: str | None = "dedicated") -> list[dict]:
    """Readings for a household, by default only from dedicated-signal devices.

    A dedicated plug carries one load, so its trace is that appliance. An
    aggregate plug (a shared extension cord) carries several at once, so summing
    the two produces a series that describes no real thing: per-device highs and
    medians computed across the mix are not about any device. Aggregate rows are
    still collected — they are the input for occupancy and correlation work,
    where a mixed signal is the point — they just do not belong here.

    Pass signal_type=None to get everything, e.g. for whole-home totals.
    """
    try:
        q = (
            db.table("energy_readings")
            .select("recorded_at,kwh_consumed,source")
            .eq("household_id", household_id)
            .order("recorded_at", desc=True)
            .limit(limit)
        )
        rows = q.execute().data or []
    except Exception:
        return []

    if signal_type is None:
        return rows

    try:
        from app.device_registry import sources_for
        wanted = {s.lower() for s in sources_for(household_id, signal_type)}
    except Exception:
        return rows

    if not wanted:
        return rows

    # `source` looks like 'kasa', 'shelly:shelly_kroven' or 'shelly:x:live'.
    # Match on any prefix segment so the tag survives those suffixes.
    kept = []
    for r in rows:
        src = str(r.get("source") or "").lower()
        if not src:
            continue
        parts = src.split(":")
        if any(p in wanted for p in parts) or src in wanted:
            kept.append(r)
    return kept
