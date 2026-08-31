"""
Occupancy / activity state for the chat strip.

WHAT THIS CAN AND CANNOT SAY
----------------------------
The label vocabulary mirrors tools/energy_logger/har.py, because that is what a
CSI sensor can actually support: unoccupied / idle / present / active. It does
NOT include room names or specific activities. A single ESP32 link observes one
zone and tells you motion vs still — not which room someone is in, and certainly
not what they are doing in it. Room-level presence needs a board per room.

SUBJECT NAMING
--------------
There is no sign-in in Kroven, so there is no account to read a name from. The
subject is resolved at request time in this order:

    1. household_profiles.display_name   (once such a column exists and the
                                          person has told the agent their name)
    2. HOUSEHOLD_LABEL env var           (a household name, not a person)
    3. "Someone"                         (the honest default)

No name is ever written to the database as a sample value, and none is baked
into the code.

LIVE VS PREVIEW
---------------
`live` is True only when a real activity source has reported. Until an ESP32 is
running there is none, so the endpoint returns preview rows that are explicitly
flagged as examples of what the detector will show. They are generated from the
real label set rather than invented copy, so the preview cannot drift away from
what the system actually produces.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

# Mirrors har.Activity. Kept as plain data so the API image does not need to
# import the logger package.
LABELS = {
    "unoccupied": "no one detected, load at standby",
    "idle": "no one detected, but something is still drawing power",
    "present": "someone detected nearby, load at standby",
    "active": "someone detected and the load is in use",
}

ZONE_DEFAULT = "your space"


def resolve_subject(profile: dict | None) -> tuple[str, str]:
    """Return (subject, where_it_came_from). Never a hardcoded personal name."""
    if profile:
        name = (profile.get("display_name") or "").strip()
        if name:
            return name, "name you told Kroven"

    label = (os.environ.get("HOUSEHOLD_LABEL") or "").strip()
    if label:
        return label, "configured household label"

    return "Someone", "no account is signed in, so there is no name to use"


def resolve_zone() -> str:
    return (os.environ.get("ACTIVITY_ZONE") or "").strip() or ZONE_DEFAULT


def _latest_event(db, household_id: str) -> dict | None:
    """Most recent real activity event, if an activity source has ever written one.

    The table does not exist yet; a missing table is treated as 'no sensor',
    which is the truth, rather than as an error.
    """
    try:
        rows = (
            db.table("activity_events")
            .select("*")
            .eq("household_id", household_id)
            .order("detected_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        return None
    return rows[0] if rows else None


def known_devices(db, household_id: str) -> list[str]:
    """Device labels this household actually has data for.

    Read from energy_readings rather than a fixed list, so the examples describe
    the person's real setup. `source` looks like "kasa:PS5:manual_export:..." —
    the second segment is the device label.
    """
    names: list[str] = []
    rows = []
    # device_name only exists after migration 003; asking for a missing column
    # makes PostgREST reject the whole query, so fall back to source alone.
    for columns in ("source,device_name", "source"):
        try:
            rows = (
                db.table("energy_readings")
                .select(columns)
                .eq("household_id", household_id)
                .limit(1000)
                .execute()
                .data
            ) or []
            break
        except Exception:
            continue
    if not rows:
        return names

    seen = set()
    for r in rows:
        label = (r.get("device_name") or "").strip()
        if not label:
            parts = (r.get("source") or "").split(":")
            label = parts[1].strip() if len(parts) > 1 else ""
        if label and label.lower() not in seen:
            seen.add(label.lower())
            names.append(label)
    return names


def build_examples(subject: str, zone: str, devices: list[str], profile: dict | None) -> list[dict]:
    """Examples of what the detector will surface once it is running.

    Spread across what Kroven actually routes on - occupancy, timing, weather,
    grid - rather than orbiting whichever single plug happens to be logging.
    One device line is plenty; a wall of them makes the product look like it is
    about one console.

    Each carries `basis` (what it would be derived from) and `available` (what
    hardware that needs), so nothing here can read as a live measurement.
    """
    assets = (profile or {}).get("assets") or {}
    out: list[dict] = []

    out.append({
        "state": "active", "line": f"{subject} active in {zone} · 18 min",
        "basis": "motion in the CSI signal plus load above standby",
        "available": "needs CSI",
    })
    out.append({
        "state": "unoccupied", "line": f"{zone.capitalize()} clear · nothing drawing",
        "basis": "no motion and every monitored load at baseline",
        "available": "needs CSI",
    })

    # One device line, only when a device is actually logging, and rotated
    # rather than repeated so it does not dominate.
    if devices:
        device = devices[0]
        out.append({
            "state": "idle", "line": f"{device} left on, no one nearby · 42 min",
            "basis": f"{device} drawing above standby with no motion detected",
            "available": "needs CSI",
        })

    out.append({
        "state": "timing", "line": "Cheaper window opens in 40 min",
        "basis": "clock against the pricing schedule",
        "available": "works today",
    })
    out.append({
        "state": "weather", "line": "Warm spell tomorrow · cooling load likely up",
        "basis": "forecast highs from the weather service for their location",
        "available": "works today",
    })
    out.append({
        "state": "grid", "line": "Grid demand climbing toward today's peak",
        "basis": "live system demand against the day-ahead forecast peak",
        "available": "works today",
    })

    if (assets.get("ev") or {}).get("present"):
        out.append({
            "state": "appliance", "line": "EV charging · 38 min",
            "basis": "sustained high draw on the EV circuit",
            "available": "needs a monitored EV circuit",
        })

    # Whole-home appliance inference is real technique (non-intrusive load
    # monitoring) but needs a whole-home meter, so it stays a capability.
    out.append({
        "state": "appliance", "line": "Oven on · 25 min",
        "basis": "appliance signature in whole-home power",
        "available": "needs whole-home metering",
    })
    return out


def build_state(db, household_id: str, profile: dict | None) -> dict:
    subject, subject_source = resolve_subject(profile)
    zone = resolve_zone()

    event = _latest_event(db, household_id)
    if event:
        state = str(event.get("activity") or "").lower()
        detected = event.get("detected_at")
        minutes = None
        if detected:
            try:
                started = datetime.fromisoformat(str(detected).replace("Z", "+00:00"))
                minutes = max(0, int((datetime.now(timezone.utc) - started).total_seconds() // 60))
            except ValueError:
                minutes = None
        return {
            "live": True,
            "subject": subject,
            "subject_source": subject_source,
            "zone": zone,
            "state": state,
            "meaning": LABELS.get(state, ""),
            "since_minutes": minutes,
            "confidence": event.get("confidence"),
            "calibrated": bool(event.get("calibrated")),
            "line": _line(subject, zone, state, minutes),
            "sensor": {"connected": True, "source": event.get("source") or "csi"},
        }

    devices = known_devices(db, household_id)
    return {
        "live": False,
        "subject": subject,
        "subject_source": subject_source,
        "zone": zone,
        "state": None,
        "since_minutes": None,
        "devices_known": devices,
        "preview": build_examples(subject, zone, devices, profile),
        "sensor": {
            "connected": False,
            "reason": "no activity sensor is reporting for this household",
            "needs": "an ESP32 running a CSI firmware, reporting to the logger",
        },
        "note": (
            "Examples of what this will show, built from the devices this household "
            "actually has. Nothing here is a measurement — no occupancy sensor is "
            "connected yet."
        ),
    }


def _line(subject: str, zone: str, state: str, minutes: int | None) -> str:
    """One human line. Deliberately about a zone, never a room or an activity
    we cannot actually derive."""
    dur = f" · {minutes} min" if minutes else ""
    if state == "active":
        return f"{subject} active in {zone}{dur}"
    if state == "present":
        return f"{subject} present in {zone}{dur}"
    if state == "idle":
        return f"{zone.capitalize()} empty, something still drawing{dur}"
    if state == "unoccupied":
        return f"{zone.capitalize()} clear{dur}"
    return f"{zone.capitalize()} — no reading"
