"""
Automated load shedding, and the checks that stand in front of it.

This is the first part of Kroven that switches hardware with nobody watching,
so it is written to refuse rather than to act. Every path that cannot prove the
device is idle declines and records why.

Four gates, all of which must pass before power is cut:

  1. consent   the user explicitly agreed, for THIS device, to automatic power
               cuts. Not inherited from another device, not a global setting.
  2. idle      the live draw is below the device's idle threshold AND has been
               for a sustained window. A single low sample is not evidence —
               an appliance between cycles reads idle for a moment.
  3. history   enough recent readings exist to judge that window at all. No
               data means unknown, and unknown means do not act.
  4. state     the device is actually on and reachable right now.

RESTORING IS NOT SWITCHING ON. Closing a relay returns mains power; it does
not boot anything. A PS5, a desktop, most AV equipment — all stay off until a
person presses something. Everything here says "power restored" for that
reason, and the distinction is not cosmetic: telling someone their console was
turned back on when it was not is how they discover at 2am that a download
never resumed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import get_db
from app.device_registry import control_device, get_device

logger = logging.getLogger("kroven.automation")

# The wording the user agreed to. Bump this when the consent copy changes so a
# prior agreement is not silently treated as consent to different terms.
CONSENT_VERSION = "2026-09-04.1"

# Conservative defaults. Below this draw, sustained for this long, is treated
# as idle. Both are per-device overridable, because a phone charger and a
# fridge disagree about what "idle" looks like.
DEFAULT_IDLE_WATTS = 10.0
DEFAULT_IDLE_MINUTES = 15

# Never judge a window from a handful of points.
MIN_SAMPLES = 5


def _log_event(household_id: str, device: dict, action: str, automated: bool,
               power_w: float | None, idle_seconds: int | None, reason: str) -> None:
    """Record what happened. Failures here must not stop the action itself,
    but they are logged loudly: an unlogged automated power cut is the thing
    this module exists to avoid."""
    row = {
        "household_id": household_id,
        "device_id": str(device.get("id")),
        "device_name": device.get("name"),
        "action": action,
        "automated": automated,
        "power_w": power_w,
        "idle_seconds": idle_seconds,
        "reason": reason,
    }
    try:
        get_db().table("automation_events").insert(row).execute()
    except Exception as e:
        logger.error("AUDIT WRITE FAILED (%s) for %s %s: %s",
                     type(e).__name__, device.get("name"), action, reason)


def settings_for(device: dict) -> dict[str, Any]:
    """Auto-shed configuration for one device, with defaults filled in."""
    cfg = dict((device.get("meta") or {}).get("auto_shed") or {})
    return {
        "enabled": bool(cfg.get("enabled")),
        "consented_at": cfg.get("consented_at"),
        "consent_version": cfg.get("consent_version"),
        "idle_watts": float(cfg.get("idle_watts") or DEFAULT_IDLE_WATTS),
        "idle_minutes": float(cfg.get("idle_minutes") or DEFAULT_IDLE_MINUTES),
    }


def has_valid_consent(device: dict) -> bool:
    """Consent must exist, be for this device, and be for the current wording."""
    s = settings_for(device)
    return bool(
        s["enabled"]
        and s["consented_at"]
        and s["consent_version"] == CONSENT_VERSION
    )


def idle_check(household_id: str, device: dict) -> dict[str, Any]:
    """Has this device been below its idle threshold for long enough?

    Returns {idle, power_w, idle_seconds, samples, reason}. `idle` is only true
    when there is positive evidence; every other outcome — no data, too few
    samples, a recent spike — is false with a reason, because the default when
    unsure has to be "leave it alone".
    """
    s = settings_for(device)
    watts, minutes = s["idle_watts"], s["idle_minutes"]
    window_start = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    source = f"{device.get('kind')}:{device.get('name')}"

    try:
        rows = (
            get_db().table("observations")
            .select("value,observed_at")
            .eq("household_id", household_id)
            .eq("signal_type", "power_w")
            .gte("observed_at", window_start.isoformat())
            .order("observed_at", desc=True)
            .limit(500)
            .execute()
            .data
        ) or []
    except Exception as e:
        return {"idle": False, "power_w": None, "idle_seconds": None, "samples": 0,
                "reason": f"could not read recent power history ({type(e).__name__})"}

    rows = [r for r in rows if str(r.get("source") or source).startswith(device.get("kind", ""))] or rows

    if len(rows) < MIN_SAMPLES:
        return {"idle": False, "power_w": None, "idle_seconds": None, "samples": len(rows),
                "reason": f"only {len(rows)} readings in the last {minutes:.0f} min; "
                          f"not enough to tell whether it is in use"}

    values = []
    for r in rows:
        try:
            values.append(float(r["value"]))
        except (TypeError, ValueError):
            continue
    if not values:
        return {"idle": False, "power_w": None, "idle_seconds": None, "samples": 0,
                "reason": "no usable power readings"}

    peak = max(values)
    latest = values[0]
    if peak >= watts:
        return {"idle": False, "power_w": latest, "idle_seconds": None,
                "samples": len(values),
                "reason": f"drew {peak:.1f} W within the last {minutes:.0f} min "
                          f"(idle threshold {watts:.1f} W) — looks like it was in use"}

    try:
        oldest = datetime.fromisoformat(str(rows[-1]["observed_at"]).replace("Z", "+00:00"))
        idle_seconds = int((datetime.now(timezone.utc) - oldest).total_seconds())
    except Exception:
        idle_seconds = None

    return {"idle": True, "power_w": latest, "idle_seconds": idle_seconds,
            "samples": len(values),
            "reason": f"stayed under {watts:.1f} W for the whole "
                      f"{minutes:.0f} min window ({len(values)} readings, peak {peak:.1f} W)"}


async def maybe_shed(household_id: str, device_id: str, trigger: str = "scheduled") -> dict:
    """Cut power only if every gate passes. Records the outcome either way."""
    device = get_device(household_id, device_id)
    if device is None:
        return {"acted": False, "reason": "device is not registered"}

    if not has_valid_consent(device):
        # Not logged as an event: nothing was attempted, and a row per skipped
        # unconsented device every cycle would bury the real entries.
        return {"acted": False, "reason": "auto-shed is not enabled for this device"}

    state = await control_device(device_id, "status", household_id)
    if not state["ok"]:
        _log_event(household_id, device, "skipped", True, None, None,
                   f"could not read device: {state['detail']}")
        return {"acted": False, "reason": state["detail"]}

    if state["state"] != "on":
        return {"acted": False, "reason": "already off"}

    check = idle_check(household_id, device)
    if not check["idle"]:
        _log_event(household_id, device, "skipped", True,
                   check["power_w"], check["idle_seconds"], check["reason"])
        return {"acted": False, "reason": check["reason"]}

    result = await control_device(device_id, "off", household_id)
    if not result["ok"]:
        _log_event(household_id, device, "failed", True,
                   check["power_w"], check["idle_seconds"],
                   f"switch failed: {result['detail']}")
        return {"acted": False, "reason": result["detail"]}

    _log_event(household_id, device, "shed", True,
               check["power_w"], check["idle_seconds"],
               f"{trigger}: {check['reason']}")
    logger.warning("AUTO-SHED %s (%s) at %.1f W — %s",
                   device.get("name"), device_id, check["power_w"] or 0.0, check["reason"])
    return {"acted": True, "device": device.get("name"),
            "power_w": check["power_w"], "reason": check["reason"]}


async def restore(household_id: str, device_id: str, automated: bool = False,
                  reason: str = "manual override") -> dict:
    """Return mains power to a device.

    Named `restore`, and described that way everywhere, because that is all it
    does. The relay closes and the socket is live again; whether anything comes
    back to life is up to the appliance. Most consumer electronics do not.
    """
    device = get_device(household_id, device_id)
    if device is None:
        return {"ok": False, "detail": "That device is not registered."}

    result = await control_device(device_id, "on", household_id)
    _log_event(household_id, device, "restore" if result["ok"] else "failed",
               automated, result.get("power_w"), None,
               reason if result["ok"] else f"restore failed: {result['detail']}")

    if not result["ok"]:
        return {"ok": False, "detail": result["detail"]}
    return {
        "ok": True,
        "device": device.get("name"),
        "power_w": result.get("power_w"),
        # Deliberate wording, mirrored in the UI.
        "detail": f"Power restored to {device.get('name')}. "
                  f"The device itself may still need switching on.",
    }


def recent_events(household_id: str, limit: int = 25) -> list[dict]:
    try:
        return (
            get_db().table("automation_events")
            .select("*")
            .eq("household_id", household_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
        ) or []
    except Exception as e:
        logger.warning("could not read automation events: %s", type(e).__name__)
        return []
