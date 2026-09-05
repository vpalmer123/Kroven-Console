"""
The authorization boundary for physical device commands.

Nothing in Kroven may dispatch a provider command directly. A control request
produces a *proposal* — a row in pending_actions — and stops. Dispatch happens
only through confirm(), which requires an action id, the same authenticated
user, an unexpired single-use record, and a device still in the state the
proposal was made against.

This exists because confirmation used to live in the conversation: the model
asked, the user said yes, and a classifier judged whether that counted. That
puts an LLM in charge of deciding whether to cut power to someone's home, and
makes a replayed or mistimed "yes" sufficient to actuate hardware. Wording is
not a control.

The rules, all enforced here rather than in a prompt:

  * single-use      consumed_at is stamped before dispatch, so a confirmation
                    can never be replayed
  * expiring        proposals die on their own; stale intent is not consent
  * user-bound      a confirmation from another account is refused even with
                    the right id
  * device-bound    the device must still be the one described
  * state-bound     if the device moved since the proposal, the proposal no
                    longer describes reality and is invalidated
  * superseded      proposing a new command for a device cancels the old one,
                    so an ambiguous "yes" can never pick the wrong action

Success is never assumed. A dispatch is only reported as done when the provider
acknowledges AND a fresh read agrees; anything else is reported as uncertain,
with the last known state preserved.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import get_db
from app.device_registry import control_device, get_device

logger = logging.getLogger("kroven.actions")

# Long enough to read a sentence and answer, short enough that intent from
# earlier in a session cannot be reused later.
DEFAULT_TTL_SECONDS = int(os.environ.get("KROVEN_ACTION_TTL", "180") or 180)

# Below this, a post-switch power reading is treated as settled rather than
# stale. An open relay passes no current, so anything above it alongside an
# "off" state is a contradiction, not standby.
RESIDUAL_WATTS = 1.0

OPEN_STATUSES = ("proposed",)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(household_id: str, device: dict | None, action: str, *,
           automated: bool = False, power_w: float | None = None,
           reason: str = "", action_id: str | None = None) -> None:
    """Record one transition. Never raises into the caller."""
    try:
        get_db().table("automation_events").insert({
            "household_id": household_id,
            "device_id": str((device or {}).get("id") or ""),
            "device_name": (device or {}).get("name"),
            "action": action,
            "automated": automated,
            "power_w": power_w,
            "reason": (f"[{action_id}] " if action_id else "") + reason,
        }).execute()
    except Exception as e:
        logger.error("AUDIT WRITE FAILED (%s): %s %s", type(e).__name__, action, reason)


def consequence_text(command: str, device_name: str) -> str:
    """What will physically happen, in the words the user is shown."""
    if command == "off":
        return (f"Power will actually be cut to whatever is plugged into "
                f"{device_name}. Anything mid-task can be interrupted.")
    if command == "on":
        # Deliberate: closing a relay re-energises a socket. It does not boot a
        # console, a desktop, or most AV equipment.
        return (f"Mains power will be restored to {device_name}. The device "
                f"itself may still need switching on by hand — this does not "
                f"turn it on.")
    return f"{device_name} will be switched to the opposite of its current state."


# --------------------------------------------------------------------------
# proposing
# --------------------------------------------------------------------------

async def propose(household_id: str, device_id: str, command: str,
                  user_id: str | None = None,
                  ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
    """Create a pending action. Dispatches nothing.

    Returns {ok, action_id, device, command, current_state, consequence,
    expires_at} or {ok: False, detail}.
    """
    if command not in ("on", "off", "toggle"):
        return {"ok": False, "detail": f"'{command}' is not a command I can run."}

    device = get_device(household_id, device_id)
    if device is None:
        return {"ok": False, "detail": "That device is not registered."}
    if not device.get("controllable", False):
        return {"ok": False,
                "detail": f"{device.get('name')} is registered read-only, so it "
                          f"cannot be switched."}

    live = await control_device(device_id, "status", household_id)
    if not live["ok"]:
        _audit(household_id, device, "failed", reason=f"propose: {live['detail']}")
        return {"ok": False, "detail": live["detail"]}

    if live["state"] == command:
        return {"ok": False, "already": True,
                "detail": f"{device.get('name')} is already {command}."}

    db = get_db()

    # A new proposal for this device supersedes any earlier one, so a later
    # "yes" can never resolve to a stale or ambiguous action.
    try:
        db.table("pending_actions").update({
            "status": "cancelled", "resolved_at": _now().isoformat(),
        }).eq("household_id", household_id).eq("device_id", str(device_id)) \
          .eq("status", "proposed").execute()
    except Exception as e:
        logger.warning("could not supersede prior proposals: %s", type(e).__name__)

    row = {
        "household_id": household_id,
        "user_id": user_id,
        "device_id": str(device_id),
        "device_name": device.get("name"),
        "command": command,
        "capability": "switch",
        "expected_state": live["state"],
        "expected_power_w": live.get("power_w"),
        "consequence": consequence_text(command, device.get("name") or "the device"),
        "status": "proposed",
        "expires_at": (_now() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    try:
        saved = db.table("pending_actions").insert(row).execute().data[0]
    except Exception as e:
        logger.error("could not create pending action: %s", type(e).__name__)
        return {"ok": False, "detail": "Could not stage that action. Try again."}

    _audit(household_id, device, "proposed", power_w=live.get("power_w"),
           reason=f"{command} requested; device is {live['state']}",
           action_id=saved["id"])

    return {
        "ok": True,
        "action_id": saved["id"],
        "device": device.get("name"),
        "device_id": str(device_id),
        "command": command,
        "current_state": live["state"],
        "current_power_w": live.get("power_w"),
        "consequence": row["consequence"],
        "expires_at": row["expires_at"],
        "expires_in_seconds": ttl_seconds,
    }


# --------------------------------------------------------------------------
# resolving a confirmation
# --------------------------------------------------------------------------

def open_actions(household_id: str, user_id: str | None = None) -> list[dict]:
    """Unexpired proposals for this household, newest first."""
    try:
        rows = (
            get_db().table("pending_actions").select("*")
            .eq("household_id", household_id).eq("status", "proposed")
            .order("created_at", desc=True).limit(20).execute().data
        ) or []
    except Exception as e:
        logger.warning("could not read pending actions: %s", type(e).__name__)
        return []

    now = _now()
    live = []
    for r in rows:
        try:
            if datetime.fromisoformat(str(r["expires_at"]).replace("Z", "+00:00")) <= now:
                continue
        except Exception:
            continue
        if user_id and r.get("user_id") and str(r["user_id"]) != str(user_id):
            continue
        live.append(r)
    return live


def resolve_bare_confirmation(household_id: str, user_id: str | None = None) -> dict:
    """Turn a bare "yes" into exactly one action, or refuse.

    Valid only when precisely one unexpired proposal is bound to this user. No
    proposals means there is nothing to agree to; several means the agreement
    is ambiguous, and guessing which one would risk switching the wrong thing.
    """
    live = open_actions(household_id, user_id)
    if not live:
        return {"ok": False, "reason": "none",
                "detail": "There's nothing waiting for confirmation."}
    if len(live) > 1:
        names = ", ".join(sorted({r.get("device_name") or "?" for r in live}))
        return {"ok": False, "reason": "ambiguous",
                "detail": f"More than one action is waiting ({names}). "
                          f"Say which one."}
    return {"ok": True, "action": live[0]}


# --------------------------------------------------------------------------
# confirming and dispatching
# --------------------------------------------------------------------------

async def confirm(household_id: str, action_id: str,
                  user_id: str | None = None) -> dict[str, Any]:
    """Dispatch a proposal, if every condition still holds.

    This is the only path to a provider command.
    """
    db = get_db()
    try:
        rows = db.table("pending_actions").select("*").eq("id", action_id).execute().data
    except Exception as e:
        logger.error("could not read action %s: %s", action_id, type(e).__name__)
        return {"ok": False, "detail": "Could not look that action up."}
    if not rows:
        return {"ok": False, "detail": "That action no longer exists."}
    a = rows[0]

    device = get_device(household_id, a["device_id"])

    def close(status: str, detail: str, **extra):
        try:
            db.table("pending_actions").update(
                {"status": status, "resolved_at": _now().isoformat(), **extra}
            ).eq("id", action_id).execute()
        except Exception:
            pass
        _audit(household_id, device, status, reason=detail, action_id=action_id)
        return {"ok": False, "detail": detail, "status": status}

    # --- every binding checked before anything is dispatched ---
    if str(a["household_id"]) != str(household_id):
        logger.warning("action %s confirmed against the wrong household", action_id)
        return {"ok": False, "detail": "That action isn't yours."}
    if user_id and a.get("user_id") and str(a["user_id"]) != str(user_id):
        logger.warning("action %s confirmed by a different user", action_id)
        return {"ok": False, "detail": "That action isn't yours."}
    if a["status"] != "proposed":
        return {"ok": False,
                "detail": f"That action was already {a['status']}."}
    if a.get("consumed_at"):
        return {"ok": False, "detail": "That confirmation was already used."}
    try:
        if datetime.fromisoformat(str(a["expires_at"]).replace("Z", "+00:00")) <= _now():
            return close("expired", "That confirmation timed out. Ask again.")
    except Exception:
        return close("expired", "That confirmation is no longer valid.")
    if device is None:
        return close("cancelled", "That device is no longer registered.")

    # The proposal described a specific situation. If the device has moved
    # since, it no longer does.
    live = await control_device(a["device_id"], "status", household_id)
    if not live["ok"]:
        return close("failed", f"Can't reach {a['device_name']} to carry that out.")
    if live["state"] != a["expected_state"]:
        return close(
            "cancelled",
            f"{a['device_name']} is now {live['state']}, not "
            f"{a['expected_state']} as when you were asked. Nothing was changed — "
            f"ask again if you still want it {a['command']}.",
        )

    # --- consume BEFORE dispatch, so a crash cannot leave it replayable ---
    try:
        db.table("pending_actions").update({
            "status": "confirmed", "consumed_at": _now().isoformat(),
        }).eq("id", action_id).eq("status", "proposed").execute()
    except Exception as e:
        logger.error("could not consume action %s: %s", action_id, type(e).__name__)
        return {"ok": False, "detail": "Could not confirm that action."}

    _audit(household_id, device, "confirmed", power_w=live.get("power_w"),
           reason=f"{a['command']} authorised", action_id=action_id)

    result = await control_device(a["device_id"], a["command"], household_id)
    _audit(household_id, device, "dispatched", power_w=result.get("power_w"),
           reason=f"provider result: {'ok' if result['ok'] else result['detail']}",
           action_id=action_id)

    if not result["ok"]:
        return close("failed", result["detail"], provider_result=result["detail"])

    # --- provider said yes; now check the hardware agrees ---
    verify = await control_device(a["device_id"], "status", household_id)
    fresh_at = _now().isoformat()

    if not verify["ok"]:
        try:
            db.table("pending_actions").update({
                "status": "dispatched", "resolved_at": fresh_at,
                "provider_result": "acknowledged, unverified",
            }).eq("id", action_id).execute()
        except Exception:
            pass
        _audit(household_id, device, "failed", reason="dispatched but unverifiable",
               action_id=action_id)
        return {"ok": True, "verified": False, "state": None,
                "device": a["device_name"], "command": a["command"],
                "detail": f"The command was accepted, but I can't read "
                          f"{a['device_name']} back to confirm it. Its state is "
                          f"uncertain right now.",
                "status": "uncertain"}

    agreed = verify["state"] == a["command"]
    power = verify.get("power_w")
    # An open relay passes no current. A non-zero reading alongside 'off' is
    # stale telemetry from the moment of switching, not standby.
    stale = (verify["state"] == "off" and isinstance(power, (int, float))
             and power > RESIDUAL_WATTS)

    try:
        db.table("pending_actions").update({
            "status": "verified" if agreed else "failed",
            "resolved_at": fresh_at, "verified_state": verify["state"],
            "verified_at": fresh_at, "provider_result": "acknowledged",
        }).eq("id", action_id).execute()
    except Exception:
        pass

    _audit(household_id, device, "verified" if agreed else "failed",
           power_w=None if stale else power,
           reason=f"post-command state {verify['state']}"
                  + (" (power reading stale)" if stale else ""),
           action_id=action_id)

    if not agreed:
        return {"ok": False, "verified": False, "state": verify["state"],
                "device": a["device_name"], "command": a["command"],
                "detail": f"I sent the command but {a['device_name']} still reads "
                          f"{verify['state']}. Nothing is confirmed.",
                "status": "failed"}

    return {
        "ok": True, "verified": True, "status": "verified",
        "device": a["device_name"], "command": a["command"],
        "state": verify["state"],
        # Withheld rather than shown wrong when it contradicts the relay.
        "power_w": None if stale else power,
        "power_stale": stale,
        "verified_at": fresh_at,
        "detail": (f"{a['device_name']} is now {verify['state']}, confirmed by "
                   f"reading the device back."),
    }


async def cancel(household_id: str, action_id: str) -> dict:
    device = get_device(household_id, action_id)
    try:
        get_db().table("pending_actions").update({
            "status": "cancelled", "resolved_at": _now().isoformat(),
        }).eq("id", action_id).eq("household_id", household_id).execute()
    except Exception:
        return {"ok": False, "detail": "Could not cancel that."}
    _audit(household_id, device, "cancelled", reason="cancelled by user",
           action_id=action_id)
    return {"ok": True}


def expire_stale(household_id: str) -> int:
    """Mark timed-out proposals expired so they stop appearing as pending."""
    n = 0
    for a in _all_open(household_id):
        try:
            if datetime.fromisoformat(str(a["expires_at"]).replace("Z", "+00:00")) <= _now():
                get_db().table("pending_actions").update({
                    "status": "expired", "resolved_at": _now().isoformat(),
                }).eq("id", a["id"]).execute()
                _audit(household_id, {"id": a["device_id"], "name": a["device_name"]},
                       "expired", reason="not confirmed in time", action_id=a["id"])
                n += 1
        except Exception:
            continue
    return n


def _all_open(household_id: str) -> list[dict]:
    try:
        return (
            get_db().table("pending_actions").select("*")
            .eq("household_id", household_id).eq("status", "proposed")
            .limit(50).execute().data
        ) or []
    except Exception:
        return []
