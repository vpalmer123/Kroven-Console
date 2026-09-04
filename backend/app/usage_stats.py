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
from zoneinfo import ZoneInfo


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
            "All times above are the household's LOCAL time (Pacific). Quote them as "
            "given. Never append 'UTC' and never convert them."
        )
        lines.append(
            "All of the above are measurements from their own device. No model has "
            "been trained on this data, so never call any of it a forecast or "
            "prediction. If asked what comes next, give the observed range and say "
            "it is what has happened, not a projection."
        )
        return chr(10).join(lines)


# The beta covers the Bay Area only, so readings are reported in Pacific time.
# Timestamps are stored in UTC, and quoting them raw meant telling someone in
# San Francisco their peak was "4am UTC" — a real hour of their life, described
# in a timezone they don't live in, seven hours out.
LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt(dt: datetime, fmt: str) -> str:
    """Render an instant in the household's local time."""
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime(fmt).replace("AM", "am").replace("PM", "pm")
    except Exception:
        return dt.strftime(fmt)


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
        busiest = f"{h:02d}:00"

    fmt = "%b %d %I:%M%p"
    return UsageSummary(
        readings=len(points),
        non_zero=len(non_zero),
        total_kwh=sum(p[1] for p in points),
        first_at=_fmt(points[0][0], fmt),
        last_at=_fmt(points[-1][0], fmt),
        highest_kwh=hi[1],
        highest_at=_fmt(hi[0], fmt),
        lowest_kwh=lo[1],
        lowest_at=_fmt(lo[0], fmt),
        mean_kwh=statistics.fmean(p[1] for p in non_zero),
        median_kwh=statistics.median(p[1] for p in non_zero),
        hours_covered=span_hours,
        recent_24h_kwh=recent,
        busiest_hour=busiest,
    )


def by_device(db, household_id: str, rows: list[dict] | None = None) -> str | None:
    """Per-device totals, so "how much did the PS5 use" has a real answer.

    Readings carry the device they came from, but the whole-home summary folded
    that away, leaving Kroven saying it could not break usage out by device
    while holding hundreds of rows tagged with exactly that.

    Devices are grouped by the role a reading had when it was recorded, not by
    the plug's name today, so a reassignment does not merge two different loads
    into one total.
    """
    if rows is None:
        rows = fetch(db, household_id, signal_type=None)
    if not rows:
        return None

    try:
        from app.device_registry import list_devices, role_rules, signal_type_of
        rules = role_rules(household_id)
        # Name per (kind, role), not per kind. A plug that moved from the PS5 to
        # a shared cord answers to a different name for each period, and calling
        # its old readings by its new name misattributes them to the wrong
        # appliance entirely — the peak that was the PS5 gets reported as the
        # cord.
        names: dict[tuple[str, str], str] = {}
        for d in list_devices(household_id):
            kind = (d.get("kind") or "").lower()
            meta = d.get("meta") or {}
            names[(kind, d.get("signal_type") or "dedicated")] = d.get("name")
            if meta.get("previous_signal_type") and meta.get("previous_name"):
                names[(kind, meta["previous_signal_type"])] = meta["previous_name"]
    except Exception:
        return None

    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        src = str(r.get("source") or "")
        kind = src.split(":")[0].lower()
        if not kind:
            continue
        sig = signal_type_of(rules, src, r.get("recorded_at")) or "unknown"
        groups.setdefault((kind, sig), []).append(r)

    if len(groups) < 2 and not any(k[1] == "dedicated" for k in groups):
        return None

    now = datetime.now(timezone.utc)
    lines = []
    for (kind, sig), grp in sorted(groups.items(), key=lambda g: -len(g[1])):
        s = summarise(grp)
        if s is None:
            continue
        pts = [(_parse(r["recorded_at"]), float(r["kwh_consumed"] or 0)) for r in grp
               if _parse(r.get("recorded_at"))]
        last24 = sum(k for t, k in pts if (now - t) <= timedelta(hours=24))
        label = names.get((kind, sig)) or names.get((kind, "dedicated")) or kind
        role = ("this is ONE appliance, so this total is that appliance"
                if sig == "dedicated"
                else "several loads share this plug, so this is a combined total, "
                     "not any single appliance")
        window = f"{s.first_at} to {s.last_at}"
        lines.append(
            f"- {label} [{kind} plug] ({sig}, measured {window}): {s.total_kwh:.3f} kWh across "
            f"{s.readings} readings, {last24:.3f} kWh in the last 24h. Peak reading "
            f"{s.highest_kwh:.4f} kWh at {s.highest_at}. {role}."
        )

    if not lines:
        return None
    return ("PER-DEVICE BREAKDOWN (you CAN answer per-device questions from this):"
            + chr(10) + chr(10).join(lines))


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
    # PostgREST caps a single response at 1000 rows however large a limit is
    # requested, and returns the newest ones. Asking for 2000 therefore gave
    # 1000 silently — enough to make a whole device disappear from a summary
    # once its readings fell outside the newest page, which read as data loss
    # rather than truncation. Paged explicitly instead.
    PAGE = 1000
    rows: list[dict] = []
    try:
        while len(rows) < limit:
            start = len(rows)
            end = min(start + PAGE, limit) - 1
            page = (
                db.table("energy_readings")
                .select("recorded_at,kwh_consumed,source")
                .eq("household_id", household_id)
                .order("recorded_at", desc=True)
                .range(start, end)
                .execute()
                .data
            ) or []
            rows.extend(page)
            if len(page) < (end - start + 1):
                break                      # last page
    except Exception:
        if not rows:
            return []

    if signal_type is None:
        return rows

    try:
        from app.device_registry import role_rules, signal_type_of
        rules = role_rules(household_id)
    except Exception:
        return rows

    if not rules:
        return rows

    # Classified as of each reading's own timestamp, not the device's role
    # today. A plug that moved from the PS5 to a shared cord logged genuine
    # single-appliance data before the move, and that data stays dedicated.
    return [
        r for r in rows
        if signal_type_of(rules, r.get("source"), r.get("recorded_at")) == signal_type
    ]
