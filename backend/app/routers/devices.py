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

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.device_registry import control_device, list_devices, resolve_device

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
        out.append({
            "id": d["id"],
            "name": d["name"],
            "kind": d["kind"],
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
