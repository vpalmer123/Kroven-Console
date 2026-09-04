"""
Device endpoints — read the registry and physically switch real hardware.

GET  /api/devices                    -> registered devices and their live state
POST /api/devices/{device_id}/control -> actuate one device

Every write goes through app.device_registry.control_device, the same function
the chat agent calls. There is deliberately no second actuation path: a device
that behaves one way through chat and another through HTTP is a device nobody
can reason about.

Reports honestly when nothing is registered or a device is unreachable, so the
UI can never imply control it does not have.
"""

import hmac
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.db import get_db
from app.device_registry import control_device, get_device, list_devices, resolve_device

router = APIRouter()


def _require_control_token(supplied: str | None) -> None:
    """Gate physical actuation behind a shared secret.

    Rate limiting bounds abuse that needs volume. Switching off someone's
    console needs exactly one request, so it needs a different control.

    Fails CLOSED: with no KROVEN_CONTROL_TOKEN set, HTTP actuation is refused
    outright rather than left open. An unset secret is a misconfiguration, and
    the safe reading of it is "nobody may switch anything", not "everybody may".

    This gates the HTTP endpoint only. The chat agent calls control_device()
    in-process and is scoped by household, so this does not disable the
    assistant's own ability to act for the household it is talking to.
    """
    expected = os.environ.get("KROVEN_CONTROL_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Device control over HTTP is disabled. Set KROVEN_CONTROL_TOKEN "
                   "on the server to enable it.",
        )
    # Constant-time: a plain == leaks the secret one byte at a time to anyone
    # who can measure response timing across many attempts.
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing control token.")


class ControlRequest(BaseModel):
    action: str                     # on | off | toggle | status
    reason: str | None = None


class ResolveRequest(BaseModel):
    household_id: str
    text: str


@router.get("")
async def get_devices(household_id: str):
    devices = list_devices(household_id)
    if not devices:
        return {
            "configured": False,
            "devices": [],
            "detail": "No devices registered for this household.",
        }

    out = []
    for d in devices:
        state = await control_device(d["id"], "status", household_id)
        meta = d.get("meta") or {}
        out.append({
            "id": d["id"],
            "name": d["name"],
            "kind": d["kind"],
            # The plug itself: real model reported by the vendor at pairing, not
            # a guess. Shown so the user can tell which physical unit this row
            # is when they own more than one.
            "model": meta.get("model"),
            "gen": meta.get("gen"),
            # What the user says is plugged into it. Kroven cannot work this out
            # on its own — see set_appliance below — so it is stored, not
            # inferred, and clearly presented as their label rather than a
            # detection.
            "appliance": meta.get("appliance"),
            "expected_watts": meta.get("expected_watts"),
            # Consumers must not feed aggregate traces to per-device models.
            "signal_type": d.get("signal_type", "dedicated"),
            "controllable": d.get("controllable", True),
            "online": state["ok"],
            "state": state["state"],
            "power_w": state["power_w"],
            "detail": None if state["ok"] else state["detail"],
        })
    return {"configured": True, "devices": out}


@router.post("/{device_id}/control")
async def control(device_id: str, req: ControlRequest, household_id: str,
                  x_kroven_control: str | None = Header(default=None)):
    # Reads are open; anything that moves a relay is not.
    if req.action in ("on", "off", "toggle"):
        _require_control_token(x_kroven_control)
    result = await control_device(device_id, req.action, household_id)
    if not result["ok"]:
        # 404 for "no such device", 502 for hardware that exists but failed.
        code = 404 if "not registered" in result["detail"] else 502
        raise HTTPException(status_code=code, detail=result["detail"])
    return {**result, "reason": req.reason}


class ApplianceRequest(BaseModel):
    appliance: str | None = None
    expected_watts: float | None = None


@router.post("/{device_id}/appliance")
async def set_appliance(device_id: str, req: ApplianceRequest, household_id: str):
    """Record what the user says is plugged into a plug.

    Deliberately a label, not a detection. Working out an appliance from a
    single plug's power trace is a real research problem (non-intrusive load
    monitoring): it needs a classifier trained on labelled examples of that
    appliance, and this household has none — every sample collected so far
    tops out at 30 W, which is a phone charger on an extension cord, not a
    console. Guessing from that would be inventing a capability.

    What the label DOES buy is a sanity check. Once Kroven knows what should be
    connected and roughly what it draws, it can say "this reads 0 W but a PS5
    idles near 40 W, so it is probably not plugged in" — which is the question
    actually being asked, and is answerable from one number.
    """
    from app.device_registry import get_device
    device = get_device(household_id, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="That device is not registered.")

    meta = dict(device.get("meta") or {})
    if req.appliance is not None:
        meta["appliance"] = req.appliance.strip() or None
    if req.expected_watts is not None:
        meta["expected_watts"] = float(req.expected_watts)

    try:
        get_db().table("devices").update({"meta": meta}).eq("id", device_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save: {type(e).__name__}") from e

    device["meta"] = meta
    return {"ok": True, "appliance": meta.get("appliance"),
            "expected_watts": meta.get("expected_watts")}


class AutoShedRequest(BaseModel):
    enabled: bool
    # Must be sent true by the client when enabling. The server does not take
    # `enabled: true` alone as consent — a toggle can be flipped by a stray
    # click, and this cuts power to real hardware.
    consent_acknowledged: bool = False
    consent_version: str | None = None
    idle_watts: float | None = None
    idle_minutes: float | None = None


@router.post("/{device_id}/autoshed")
async def set_autoshed(device_id: str, req: AutoShedRequest, household_id: str):
    """Turn automatic load shedding on or off for ONE device.

    Enabling requires an explicit acknowledgement carrying the version of the
    wording that was shown. Consent to older wording does not carry forward:
    if the copy changes, the user is asked again, because what they agreed to
    is no longer what the feature does.

    Disabling never requires anything. Withdrawing permission must always be
    easier than granting it.
    """
    from app.automation import CONSENT_VERSION, settings_for

    device = get_device(household_id, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="That device is not registered.")

    if req.enabled:
        if not device.get("controllable", False):
            raise HTTPException(
                status_code=409,
                detail=f"{device.get('name')} is read-only, so it cannot be shed.",
            )
        if not req.consent_acknowledged:
            raise HTTPException(
                status_code=400,
                detail="Automatic power cuts need explicit acknowledgement.",
            )
        if req.consent_version != CONSENT_VERSION:
            raise HTTPException(
                status_code=409,
                detail="The terms shown are out of date. Reload and confirm again.",
            )

    meta = dict(device.get("meta") or {})
    current = settings_for(device)
    if req.enabled:
        meta["auto_shed"] = {
            "enabled": True,
            "consented_at": datetime.now(timezone.utc).isoformat(),
            "consent_version": CONSENT_VERSION,
            "idle_watts": float(req.idle_watts or current["idle_watts"]),
            "idle_minutes": float(req.idle_minutes or current["idle_minutes"]),
        }
    else:
        # Keep the record that consent was once given, but switch it off.
        prev = dict(meta.get("auto_shed") or {})
        prev["enabled"] = False
        meta["auto_shed"] = prev

    try:
        get_db().table("devices").update({"meta": meta}).eq("id", device_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save: {type(e).__name__}") from e

    device["meta"] = meta
    return {"ok": True, "auto_shed": meta["auto_shed"]}


@router.post("/{device_id}/restore")
async def restore_power(device_id: str, household_id: str):
    """Manual override: return mains power immediately.

    Deliberately not gated behind the control token. That token protects
    against a stranger switching hardware off; this only ever switches power
    back ON, and making the undo harder to reach than the action is how people
    get stuck with something they cannot re-energise.
    """
    from app.automation import restore
    result = await restore(household_id, device_id, automated=False,
                           reason="manual override from console")
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=result["detail"])
    return result


@router.get("/events")
async def automation_events(household_id: str, limit: int = 25):
    """Audit trail of automated actions, newest first."""
    from app.automation import recent_events
    return {"events": recent_events(household_id, limit)}


@router.post("/resolve")
async def resolve(req: ResolveRequest):
    """Map spoken text to a registered device. Exposed for the UI and testing."""
    match = resolve_device(req.household_id, req.text)
    return {
        "status": match["status"],
        "score": round(match.get("score", 0.0), 3),
        "device": match["device"],
        "candidates": [
            {"id": c["id"], "name": c["name"]} for c in match.get("candidates") or []
        ],
    }
