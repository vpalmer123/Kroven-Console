"""
One insert path for every device.

The Shelly poller hands over a flat row (power_w, voltage_v, current_a, ...).
That does not fit `energy_readings`, which only models kWh, and it definitely
does not fit `household_profiles`, whose primary key is household_id — one row
per household, so a 30-second poller would overwrite the same row forever.

Everything lands in `observations` instead: one row per signal, tagged with the
device it came from. A Shelly reading becomes several observations (power,
voltage, current, energy total, switch state, temperature) rather than being
squeezed into a single column, so nothing measured is thrown away and the
baseline engine can learn from any of it.

`energy_readings` still receives the energy delta, because that is the series
the forecaster already reads.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.db import get_db

logger = logging.getLogger("kroven.writes")

# Which fields of a poller row map to which signal, and their units.
SIGNAL_MAP = {
    "power_w": ("power_w", "W"),
    "voltage_v": ("voltage_v", "V"),
    "current_a": ("current_a", "A"),
    "frequency_hz": ("frequency_hz", "Hz"),
    "energy_wh_total": ("energy_wh_total", "Wh"),
    "temp_c": ("temperature_c", "C"),
}

_last_energy_wh: dict[str, float] = {}


async def insert_device_reading(row: dict, household_id: str | None = None) -> int:
    """Write one poller row as observations. Returns how many were stored."""
    import os

    household = household_id or os.environ.get("KROVEN_HOUSEHOLD_ID", "").strip()
    if not household:
        logger.error("No KROVEN_HOUSEHOLD_ID set; refusing to write unattributed readings")
        return 0

    device = row.get("device") or "unknown"
    source = device if ":" in device else f"shelly:{device}"
    when = row.get("timestamp") or datetime.now(timezone.utc).isoformat()

    records = []
    for field, (signal_type, unit) in SIGNAL_MAP.items():
        value = row.get(field)
        if value is None:
            continue
        records.append({
            "household_id": household,
            "observed_at": when,
            "source": source,
            "signal_type": signal_type,
            "value": float(value),
            "meta": {"unit": unit},
        })

    if row.get("output_on") is not None:
        records.append({
            "household_id": household,
            "observed_at": when,
            "source": source,
            "signal_type": "switch_state",
            "value": 1.0 if row["output_on"] else 0.0,
            "meta": {"unit": "bool"},
        })

    if not records:
        return 0

    db = get_db()
    try:
        db.table("observations").upsert(
            records, on_conflict="household_id,source,signal_type,observed_at"
        ).execute()
    except Exception as e:
        logger.error(
            f"observations insert failed ({type(e).__name__}). If the table is missing, "
            f"run migrations/004_observations.sql. {e}"
        )
        return 0

    _write_energy_delta(db, household, source, row, when)
    return len(records)


def _write_energy_delta(db, household: str, source: str, row: dict, when: str) -> None:
    """Mirror the energy counter into energy_readings as a per-interval delta.

    The forecaster reads that table, and it wants energy used during an
    interval, not a lifetime counter. A counter that goes backwards means the
    device rebooted, so the new value is treated as the interval rather than a
    negative number.
    """
    total_wh = row.get("energy_wh_total")
    if total_wh is None:
        return
    try:
        total_kwh = float(total_wh) / 1000.0
    except (TypeError, ValueError):
        return

    previous = _last_energy_wh.get(source)
    _last_energy_wh[source] = total_kwh
    if previous is None:
        return                                  # first sample only primes it

    delta = total_kwh - previous
    if delta < 0:
        delta = max(total_kwh, 0.0)
    if delta == 0:
        return

    try:
        db.table("energy_readings").insert({
            "household_id": household,
            "recorded_at": when,
            "kwh_consumed": round(delta, 6),
            "source": f"{source}:live",
        }).execute()
    except Exception as e:
        logger.warning(f"energy_readings mirror failed: {type(e).__name__}")
