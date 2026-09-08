"""Connect a device the user already owns.

The user is asked how they want to connect, never which brand they own. Kroven
offers connection methods — a URL/API endpoint, or a direct local connection —
and works out what is on the other end itself. See app.connectors for why there
is no manufacturer list here, and for how a new kind of hardware is added
without changing a word of what the user sees.

    GET  /api/pair/methods
    POST /api/pair/connect  {"method":"url_api","endpoint":"...","credential":"..."}

Discovery is the part worth automating. A credential alone is not enough to
control anything: each call needs the device's own id, which the user would
otherwise have to hunt for. One identity call turns the endpoint into the full
set of devices, named as the user named them, so they never see an id at all.

Devices are registered to the caller's own household, so connecting is scoped
by the same ownership rule as everything else.

ON STORING THE CREDENTIAL: it is written to devices.meta so the backend can
actuate later, which means it sits in the database in readable form. That table
is RLS-protected and reachable only with the service role, but the honest
framing is that Kroven holds a credential that can switch the user's hardware.
It stays revocable wherever it was issued, which is the mitigation that matters.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import AuthError, auth_required, require_household
from app.connectors import (METHOD_IDS, METHODS, ConnectorError, identify,
                            normalise_endpoint)
from app.db import get_db

logger = logging.getLogger("kroven.pairing")
router = APIRouter()


class ConnectRequest(BaseModel):
    method: str
    endpoint: str = Field(min_length=3, max_length=300)
    # Optional because not every connection method needs one. A device on the
    # local network often has no password at all, and demanding one would make
    # the form lie about what is required.
    credential: str = Field(default="", max_length=512)
    household_id: str | None = None
    # What the user calls this thing. Optional, and blank by default: an empty
    # field means "keep whatever the device calls itself", never a name made up
    # here. Only meaningful when the endpoint holds a single switchable device,
    # because one name cannot describe several.
    label: str | None = Field(default=None, max_length=48)


def _fail(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"ok": False, "detail": detail}, status_code=status)


@router.get("/methods")
async def methods():
    """The ways a device can be connected. Not a catalogue of devices.

    Nothing here names a manufacturer, model or firmware, because the user is
    not being asked to identify their hardware — only to say how Kroven should
    reach it.
    """
    return {"methods": METHODS}


@router.post("/connect")
async def connect(req: ConnectRequest, authorization: str | None = Header(default=None)):
    if req.method not in METHOD_IDS:
        return _fail(400, "That connection method isn't available yet.")

    household = req.household_id
    if auth_required():
        try:
            household = await require_household(authorization, req.household_id)
        except AuthError as e:
            return _fail(401, str(e))
    household = household or os.environ.get("KROVEN_HOUSEHOLD_ID", "").strip()
    if not household:
        return _fail(401, "Sign in to connect a device.")

    endpoint = normalise_endpoint(req.endpoint, req.method)
    if not endpoint:
        return _fail(400, "Enter the address to connect to.")

    try:
        connector, found = await identify(req.method, endpoint, req.credential.strip())
    except ConnectorError as e:
        return _fail(400, str(e))
    except Exception as e:                                  # noqa: BLE001
        logger.error("identify failed: %s", type(e).__name__)
        return _fail(502, "Kroven couldn't complete that connection. Try again in a moment.")

    db = get_db()
    switchable = [d for d in found if d.switchable]
    label = (req.label or "").strip()
    # A single name can only belong to a single device. With more than one
    # discovered, the name is not silently attached to an arbitrary row — it is
    # dropped, and the response says so.
    label_applies = bool(label) and len(switchable) == 1

    saved, skipped = [], []
    for d in found:
        if not d.switchable:
            skipped.append(d.provider_name or "Untitled device")
            continue

        meta = {
            **d.transport,
            "model": d.model,
            "gen": d.gen,
            # How this row came to exist. Only 'discovered' belongs in a real
            # inventory: it means an endpoint was reached and returned this
            # device. Anything else is a fixture and is filtered out of
            # production listings.
            "source": "discovered",
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "connected_via": req.method,
            # Exactly what the device called itself, kept separate from the
            # display name so a user rename never destroys the evidence of what
            # was actually discovered.
            "provider_name": d.provider_name,
            "category": d.category,
            "aliases": [],
            # A name the user typed outranks the device's own on every later
            # rediscovery; a name that came from the device does not.
            "named_by_user": label_applies,
        }
        row = {
            "household_id": household,
            "name": label if label_applies else (d.provider_name or "Untitled device"),
            "kind": d.kind,
            # A LAN address is only stored when the user actually connected
            # over the local network. Storing one from a cloud pairing would
            # make build_adapter prefer a route that only works from inside
            # the house.
            "host": d.host,
            "channel": d.channel,
            "signal_type": "dedicated",
            "controllable": True,
            "state": "unknown",
            "meta": meta,
        }

        # Identity is the device's own id, not its name. Matching on name would
        # create a second row the moment a user renames something, and would
        # collide with an unrelated device that happens to share a name.
        # Rediscovery has to update in place, never duplicate.
        try:
            existing = (
                db.table("devices").select("id,name,meta")
                .eq("household_id", household).eq("kind", d.kind)
                .execute().data
            ) or []
            match = next(
                (e for e in existing
                 if _external_id(e.get("meta")) == d.external_id),
                None,
            )

            if match:
                # Keep whatever the user renamed it to; refresh everything the
                # device itself owns. A name typed on this connection is a
                # deliberate rename and does replace it.
                merged = dict(match.get("meta") or {})
                merged.update(meta)
                patch = {
                    "channel": row["channel"],
                    "controllable": True,
                    "host": d.host,
                    "meta": merged,
                }
                if label_applies:
                    patch["name"] = label
                db.table("devices").update(patch).eq("id", match["id"]).execute()
                saved.append({"name": patch.get("name") or match.get("name") or row["name"],
                              "model": d.model, "online": d.online, "updated": True})
            else:
                db.table("devices").upsert(row, on_conflict="household_id,name").execute()
                saved.append({"name": row["name"], "model": d.model,
                              "online": d.online, "updated": False})
        except Exception as e:                              # noqa: BLE001
            logger.error("could not save %s: %s", row["name"], type(e).__name__)
            return _fail(500, f"Connected, but couldn't save {row['name']}.")

    return {
        "ok": True,
        "connected": saved,
        "skipped": skipped,
        "named": label if label_applies else None,
        "detail": f"Connected {len(saved)} device"
                  f"{'' if len(saved) == 1 else 's'}."
                  + (f" Skipped {len(skipped)} without a switch." if skipped else "")
                  + (f" Named it {label}." if label_applies else "")
                  + (" That endpoint has several devices, so the name you typed "
                     "wasn't used - rename them individually instead."
                     if label and not label_applies else ""),
    }


def _external_id(meta: dict | None) -> str | None:
    """The device's own id, wherever the connector recorded it."""
    m = meta or {}
    return m.get("cloud_device_id") or m.get("local_host")
