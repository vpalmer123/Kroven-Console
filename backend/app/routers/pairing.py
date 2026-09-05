"""
Connect a user's own smart-plug account.

The user authenticates with their vendor, not with us: Kroven never sees a
Shelly password and never proxies a Shelly login. What it holds is a cloud
authorization key the user generates in their own Shelly app, which is
revocable from that app at any time without touching their account.

WHY THERE IS NO "SIGN IN WITH SHELLY" BUTTON
Shelly Cloud exposes no OAuth flow — /oauth/authorize, /oauth/token,
/authorize and /oauth2/authorize all 404 on both the regional server and the
main cloud host. A redirect-based connect is therefore not available to build,
however much nicer it would be. The documented path is the authorization key,
so the flow is: brand button -> short guided step -> paste key -> we discover
their devices for them.

Discovery is the part worth automating. A key alone is not enough to control
anything: each call needs the device id, which is a hex string the user would
otherwise have to hunt for. One list call turns the key into the full set of
devices, named as the user named them, so they never see an id at all.

    POST /api/devices/pair  {"provider":"shelly","auth_key":"...","server":"..."}

Devices are registered to the caller's own household, so pairing is scoped by
the same ownership rule as everything else.

ON STORING THE KEY: it is written to devices.meta so the backend can actuate
later, which means it sits in the database in readable form. That table is
RLS-protected and reachable only with the service role, but the honest framing
is that Kroven holds a credential that can switch the user's hardware. It is
revocable from the Shelly app, which is the mitigation that matters.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import AuthError, auth_required, require_household
from app.db import get_db

logger = logging.getLogger("kroven.pairing")
router = APIRouter()

SUPPORTED = ("shelly",)


class PairRequest(BaseModel):
    provider: str = "shelly"
    auth_key: str = Field(min_length=8, max_length=512)
    server: str = Field(min_length=4, max_length=200)
    household_id: str | None = None


def _fail(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"ok": False, "detail": detail}, status_code=status)


def _normalise_server(raw: str) -> str:
    s = raw.strip().rstrip("/")
    if not s.startswith(("http://", "https://")):
        s = f"https://{s}"
    return s


async def _shelly_devices(server: str, auth_key: str) -> list[dict]:
    """Everything on the user's Shelly account, as the user named it."""
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.post(f"{server}/interface/device/list", data={"auth_key": auth_key})
    if r.status_code in (401, 403):
        raise ValueError("Shelly didn't accept that key. Check it was copied "
                         "in full, and that the server matches the one shown "
                         "next to it in the app.")
    if r.status_code != 200:
        # Anything else is Shelly's problem, not something the user typed.
        raise ValueError("Shelly couldn't be reached just now. Try again in a moment.")
    payload = r.json()
    if not payload.get("isok"):
        raise ValueError("Shelly rejected that key. Check it was copied in full.")

    found = []
    for dev_id, d in ((payload.get("data") or {}).get("devices") or {}).items():
        # Only things with a relay can be switched. A sensor paired here would
        # otherwise show a control that does nothing.
        switchable = (d.get("category") == "relay") or d.get("mode") == "relay"
        raw_name = (d.get("name") or "").strip()
        found.append({
            "cloud_id": dev_id,
            # The vendor's name, verbatim, or nothing. "Untitled device" is a
            # placeholder chosen here, not a name pretending to come from the
            # provider — provider_name records the difference.
            "provider_name": raw_name or None,
            "name": raw_name or "Untitled device",
            "category": d.get("category"),
            "model": d.get("type"),
            "gen": d.get("gen"),
            "channel": int(d.get("channel") or 0),
            "online": bool(d.get("cloud_online")),
            "switchable": switchable,
            "lan_ip": d.get("ip"),
        })
    return found


@router.get("/providers")
async def providers():
    """What can be connected, and how each one is connected.

    `flow` tells the UI which screen to show. 'auth_key' means a guided paste;
    there is deliberately no 'oauth' option because no supported vendor offers
    one today.
    """
    return {
        "providers": [
            {
                "id": "shelly",
                "name": "Shelly",
                "flow": "auth_key",
                "help": "In the Shelly app: Settings → Authorization cloud key → "
                        "Get key. Copy both the key and the server address.",
                "fields": ["server", "auth_key"],
            },
            {
                "id": "kasa",
                "name": "TP-Link Kasa",
                "flow": "unavailable",
                "help": "Newer Kasa plugs use an encryption scheme no open library "
                        "can speak yet, so they can't be connected for control. "
                        "Older models (KP115, HS103) work on the local network.",
                "fields": [],
            },
        ]
    }


@router.post("/pair")
async def pair(req: PairRequest, authorization: str | None = Header(default=None)):
    if req.provider not in SUPPORTED:
        return _fail(400, f"'{req.provider}' can't be connected yet.")

    household = req.household_id
    if auth_required():
        try:
            household = await require_household(authorization, req.household_id)
        except AuthError as e:
            return _fail(401, str(e))
    household = household or os.environ.get("KROVEN_HOUSEHOLD_ID", "").strip()
    if not household:
        return _fail(401, "Sign in to connect a device.")

    server = _normalise_server(req.server)
    try:
        devices = await _shelly_devices(server, req.auth_key.strip())
    except ValueError as e:
        return _fail(400, str(e))
    except httpx.HTTPError:
        return _fail(502, "Couldn't reach Shelly. Try again in a moment.")

    if not devices:
        return _fail(404, "That account has no devices on it yet.")

    db = get_db()
    saved, skipped = [], []
    for d in devices:
        if not d["switchable"]:
            skipped.append(d["name"])
            continue
        row = {
            "household_id": household,
            "name": d["name"],
            "kind": "shelly",
            # No LAN address on purpose: a home address is unreachable from the
            # deployed backend, and storing one would make build_adapter prefer
            # a route that only works from inside the house.
            "host": None,
            "channel": d["channel"],
            "signal_type": "dedicated",
            "controllable": True,
            "state": "unknown",
            "meta": {
                "cloud_server": server,
                "cloud_auth_key": req.auth_key.strip(),
                "cloud_device_id": d["cloud_id"],
                "model": d["model"],
                "gen": d["gen"],
                "paired_via": "shelly_cloud",
                # How this row came to exist. Only 'discovered' belongs in a
                # real inventory: it means the provider was authenticated and
                # returned this device. Anything else is a fixture and is
                # filtered out of production listings.
                "source": "discovered",
                "discovered_at": datetime.now(timezone.utc).isoformat(),
                # Exactly what the vendor called it, kept separate from the
                # display name so a user rename never destroys the evidence of
                # what was actually discovered.
                "provider_name": d["provider_name"],
                "category": d.get("category"),
                "aliases": [],
            },
        }
        # Identity is the provider's device id, not the name. Matching on name
        # would create a second row the moment a user renames a plug in the
        # vendor app, and would collide with an unrelated device that happens
        # to share a name. Rediscovery has to update in place, never duplicate.
        try:
            existing = (
                db.table("devices").select("id,name,meta")
                .eq("household_id", household).eq("kind", "shelly")
                .execute().data
            ) or []
            match = next(
                (e for e in existing
                 if ((e.get("meta") or {}).get("cloud_device_id") == d["cloud_id"])),
                None,
            )

            if match:
                # Keep whatever the user renamed it to; refresh everything the
                # provider owns.
                merged = dict(match.get("meta") or {})
                merged.update(row["meta"])
                db.table("devices").update({
                    "channel": row["channel"],
                    "controllable": True,
                    "host": None,
                    "meta": merged,
                }).eq("id", match["id"]).execute()
                saved.append({"name": match.get("name") or d["name"],
                              "model": d["model"], "online": d["online"],
                              "updated": True})
            else:
                db.table("devices").upsert(row, on_conflict="household_id,name").execute()
                saved.append({"name": d["name"], "model": d["model"],
                              "online": d["online"], "updated": False})
        except Exception as e:
            logger.error("could not save %s: %s", d["name"], type(e).__name__)
            return _fail(500, f"Connected to Shelly but couldn't save {d['name']}.")

    try:
        from app.device_registry import _env_cache
        _env_cache.pop(household, None)
    except Exception:
        pass

    return {
        "ok": True,
        "connected": saved,
        "skipped": skipped,
        "detail": f"Connected {len(saved)} device"
                  f"{'' if len(saved) == 1 else 's'}."
                  + (f" Skipped {len(skipped)} without a switch." if skipped else ""),
    }
