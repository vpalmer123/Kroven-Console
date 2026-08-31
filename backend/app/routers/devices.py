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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.device_registry import control_device, list_devices, resolve_device

router = APIRouter()


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
async def control(device_id: str, req: ControlRequest, household_id: str):
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
