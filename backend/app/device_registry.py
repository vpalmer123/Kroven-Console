"""
The device registry, and the single function that actuates anything.

Everything Kroven can touch is a row in `devices`. Nothing in this module knows
that a PS5 or an extension cord exists — names come from the database (or, until
migration 005 is applied, from the env vars the logger already reads). A new
plug becomes controllable by inserting a row, never by editing code.

    control_device(device_id, action)

is the only actuation path. It takes an opaque device id and one of
on/off/toggle/status, and behaves identically whatever the hardware is: resolve
the row, build the adapter for its `kind`, actuate, then write the observed
state back. There is no per-device branch anywhere in it — the type-specific
part lives in devices.build_adapter().

State is written back after every call, including failures, so the database
records what was actually observed rather than what was requested. A command
that was accepted by the API but did not take effect must not look successful.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from app.db import get_db
from app.devices import DeviceError, build_adapter

logger = logging.getLogger("kroven.registry")

VALID_ACTIONS = ("on", "off", "toggle", "status")

# Above this, a spoken name is accepted as that device. rapidfuzz scores 0-100.
MATCH_THRESHOLD = 0.80
# A match this close to the runner-up is ambiguous no matter how high it scored.
AMBIGUITY_MARGIN = 0.08

# How long to let a load ramp before re-reading power after switching on.
SETTLE_SECONDS = 1.5

# How long to serve env-derived devices after a registry read fails, before
# trying the database again.
TABLE_RETRY_SECONDS = 60
_table_unavailable_until = 0.0


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def list_devices(household_id: str) -> list[dict]:
    """Every device for a household, from the registry, env as fallback."""
    global _table_unavailable_until

    if time.monotonic() >= _table_unavailable_until:
        try:
            rows = (
                get_db().table("devices")
                .select("*")
                .eq("household_id", household_id)
                .order("name")
                .execute()
                .data
            ) or []
            if rows:
                return rows
        except Exception as e:
            # Back off, do not give up. A transient network blip once latched
            # this permanently, so a process would keep serving env-derived
            # devices — wrong names, wrong roles, no persisted state — until it
            # was restarted, long after Supabase had recovered.
            _table_unavailable_until = time.monotonic() + TABLE_RETRY_SECONDS
            logger.warning(
                "devices table unavailable (%s); using env-configured devices "
                "and retrying in %ds.",
                type(e).__name__, TABLE_RETRY_SECONDS,
            )

    # Cached, because these dicts ARE the state store until migration 005 is
    # applied. Rebuilding them per call would throw away every state write-back
    # the moment it was made.
    if household_id not in _env_cache:
        _env_cache[household_id] = _env_devices(household_id)
    return _env_cache[household_id]


_env_cache: dict[str, list[dict]] = {}


def _env_devices(household_id: str) -> list[dict]:
    """Devices implied by the env vars the logger already uses.

    Keeps control working before migration 005 is applied. The names still come
    from configuration (KASA_DEVICE_NAME / SHELLY_DEVICE_NAME), never literals,
    so renaming a plug is a config change and nothing here has to be touched.
    """
    out: list[dict] = []

    kasa_host = os.environ.get("KASA_HOST", "").strip()
    if kasa_host:
        out.append({
            "id": f"env:kasa:{kasa_host}",
            "household_id": household_id,
            "name": os.environ.get("KASA_DEVICE_NAME", "kasa plug"),
            "kind": "kasa",
            "host": kasa_host,
            "channel": 0,
            # One appliance behind this plug, so its trace is that appliance.
            "signal_type": "dedicated",
            "controllable": True,
            "state": None,
            "meta": {"aliases": _aliases("KASA_DEVICE_ALIASES")},
        })

    shelly_host = os.environ.get("SHELLY_HOST", "").strip()
    if shelly_host:
        out.append({
            "id": f"env:shelly:{shelly_host}",
            "household_id": household_id,
            "name": os.environ.get("SHELLY_DEVICE_NAME", "shelly plug"),
            "kind": "shelly",
            "host": shelly_host,
            "channel": int(os.environ.get("SHELLY_CHANNEL", "0")),
            # Shared extension cord: the trace is a sum of several loads.
            "signal_type": "aggregate",
            # Read-only by product decision, not by capability — the hardware
            # switches fine. Env-gated so enabling it is a config change and
            # the default stays off.
            "controllable": os.environ.get("SHELLY_CONTROLLABLE", "").strip().lower()
                            in ("1", "true", "yes"),
            "state": None,
            "meta": {
                "read_only_reason": "aggregate circuit, actuation not enabled yet",
                "aliases": _aliases("SHELLY_DEVICE_ALIASES"),
            },
        })

    return out


def _aliases(var: str) -> list[str]:
    """Comma-separated alternate names from config, e.g. 'playstation,console'."""
    return [a.strip() for a in os.environ.get(var, "").split(",") if a.strip()]


def get_device(household_id: str, device_id: str) -> dict | None:
    for d in list_devices(household_id):
        if str(d["id"]) == str(device_id):
            return d
    return None


def role_rules(household_id: str) -> list[dict]:
    """How to classify a reading's signal_type, including past roles.

    Devices get physically reassigned — a plug moves from the PS5 to a shared
    cord — and when that happens its signal_type flips. Classifying old rows by
    the device's *current* role would silently relabel history: readings logged
    while a plug genuinely carried one appliance would start being treated as a
    mixed trace, corrupting exactly the training data a swap is meant to
    preserve. Nothing is edited, so it looks safe, and the damage only shows up
    as a model quietly learning from the wrong series.

    So a role has an effective date. Readings before it keep the role that was
    true at the time; readings after take the new one.

    Keyed on `kind` rather than `name`, because a reassignment usually renames
    the device too, and `kind` is the part that stays put.
    """
    rules = []
    for d in list_devices(household_id):
        meta = d.get("meta") or {}
        rules.append({
            "kind": (d.get("kind") or "").lower(),
            "name": (d.get("name") or "").lower(),
            "previous_name": (meta.get("previous_name") or "").lower(),
            "current": d.get("signal_type") or "dedicated",
            "previous": meta.get("previous_signal_type"),
            "changed_at": meta.get("role_changed_at"),
        })
    return rules


def signal_type_of(rules: list[dict], source: str, recorded_at: str | None) -> str | None:
    """Signal type for one reading, as of when it was actually recorded.

    The device's name at the time is the strongest evidence and is checked
    first: a logger only starts writing the new name once it has picked up the
    new role, so the name a row carries says which role produced it. The clock
    is the fallback, and it is genuinely fuzzy at the boundary — the role
    changes in the database at one instant, but a logger already mid-cycle can
    write another row under the old role seconds later.
    """
    src = (source or "").lower()
    if not src:
        return None
    parts = src.split(":")

    for r in rules:
        # Kind first, always. A swap hands one device the name the other just
        # gave up, so "PS5" alone can identify two different devices — but a
        # source always leads with its kind, which no swap changes.
        if r["kind"] not in parts and src != r["kind"]:
            continue

        # The OLD name is decisive: a logger still writing under it has not yet
        # picked up the new role, whatever the clock says. This covers the
        # one-row window where the database has flipped mid-poll.
        if r["previous_name"] and r["previous_name"] in parts and r["previous"]:
            return r["previous"]

        # Then the clock, and it must outrank the current name. A device can
        # carry its new name for days before the hardware physically moves —
        # the registry is updated when someone edits it, the plug moves when
        # someone walks over to the wall. Readings in between belong to the old
        # role, and matching on the current name alone would silently relabel
        # every one of them.
        if r["previous"] and r["changed_at"] and recorded_at:
            # String compare is safe here: both are ISO-8601 UTC from Postgres.
            if str(recorded_at) < str(r["changed_at"]):
                return r["previous"]

        if r["name"] and r["name"] in parts:
            return r["current"]
        return r["current"]
    return None


def sources_for(household_id: str, signal_type: str) -> list[str]:
    """Devices *currently* holding one signal_type. Present-tense only.

    Kept for callers that want the live registry view. Do not use it to filter
    historical readings — use role_rules()/signal_type_of(), which know when a
    role changed.
    """
    out = []
    for d in list_devices(household_id):
        if (d.get("signal_type") or "dedicated") == signal_type:
            out.append(f"{d['kind']}:{d['name']}")
            out.append(d["kind"])
    return out


# --------------------------------------------------------------------------
# name resolution
# --------------------------------------------------------------------------

def resolve_device(household_id: str, spoken: str) -> dict[str, Any]:
    """Match what someone said against the real registry.

    Returns {status, device, score, candidates}. status is one of:
      resolved   confident single match
      ambiguous  two devices scored too close to separate
      unknown    nothing scored high enough
      empty      no devices registered at all

    Never guesses: 'unknown' and 'ambiguous' are outcomes the caller is
    expected to turn into a question, because switching the wrong thing in
    someone's house is worse than asking.
    """
    devices = list_devices(household_id)
    if not devices:
        return {"status": "empty", "device": None, "score": 0.0, "candidates": []}

    text = (spoken or "").strip()
    if not text:
        return {"status": "unknown", "device": None, "score": 0.0, "candidates": devices}

    try:
        from rapidfuzz import fuzz
    except ImportError:
        return _resolve_without_rapidfuzz(devices, text)

    scored = []
    for d in devices:
        # Aliases are data, not code: "the playstation" is nowhere near "PS5"
        # lexically, so no fuzzy threshold can bridge it. Storing the words a
        # household actually uses keeps synonyms out of the source.
        labels = [str(d.get("name") or "")]
        labels += [str(a) for a in ((d.get("meta") or {}).get("aliases") or [])]
        # WRatio and token_set_ratio both tolerate the extra words people say
        # ("ps5 plug", "PS 5") without the trap partial_ratio falls into: it
        # scores "the dishwasher" against the alias "the ps5" at 0.85 purely on
        # the word "the", which is why articles are stripped before scoring.
        query = _strip_articles(text)
        score = max(
            max(fuzz.WRatio(query, lb2), fuzz.token_set_ratio(query, lb2))
            for lb2 in (_strip_articles(lb) for lb in labels if lb) if lb2
        ) / 100.0
        scored.append((score, d))

    scored.sort(key=lambda s: s[0], reverse=True)
    best_score, best = scored[0]

    if best_score < MATCH_THRESHOLD:
        return {"status": "unknown", "device": None, "score": best_score,
                "candidates": devices}

    if len(scored) > 1 and (best_score - scored[1][0]) < AMBIGUITY_MARGIN:
        return {"status": "ambiguous", "device": None, "score": best_score,
                "candidates": [d for _, d in scored[:3]]}

    return {"status": "resolved", "device": best, "score": best_score,
            "candidates": devices}


_ARTICLES = re.compile(r"\b(the|my|our|a|an|that|this)\b", re.I)


def _strip_articles(s: str) -> str:
    """Drop words that carry no identity, so they cannot score a match alone."""
    return re.sub(r"\s+", " ", _ARTICLES.sub(" ", s or "")).strip().lower()


def _resolve_without_rapidfuzz(devices: list[dict], text: str) -> dict[str, Any]:
    """Substring fallback so a missing dependency degrades instead of crashing."""
    low = text.lower()
    hits = [d for d in devices if str(d.get("name", "")).lower() in low
            or low in str(d.get("name", "")).lower()]
    if len(hits) == 1:
        return {"status": "resolved", "device": hits[0], "score": 1.0,
                "candidates": devices}
    if len(hits) > 1:
        return {"status": "ambiguous", "device": None, "score": 1.0, "candidates": hits}
    return {"status": "unknown", "device": None, "score": 0.0, "candidates": devices}


# --------------------------------------------------------------------------
# actuation
# --------------------------------------------------------------------------

async def control_device(device_id: str, action: str,
                         household_id: str | None = None) -> dict[str, Any]:
    """Actuate any registered device. The only actuation path in the codebase.

    action: 'on' | 'off' | 'toggle' | 'status'

    Returns {ok, device_id, device, action, state, power_w, detail}. Failures
    come back as ok=False with a readable detail rather than raising, because
    every caller (chat, HTTP, automation) has to explain them to a person.
    """
    action = (action or "").strip().lower()
    household = household_id or os.environ.get("KROVEN_HOUSEHOLD_ID", "").strip()

    if action not in VALID_ACTIONS:
        return _fail(device_id, None, action,
                     f"Unknown action '{action}'. Use one of: {', '.join(VALID_ACTIONS)}.")

    device = get_device(household, device_id) if household else None
    if device is None:
        return _fail(device_id, None, action, "That device is not registered.")

    name = device.get("name") or "device"

    if action != "status" and not device.get("controllable", True):
        reason = (device.get("meta") or {}).get("read_only_reason") or "it is registered read-only"
        return _fail(device_id, device, action,
                     f"{name} cannot be switched: {reason}.")

    try:
        adapter = build_adapter(
            kind=device.get("kind"),
            host=device.get("host") or "",
            channel=int(device.get("channel") or 0),
            label=name,
            meta=device.get("meta") or {},
            # Passed so environment credentials can be restricted to the
            # operator's own household and never used for a paired user.
            household_id=device.get("household_id") or household,
        )
    except DeviceError as e:
        return _fail(device_id, device, action, str(e))

    try:
        if action == "status":
            state = await adapter.get_state()
        elif action == "toggle":
            current = await adapter.get_state()
            state = await adapter.set_switch(not current["on"])
        else:
            state = await adapter.set_switch(action == "on")
    except DeviceError as e:
        _record_state(device, None, None, "error")
        return _fail(device_id, device, action, str(e))
    except Exception as e:
        _record_state(device, None, None, "error")
        return _fail(device_id, device, action, f"{type(e).__name__}: {e}")

    # A plug answers the instant the relay closes, before the load has drawn
    # anything, so a freshly-switched-on device reads 0 W and we would report
    # "turned it on, drawing 0 W". One short settle re-read fixes the number.
    if action != "status" and state.get("on") and not state.get("power_w"):
        try:
            await asyncio.sleep(SETTLE_SECONDS)
            state = await adapter.get_state()
        except Exception:
            pass                                # keep the pre-settle reading

    # Write back what the hardware reported, not what was asked for.
    on = bool(state.get("on"))
    _record_state(device, on, state.get("power_w"),
                  "poll" if action == "status" else "actuation")

    return {
        "ok": True,
        "device_id": device_id,
        "device": name,
        "kind": device.get("kind"),
        "signal_type": device.get("signal_type") or "dedicated",
        "action": action,
        "state": "on" if on else "off",
        "power_w": state.get("power_w"),
        "detail": f"{name} is {'on' if on else 'off'}.",
    }


def _fail(device_id: str, device: dict | None, action: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "device_id": device_id,
        "device": (device or {}).get("name"),
        "kind": (device or {}).get("kind"),
        "signal_type": (device or {}).get("signal_type"),
        "action": action,
        "state": None,
        "power_w": None,
        "detail": detail,
    }


def _record_state(device: dict, on: bool | None, power_w: float | None,
                  source: str) -> None:
    """Persist observed state. Never fire-and-forget an actuation."""
    device_id = str(device.get("id", ""))
    patch = {
        "state": ("on" if on else "off") if on is not None else "unknown",
        "state_source": source,
        "state_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if power_w is not None:
        patch["power_w"] = float(power_w)
    if on is not None:
        patch["last_seen_at"] = patch["state_updated_at"]

    # Env-derived devices have no row to update yet; keep the in-memory result
    # rather than logging a failure every time before migration 005 is applied.
    if device_id.startswith("env:"):
        device.update(patch)
        return

    try:
        get_db().table("devices").update(patch).eq("id", device_id).execute()
    except Exception as e:
        logger.warning("could not persist state for %s: %s", device_id, type(e).__name__)
