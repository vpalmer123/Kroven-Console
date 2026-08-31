"""
Multi-source ingestion + the live baseline read.

Every device writes the same row shape — household, time, source, signal_type,
value, meta — so Shelly Gen4 and the ESP32 slot in beside Kasa without a schema
change or a branch in the reader. Adding a domain is adding a signal_type.

Falls back to energy_readings when the observations table has not been created
yet, so the baseline works off the Kasa data that is already in the pool today.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.baseline import Baseline, Correlation, compute, correlate_presence, transitions

POWER_TYPES = ("power_w", "energy_kwh")
PRESENCE_TYPES = ("presence", "csi_variance")


def _parse(ts) -> datetime | None:
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def record(db, household_id: str, source: str, signal_type: str,
           value: float | None, observed_at: datetime | None = None,
           meta: dict | None = None) -> bool:
    """Write one observation. Returns False if the table is not there yet."""
    row = {
        "household_id": household_id,
        "observed_at": (observed_at or datetime.now(timezone.utc)).isoformat(),
        "source": source,
        "signal_type": signal_type,
        "value": value,
        "meta": meta or {},
    }
    try:
        db.table("observations").upsert(
            row, on_conflict="household_id,source,signal_type,observed_at"
        ).execute()
        return True
    except Exception:
        return False


def _from_observations(db, household_id: str, since_hours: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    try:
        return (
            db.table("observations")
            .select("observed_at,source,signal_type,value")
            .eq("household_id", household_id)
            .gte("observed_at", cutoff)
            .order("observed_at", desc=True)
            .limit(5000)
            .execute()
            .data
        ) or []
    except Exception:
        return []


def _from_energy_readings(db, household_id: str) -> list[dict]:
    """The pool that exists today: Kasa kWh already imported."""
    try:
        rows = (
            db.table("energy_readings")
            .select("recorded_at,kwh_consumed,source")
            .eq("household_id", household_id)
            .order("recorded_at", desc=True)
            .limit(3000)
            .execute()
            .data
        ) or []
    except Exception:
        return []
    out = []
    for r in rows:
        src = (r.get("source") or "unknown").split(":")
        label = ":".join(src[:2]) if len(src) > 1 else src[0]
        out.append({
            "observed_at": r.get("recorded_at"),
            "source": label,
            "signal_type": "energy_kwh",
            "value": r.get("kwh_consumed"),
        })
    return out


def gather(db, household_id: str, since_hours: int = 24 * 30) -> list[dict]:
    """Everything measured for this household, from every source."""
    rows = _from_observations(db, household_id, since_hours)
    rows.extend(_from_energy_readings(db, household_id))
    return rows


def baselines(db, household_id: str) -> dict:
    """Current rolling thresholds per device/signal, plus the presence correlation."""
    rows = gather(db, household_id)
    if not rows:
        return {"devices": [], "correlation": None,
                "note": "No measurements for this household yet."}

    grouped: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    for r in rows:
        when = _parse(r.get("observed_at"))
        val = r.get("value")
        if when is None or val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        key = (r.get("source") or "unknown", r.get("signal_type") or "unknown")
        grouped.setdefault(key, []).append((when, val))

    devices: list[dict] = []
    power_transitions: list[datetime] = []
    presence_events: list[datetime] = []

    for (source, signal_type), points in sorted(grouped.items()):
        values = [v for _, v in points]
        base: Baseline = compute(source, signal_type, values)
        entry = base.as_dict()
        entry["last_seen"] = max(p[0] for p in points).isoformat()
        entry["latest_value"] = points[-1][1] if points else None
        entry["latest_state"] = base.classify(entry["latest_value"])
        devices.append(entry)

        if signal_type in POWER_TYPES:
            power_transitions.extend(transitions(points, base))
        if signal_type in PRESENCE_TYPES:
            # a presence event is any reading the baseline calls "active"
            presence_events.extend(w for w, v in points if base.classify(v) == "active")

    corr: Correlation = correlate_presence(power_transitions, presence_events)
    return {
        "devices": devices,
        "correlation": corr.as_dict(),
        "sources": sorted({d["source"] for d in devices}),
        "signal_types": sorted({d["signal_type"] for d in devices}),
    }


def usage_vs_typical(db, household_id: str) -> dict:
    """Today's energy against this household's own typical day.

    Deliberately energy, not money: Kroven does not surface prices. 'Typical' is
    the median of the household's own complete days, so it is a comparison
    against itself rather than an external benchmark.
    """
    try:
        rows = (
            db.table("energy_readings")
            .select("recorded_at,kwh_consumed")
            .eq("household_id", household_id)
            .order("recorded_at", desc=True)
            .limit(5000)
            .execute()
            .data
        ) or []
    except Exception:
        rows = []

    if not rows:
        return {"state": "empty", "note": "No readings yet."}

    per_day: dict[str, float] = {}
    for r in rows:
        when = _parse(r.get("recorded_at"))
        try:
            kwh = float(r.get("kwh_consumed"))
        except (TypeError, ValueError):
            continue
        if when is None:
            continue
        per_day[when.date().isoformat()] = per_day.get(when.date().isoformat(), 0.0) + kwh

    today = datetime.now(timezone.utc).date().isoformat()
    today_kwh = per_day.get(today, 0.0)
    prior = [v for d, v in per_day.items() if d != today and v > 0]

    if len(prior) < 2:
        return {
            "state": "building",
            "today_kwh": round(today_kwh, 3),
            "days_logged": len(per_day),
            "note": (f"Only {len(prior)} complete prior day(s) logged. A typical day "
                     f"needs a few more before the comparison means anything."),
        }

    prior.sort()
    mid = len(prior) // 2
    typical = prior[mid] if len(prior) % 2 else (prior[mid - 1] + prior[mid]) / 2
    ratio = (today_kwh / typical) if typical else None

    return {
        "state": "live",
        "today_kwh": round(today_kwh, 3),
        "typical_kwh": round(typical, 3),
        "ratio": round(ratio, 3) if ratio is not None else None,
        "days_compared": len(prior),
        "note": f"Today against the median of {len(prior)} previous logged days.",
    }
