"""
Replaces netlify/functions/chat.js. The difference: this version pulls
real household data and the latest forecast from the DB and injects it
into the prompt, so agents answer from actual numbers instead of just
whatever the user typed in that turn.

It also runs a tool-use loop. When a device is paired (see app.devices),
Claude gets real tools that read and physically switch that hardware —
this is what makes Kroven an actuator instead of an advisor. When nothing
is paired, no tools are offered at all, so the model has no way to claim
it controlled something it didn't.
"""

import os
import re
from datetime import datetime

import httpx
from fastapi import APIRouter, Header
from pydantic import BaseModel

from app.auth import AuthError, auth_required, require_household
from app.db import get_db
from app.device_registry import control_device, list_devices, resolve_device
from app.actions import (confirm as confirm_action, propose as propose_action,
                         resolve_bare_confirmation)
from app.device_router import classify as classify_device_intent
from app.intent import Domain, classify as classify_intent
from app.usage_stats import (by_device as usage_by_device, fetch as fetch_readings,
                             summarise as summarise_usage)
from app.regional import resolve as resolve_location
from app.security import household_permitted
from app.rate_data import other_period, pricing_at
from app.routers.rates import recommend, RecommendationRequest

router = APIRouter()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"
MAX_TOOL_ROUNDS = 4

# Read-only on purpose. There is deliberately no tool that switches anything:
# dispatch belongs to app.actions, behind a pending action the server has to
# confirm. Giving the model a switch tool would put it back in charge of
# deciding when power gets cut, which is the exact hole the action layer
# exists to close.
TOOLS = [
    {
        "name": "get_device_state",
        "description": (
            "Read the real, current state of one of the household's registered devices: "
            "whether it is on and its live power draw in watts. Omit `device` to read "
            "every device at once. Use this before deciding whether a switch is needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": (
                        "Name of the device, from the registered list you were given. "
                        "Leave empty to read all of them."
                    ),
                },
            },
            "required": [],
        },
    },
]


class ChatRequest(BaseModel):
    household_id: str
    message: str
    device_battery_pct: float | None = None
    # Context gathered in the visitor's own browser. This is what makes the
    # answer about *them* rather than about whatever happens to be seeded in
    # the database for some other household.
    user_data: str | None = None
    device_context: str | None = None
    weather_context: str | None = None
    # Location from the browser as "city|county|state|country", or a
    # "__denied__"/"__unavailable__" sentinel. Never treated as their usage.
    region: str | None = None
    # Prior turns, so Kroven can tell it already offered something and not nag.
    history: list[dict] | None = None


PROFILE_TOOL = {
    "name": "save_home_profile",
    "description": (
        "Save what this person just revealed about their home energy setup, so you never "
        "have to ask again. Call this the moment they mention a monthly kWh figure, a bill "
        "total, or any energy asset — solar, a battery, an EV, a generator, their heating "
        "type. Record only what they actually said or clearly implied; leave everything "
        "else out rather than guessing. Saving is silent — never announce it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "monthly_kwh": {"type": "number", "description": "Monthly kWh they reported."},
            "monthly_bill_usd": {"type": "number", "description": "Monthly bill total in dollars."},
            "home_notes": {
                "type": "string",
                "description": "Short free-text detail, e.g. '3-bed house, electric dryer'.",
            },
            "assets": {
                "type": "object",
                "description": (
                    "Energy assets they revealed. Omit any asset not mentioned — an absent "
                    "key means 'unknown', which is different from present:false. Only set "
                    "present:false when they explicitly said they don't have it."
                ),
                "properties": {
                    "solar": {
                        "type": "object",
                        "properties": {
                            "present": {"type": "boolean"},
                            "size_kw": {"type": "number"},
                            "notes": {"type": "string"},
                        },
                    },
                    "battery": {
                        "type": "object",
                        "properties": {
                            "present": {"type": "boolean"},
                            "capacity_kwh": {"type": "number"},
                            "notes": {"type": "string"},
                        },
                    },
                    "ev": {
                        "type": "object",
                        "properties": {
                            "present": {"type": "boolean"},
                            "charges_per_week": {"type": "number"},
                            "kwh_per_charge": {"type": "number"},
                            "notes": {"type": "string"},
                        },
                    },
                    "generator": {
                        "type": "object",
                        "properties": {
                            "present": {"type": "boolean"},
                            "fuel": {"type": "string"},
                            "notes": {"type": "string"},
                        },
                    },
                    "heating": {
                        "type": "object",
                        "properties": {"type": {"type": "string"}},
                    },
                    "other": {"type": "string"},
                },
            },
        },
        "required": [],
    },
}


def _load_profile(db, household_id: str) -> dict | None:
    """Return this household's saved profile, or None. Missing table is treated
    as 'no profile yet' so the app still works before the migration is run."""
    try:
        rows = (
            db.table("household_profiles")
            .select("*")
            .eq("household_id", household_id)
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        return None
    return rows[0] if rows else None


def _merge_assets(existing: dict | None, incoming: dict | None) -> dict:
    """Fold newly revealed assets into what's already known.

    Two levels deep, because a later turn often adds detail to an asset we
    already know about ("actually it's a 13.5 kWh Powerwall") and that must not
    wipe the rest of the entry.
    """
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            inner = dict(merged[key])
            inner.update({k: v for k, v in value.items() if v is not None})
            merged[key] = inner
        elif value is not None:
            merged[key] = value
    return merged


def _save_profile(db, household_id: str, args: dict) -> tuple[str, bool]:
    row = {"household_id": household_id}
    for key in ("monthly_kwh", "monthly_bill_usd", "home_notes"):
        if args.get(key) is not None:
            row[key] = args[key]

    if args.get("assets"):
        current = _load_profile(db, household_id) or {}
        row["assets"] = _merge_assets(current.get("assets"), args["assets"])

    if len(row) == 1:
        return ("Nothing to save.", True)
    try:
        db.table("household_profiles").upsert(row, on_conflict="household_id").execute()
    except Exception:
        # Table not migrated yet (or a transient write failure). Report success so
        # the model stops retrying and just answers — a failed save must never
        # burn the tool budget and leave the user staring at an empty bubble.
        # It still has the detail in this conversation's context.
        return ("Noted. Do not mention saving; just continue the answer.", False)
    return ("Saved. Do not mention saving; just continue the answer.", False)


async def _run_tool(name: str, args: dict, db, household_id: str) -> tuple[str, bool]:
    """Execute a tool call. Returns (result_text, is_error)."""
    if name == "save_home_profile":
        return _save_profile(db, household_id, args)

    if name == "set_device_switch":
        # Older prompts may still try. Refuse rather than dispatch.
        return ("You cannot switch devices yourself. A control request stages a "
                "pending action that the person must confirm first. Tell them what "
                "would happen and ask them to confirm.", True)
    if name != "get_device_state":
        return (f"Unknown tool: {name}", True)

    # Device errors are written for an engineer reading logs. Anything handed
    # back to the model gets a plain-speech instruction attached, so the
    # protocol detail informs the answer without appearing in it.
    plain = (
        " Explain this to them in plain words: no protocol names, library names, "
        "IP addresses or error codes. A device that cannot be reached can neither be "
        "read nor switched — never imply one of the two still works. If a device "
        "reports OFF while also reporting a power draw above ~1 W, that is stale "
        "telemetry from just after a switch, not standby: say the reading is settling "
        "rather than inventing an explanation for an impossible state."
    )

    devices = list_devices(household_id)
    if not devices:
        return ("No devices are registered, so this action is unavailable.", True)

    spoken = str(args.get("device") or "").strip()

    # No name on a read means "everything", which is what people actually mean
    # when they ask what's on. Only reads get this — a switch must name a target.
    if name == "get_device_state" and not spoken:
        lines = []
        for d in devices:
            r = await control_device(d["id"], "status", household_id)
            lines.append(
                f"{d['name']}: {r['state']}, {r['power_w']} W" if r["ok"]
                else f"{d['name']}: cannot be read right now ({r['detail']})"
            )
        return ("\n".join(lines) + plain, False)

    match = resolve_device(household_id, spoken)
    if match["status"] != "resolved":
        names = ", ".join(str(d["name"]) for d in devices)
        return (
            f"'{spoken}' does not match a registered device. Registered: {names}. "
            f"Ask which one they mean; do not guess.",
            True,
        )

    result = await control_device(match["device"]["id"], "status", household_id)

    if not result["ok"]:
        return (f"Device command failed: {result['detail']}{plain}", True)
    return (f"{result['device']} is {result['state']}, drawing {result['power_w']} W.", False)



def _has_text(payload: dict) -> bool:
    return any(
        b.get("type") == "text" and b.get("text", "").strip()
        for b in payload.get("content") or []
    )


def _text_blocks(payload: dict) -> list[str]:
    return [
        b["text"].strip()
        for b in payload.get("content") or []
        if b.get("type") == "text" and b.get("text", "").strip()
    ]


# "Let me check that." said before a tool call, when a later round then gives
# the real answer. Keeping it produces a reply that announces itself and then
# answers, which reads like a bot narrating its own plumbing.
_FILLER = re.compile(
    r"^(ok(ay)?[,.]?\s*)?(let me|lemme|i'?ll|i am going to|i'?m going to|"
    r"give me a|one|hold on|hang on)\b.{0,40}?"
    r"\b(check|checking|look|looking|see|grab|grabbing|pull|pulling|read|reading|"
    r"fetch|verify|confirm|moment|sec|second)\b",
    re.I,
)

# A measurement is the tell. "Let me check the PS5 — it's drawing 26 W" opens
# like filler but carries the answer, so the opener alone cannot decide.
_MEASUREMENT = re.compile(r"\d+(\.\d+)?\s*(w|kw|kwh|wh|v|a|hz|%|c\b)|\$\s*\d", re.I)


def _drop_filler(chunks: list[str]) -> list[str]:
    """Drop leading 'let me check' lines when a later chunk actually answers."""
    if len(chunks) < 2:
        return chunks

    def is_filler(c: str) -> bool:
        c = c.strip()
        return (
            len(c) <= 160
            and bool(_FILLER.match(c))
            and not _MEASUREMENT.search(c)
        )

    kept = [c for i, c in enumerate(chunks)
            if i == len(chunks) - 1 or not is_filler(c)]
    return kept or chunks[-1:]


def _as_text_payload(payload: dict, chunks: list[str]) -> dict:
    """Rebuild a payload whose content is the text the model produced across all
    tool rounds. The model often speaks *and* calls a tool in the same turn; that
    text is the real answer and must not be dropped when a later round is silent.
    """
    out = dict(payload)
    out["content"] = [{"type": "text", "text": "\n\n".join(_drop_filler(chunks))}]
    out["stop_reason"] = "end_turn"
    return out


async def _final_text_call(client, headers: dict, system_prompt: str, messages: list) -> dict:
    """Last resort: re-ask with no tools available, so the model must reply in words."""
    r = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json={
            "model": MODEL,
            "max_tokens": 1000,
            "system": system_prompt + "\n\nAnswer now, in words. Do not use tools.",
            "messages": messages,
        },
    )
    return r.json()


def _build_messages(req: "ChatRequest") -> list[dict]:
    """Turn the browser's transcript into a valid Anthropic message list.

    Without this the model saw only the newest line, so it could never tell it
    had already offered something and would repeat itself every turn.
    """
    msgs: list[dict] = []
    for m in req.history or []:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            # The API rejects consecutive same-role turns, so fold them together.
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n\n" + content.strip()
            else:
                msgs.append({"role": role, "content": content.strip()})

    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)

    if not msgs or msgs[-1]["role"] != "user":
        msgs.append({"role": "user", "content": req.message})
    elif msgs[-1]["content"].strip() != req.message.strip():
        msgs[-1]["content"] = req.message.strip()

    return msgs[-20:]


def _assistant_says(text: str) -> dict:
    """Shape a plain message the console can render.

    Auth problems come back as an assistant turn rather than a 4xx: the console
    renders whatever it gets, so an HTTP error shows up as a broken bubble
    while this reads as an explanation.
    """
    return {
        "type": "message", "role": "assistant", "model": MODEL,
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
    }


@router.post("")
async def chat(req: ChatRequest, authorization: str | None = Header(default=None)):
    # Accounts, when enabled. The household id in the body is a claim the
    # caller makes, so it is checked against what they actually own — otherwise
    # sending someone else's id is enough to read their data.
    authenticated_user_id = None
    if auth_required():
        try:
            req.household_id = await require_household(authorization, req.household_id)
            from app.auth import bearer_token, verify_token
            tok = bearer_token(authorization)
            if tok:
                authenticated_user_id = (await verify_token(tok)).get("id")
        except AuthError as e:
            return _assistant_says(str(e))

    # Checked before anything else, because everything below this line either
    # queries the database or spends money with Anthropic.
    #
    # Returned as a normal assistant turn rather than an HTTP error: the
    # console renders whatever comes back, so a 403 shows up as a broken bubble
    # while this reads as an answer. Someone opening a shared link should see
    # an explanation, not a failure.
    if not household_permitted(req.household_id):
        return {
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "stop_reason": "end_turn",
            "content": [{
                "type": "text",
                "text": "This is a private preview of Kroven, so the assistant "
                        "is limited to the household it's set up for. Everything "
                        "else on the page works — have a look around.",
            }],
        }

    db = get_db()

    forecast_row = (
        db.table("forecasts")
        .select("*")
        .eq("household_id", req.household_id)
        .order("generated_at", desc=True)
        .limit(1)
        .execute()
    )
    forecast_context = forecast_row.data[0] if forecast_row.data else None
    profile = _load_profile(db, req.household_id)

    # --- intent routing: one agent, different logic per energy domain ---
    route = classify_intent(req.message)
    domain_blocks: list[str] = []

    if route.has(Domain.USAGE, Domain.FORECAST, Domain.COST, Domain.TIMING):
        summary = summarise_usage(fetch_readings(db, req.household_id))
        if summary is not None:
            domain_blocks.append("THEIR OWN LOGGED USAGE:\n" + summary.describe())
            # Readings are tagged with the device that produced them, so
            # per-device questions are answerable. Without this the whole-home
            # total was all Kroven could see, and it said so.
            per_device = usage_by_device(db, req.household_id)
            if per_device:
                domain_blocks.append(per_device)
        else:
            domain_blocks.append(
                "THEIR OWN LOGGED USAGE: nothing is logged for this household yet, so "
                "you have no usage figures to quote. Say so instead of estimating."
            )

    if route.has(Domain.SOLAR):
        domain_blocks.append(
            "SOLAR: no inverter feed is connected, so generation is unknown. A panel "
            "size is not a generation reading. Say you cannot see their output yet."
        )

    if route.has(Domain.BATTERY):
        domain_blocks.append(
            "BATTERY: no battery telemetry is connected, so state of charge is "
            "unknown. Capacity is not charge level. Say you cannot see it yet."
        )

    if route.has(Domain.EV):
        domain_blocks.append(
            "EV: no charger is connected, so you cannot see charging sessions. You "
            "can still answer on timing, which only needs the clock."
        )

    if route.has(Domain.DEVICE):
        domain_blocks.append(
            "DEVICE CONTROL: act only through your tools. With no device paired you "
            "cannot switch anything - say that plainly rather than implying you did."
        )

    rec = recommend(
        RecommendationRequest(
            household_id=req.household_id,
            device_battery_pct=req.device_battery_pct,
        )
    )

    # --- device routing: an LLM classifier decides if this touches hardware ---
    # Keyword rules pick the context blocks above; they are not reliable enough
    # to fire an actuator, because "i'm done gaming" contains no switch verb and
    # "could you ever control this?" contains several.
    # Same inventory the console shows. A seeded row is not something this
    # user connected, so the assistant must not offer to control it either.
    registry = [
        d for d in list_devices(req.household_id)
        if ((d.get("meta") or {}).get("source") or "seeded") == "discovered"
    ]
    device_route = await classify_device_intent(req.message, req.household_id,
                                            req.history or [])

    # The assistant proposes; the server decides. A control request only ever
    # stages a pending action here — dispatch requires app.actions.confirm(),
    # which checks the action id, the user, expiry, single use and whether the
    # device has moved since. The classifier's opinion that a message "sounds
    # like a yes" is not authorization for cutting power to a home.
    if (device_route["category"] == "DEVICE_CONTROL"
            and not device_route["needs_clarification"]
            and not device_route.get("confirming")):
        staged = await propose_action(
            req.household_id, device_route["device_id"], device_route["action"],
            user_id=authenticated_user_id,
        )
        if staged.get("already"):
            domain_blocks.append(
                f"ALREADY IN THAT STATE: {staged['detail']} Say so in one line. "
                f"Nothing was staged and nothing needs confirming."
            )
        elif not staged["ok"]:
            domain_blocks.append(
                f"CANNOT ACT: nothing was staged. Internal reason: {staged['detail']}\n"
                f"Say in one line, plainly, that you can't do it — no protocol names "
                f"or error codes — and never imply anything was switched."
            )
        else:
            domain_blocks.append(
                f"AWAITING CONFIRMATION — NOTHING HAS HAPPENED: a pending action is "
                f"staged to switch {staged['device']} {staged['command']}. It is "
                f"currently {staged['current_state']}.\n"
                f"Physical consequence to convey: {staged['consequence']}\n"
                f"In ONE short line, say what you are about to do, state that "
                f"consequence plainly, and ask them to confirm. It expires in about "
                f"{staged['expires_in_seconds'] // 60} minutes. Do NOT use past tense, "
                f"do NOT say it is done, and do NOT call a tool — you cannot switch "
                f"anything yourself."
            )

    elif device_route["category"] == "DEVICE_CONTROL" and not device_route["needs_clarification"]:
        # An agreement. It still has to resolve to exactly one staged action
        # belonging to this user before anything is dispatched.
        picked = resolve_bare_confirmation(req.household_id, authenticated_user_id)
        if not picked["ok"]:
            domain_blocks.append(
                f"NOTHING TO CONFIRM: {picked['detail']} Say that in one line. Do not "
                f"switch anything and do not treat this as a new command."
            )
            outcome = {"ok": False, "detail": picked["detail"], "_handled": True}
        else:
            outcome = await confirm_action(
                req.household_id, picked["action"]["id"], user_id=authenticated_user_id
            )
        if outcome.get("_handled"):
            pass                    # nothing was staged; already explained above
        elif outcome["ok"] and outcome.get("verified"):
            # Verified means two things agreed: the provider acknowledged the
            # command, AND a fresh read of the device matched. Anything less is
            # reported as uncertain rather than as done.
            #
            # Restoring power is also not switching a device on. Closing a
            # relay re-energises a socket; a console or desktop stays off until
            # someone presses something, and claiming otherwise is how a person
            # finds out hours later that a download never resumed.
            did = ("cut power to" if outcome["command"] == "off"
                   else "restored power to")
            caveat = ("" if outcome["command"] == "off" else
                      " Add that the device itself may still need switching on by "
                      "hand — you restored power, you did not turn the device on. "
                      "Never write 'turned it on'.")
            reading = (
                f" It reads {outcome['power_w']} W."
                if isinstance(outcome.get("power_w"), (int, float))
                else " Do not quote a wattage: the reading contradicts the switch "
                     "state and is still settling."
            )
            domain_blocks.append(
                f"ACTION COMPLETED AND VERIFIED: you {did} the {outcome['device']}. "
                f"A fresh read confirms it is {outcome['state']}.{reading} Tell them "
                f"in one short line, past tense.{caveat} Do not call a tool again."
            )
        elif outcome["ok"]:
            domain_blocks.append(
                f"OUTCOME UNCERTAIN — DO NOT CLAIM SUCCESS: {outcome['detail']}\n"
                f"Say in one line that the command was accepted but you could not read "
                f"the device back, so its state is unconfirmed. Do not say it worked "
                f"and do not say it failed."
            )
        else:
            domain_blocks.append(
                f"ACTION DID NOT COMPLETE: nothing was changed. Internal reason, for "
                f"your understanding only: {outcome['detail']}\n"
                f"Tell them in ONE line that nothing was switched and why, in plain "
                f"speech — never repeat the internal wording and never name a protocol, "
                f"library, encryption scheme, IP address or error code. Never claim it "
                f"worked, and do not retry."
            )
    elif device_route["needs_clarification"] and device_route["category"] in (
        "DEVICE_CONTROL", "STATUS_QUERY"
    ):
        domain_blocks.append(
            f"UNCLEAR DEVICE REQUEST: they seem to mean a device but it is not certain "
            f"which, or whether they want it switched. Ask exactly this, in your own "
            f"voice, and nothing else: \"{device_route['clarification']}\" Do not switch "
            f"anything and do not guess."
        )

    switchable = [d for d in registry if d.get("controllable", True)]
    if registry:
        listing = "; ".join(
            f"{d['name']} ({d['kind']}, "
            f"{'switchable' if d.get('controllable', True) else 'read-only'}, "
            f"{d.get('signal_type', 'dedicated')} signal)"
            for d in registry
        )
        device_note = (
            f"Registered devices for this household: {listing}.\n"
            "Read or switch them with your tools, always by name. Switching cuts or "
            "restores real power to whatever is plugged in, so say what you did and why. "
            "Never name a device that is not on that list, and never switch a read-only "
            "one — say it is monitored but not switchable."
        )
        if not switchable:
            device_note += (
                "\nNothing here is switchable right now, so you can report state but "
                "cannot change it. Say that plainly if they ask you to switch something."
            )
    else:
        device_note = (
            "No devices are registered to this household, so you cannot control anything. "
            "Do not imply that you can switch, toggle, or automate hardware. You may "
            "explain what you would do once a device is connected."
        )

    # Beta coverage is the nine-county Bay Area. The rate engine is a real PG&E
    # schedule, so quoting it outside that area would be confidently wrong.
    # Browser geolocation first, then anything they typed (city or ZIP).
    loc = resolve_location(req.region, req.message)
    status = loc.status

    # What we know about THIS visitor, in priority order: numbers they pasted,
    # then anything stored for their household, then nothing at all.
    if req.user_data:
        data_note = (
            "This person pasted their own usage numbers into the app. Ground the answer "
            "in THESE numbers and cite them directly:\n"
            f"{req.user_data[:4000]}"
        )
    elif forecast_context:
        model_version = (forecast_context or {}).get("model_version", "")
        if model_version == "naive-fallback":
            provenance = (
                "IMPORTANT: this was NOT produced by a trained model. It is the plain "
                "average of their own recent readings, because no model file is loaded. "
                "Describe it as what their recent usage has been averaging. Do NOT call "
                "it a forecast, a prediction, or the output of a model, and never "
                "mention an LSTM."
            )
        else:
            provenance = (
                f"Produced by model version '{model_version}' from their own readings. "
                "You may describe it as a prediction from their usage history."
            )
        data_note = f"Stored figures for this household: {forecast_context}\n{provenance}"
    elif profile:
        data_note = (
            "This person already told you about their home once, and you saved it. Use "
            "these numbers as theirs and answer directly and concretely. Do NOT ask them "
            "for their usage again, and do NOT say you lack their data — you have it.\n"
            f"{ {k: v for k, v in profile.items() if k not in ('id', 'household_id')} }"
        )
    elif status == "outside":
        where = f" ({loc.label})" if loc.label else ""
        data_note = (
            f"This person is outside the service area{where}. The Kroven beta covers the "
            "San Francisco Bay Area only right now, and the rate schedule you were given "
            "only covers the Bay Area — it does not apply to them.\n"
            "Tell them warmly and briefly that the beta is Bay Area-only for now, with "
            "more areas coming soon. Give them NO analytics: no prices, no kWh, no "
            "estimated bills, no savings figures. Do not substitute a national average "
            "and do not invent numbers for their area. Keep it to a couple of sentences "
            "and don't be apologetic about it."
        )
    elif status == "denied":
        data_note = (
            "Their browser is BLOCKING location for this site, so nothing can be detected "
            "automatically — and because permission was already denied, no prompt will "
            "appear no matter what.\n"
            "You need their location before giving any analytics. Ask for it in one "
            "casual line, and give them both options: they can re-enable location for the "
            "site (the icon at the left of the address bar), or just type their city or "
            "ZIP and you'll take it from there. Until you have it, give NO prices, kWh, "
            "or estimated costs — the beta is Bay Area-only and you don't yet know if "
            "they're in it. Answer any part of their question that doesn't need location, "
            "keep it short, and ask once."
        )
    elif status == "unavailable":
        data_note = (
            "Location lookup failed this turn (timed out, or the browser couldn't get a "
            "fix). Nothing is known about where they are.\n"
            "Ask once, casually, for their city or ZIP so you can check whether they're "
            "in the Bay Area beta. Until you have it, give NO prices, kWh, or estimated "
            "costs. Answer whatever part of their question doesn't depend on location."
        )
    elif status == "unknown":
        data_note = (
            "No location for this person yet, so you cannot tell whether they're in the "
            "Bay Area beta service area.\n"
            "If their question needs local analytics (spending, rates, timing, savings), "
            "ask for their location first in ONE casual line — they can allow location "
            "access in the browser, or just type their city or ZIP. Give NO prices, kWh, "
            "or cost figures until you know where they are. If their question doesn't "
            "need location at all, just answer it and don't bring location up."
        )
    else:
        data_note = (
            f"This person is in the Bay Area beta service area"
            f"{f' ({loc.label})' if loc.label else ''}, so the verified pricing you were "
            "given DOES apply — use it freely and concretely for timing, peak windows, "
            "and what anything costs to run.\n"
            "What you do NOT have is any usage figure for them. There is no area average "
            "and there is no fallback: never quote a monthly kWh or a monthly bill total, "
            "never estimate one from housing type or averages, and never borrow another "
            "household's numbers. Rates you know; their consumption you do not.\n"
            "When they ask about rates or timing, the prices are there to use. "
            "that covers most of what people ask. When they ask what they spend overall, "
            "give them what the rates do tell them, then ask once, casually: 'roughly "
            "what do you pay a month?'. Never say 'share your bill', 'provide', or "
            "'submit' — that reads like a form and nobody does it.\n"
            "If you already asked and they brushed it off, never raise it again — but "
            "still answer whatever they just asked, in full. 'Forget that' means drop the "
            "question, never drop the answer.\n"
            "Get sharper from what they DO say instead. People leak their setup constantly "
            "— 'my dryer', 'my EV', 'this apartment', 'we keep it at 68'. Pick that up, "
            "use it, and quietly call save_home_profile so it sticks. Each answer should "
            "feel a little more tailored than the last, without you ever running an "
            "interview."
        )

    known_assets = (profile or {}).get("assets") or {}
    if known_assets:
        assets_note = (
            f"Energy setup they have revealed so far: {known_assets}\n"
            "An asset missing from that list is UNKNOWN, not absent — say nothing about it."
        )
    else:
        assets_note = (
            "Nothing known about their setup yet beyond grid supply. Do not assume they "
            "have solar, a battery, an EV, or a generator — and do not assume they don't."
        )

    live = [b for b in (req.device_context, req.weather_context) if b]
    live_note = "\n".join(live) if live else "No live device or weather reading this turn."

    # Prices only go into the prompt for people the published tariff covers.
    if status == "bay_area":
        now = datetime.now()
        current = pricing_at(now)
        alternate = other_period(now)
        window = (
            f"{current.peak_start.strftime('%I%p').lstrip('0').lower()}-"
            f"{current.peak_end.strftime('%I%p').lstrip('0').lower()}"
        )
        rate_note = (
            f"PRICING: you DO know this area's real rate schedule. Right now it is "
            f"{'the EXPENSIVE part of the day' if current.period == 'peak' else 'a CHEAP part of the day'}"
            f", and the expensive stretch runs {window} daily. Local time is now "
            f"{now.strftime('%I:%M%p').lstrip('0').lower()}.\n"
            "Use it for ADVICE, never as figures. The distinction is the whole rule:\n"
            "- SAY: whether now is a good or bad time to run something, whether to "
            "wait, roughly how long until it gets cheaper, which part of the day is "
            "expensive. Naming the clock hour is fine — 'wait until after 9' is "
            "useful advice, not a price.\n"
            "- NEVER SAY: a per-kWh rate, a dollar amount, cents, a percentage "
            "difference, an amount saved, or a monthly cost. No figures at all.\n"
            "- NEVER name the utility, the tariff, the plan, or a rate schedule, and "
            "never offer to show one. People neither know nor care what it is.\n"
            "You must NOT claim you lack rate information, that you 'only have their "
            "usage', or that pricing is 'on their bill' — that is false, you have it. "
            "Answer the timing question directly instead.\n"
            "If they push for an actual price, say you don't show rate figures, then "
            "give them the timing answer anyway. Never let the no-figures rule turn "
            "into a non-answer."
        )
    else:
        rate_note = (
            "You have NO verified pricing for this person's location. Do not state any "
            "price per kWh, dollar amount, cost comparison, or time-of-day window. "
            "Saying you don't have their rates yet is correct and expected."
        )

    system_prompt = (
        "You are Kroven's Energy Lead agent. Answer using the real data below — "
        "answer in plain English, the way "
        "a person would explain it to a friend. Be concise, no hedging filler.\n\n"
        "ALWAYS REPLY WITH SOMETHING. Every single turn gets real words back. No "
        "instruction here ever means 'stay silent' — if the user waves off one topic, "
        "you drop that topic and answer the rest, you do not go quiet.\n\n"
        "EVERY NUMBER MUST BE SOURCED. This outranks being helpful, being specific, and "
        "sounding confident.\n"
        "- The ONLY numbers you may state are ones given to you below. Prices, kWh, "
        "percentages, savings, forecasts, timings — all of it.\n"
        "- Never estimate, extrapolate, average, or infer a figure that isn't here. A "
        "plausible-looking number is worse than no number, because the user cannot tell "
        "the difference.\n"
        "- 'Typical', 'average', 'roughly', and 'about' do not license inventing a "
        "figure. There are no typical-appliance numbers available to you at all.\n"
        "- You may only do arithmetic on numbers given below or numbers the user "
        "themselves told you. Show the working when you do.\n"
        "- If you don't have a figure, say so plainly in a few words and move on: 'I "
        "don't have your usage yet', 'I don't have a figure for that'. That is a good "
        "answer, not a failure.\n"
        "- Never dress an unknown up as a range, a rough estimate, a typical case, or "
        "'ballpark' to make it feel answered.\n\n"
        "PLAIN LANGUAGE, NOT UTILITY JARGON. Most people don't know their utility's name "
        "or their plan code, and don't care.\n"
        "- Never lead with or name a utility company or a plan code.\n"
        "- Say 'energy costs more between 4 and 9pm'. NEVER name a utility "
        "company, a rate plan, a plan code, or a rate sheet. Those words must not "
        "appear in your reply at all.\n"
        "- If you need to explain why a price is a range, say it depends on their "
        "plan and how much they have used this month. Never say 'baseline "
        "allowance'.\n\n"
        "NOT EVERY MESSAGE IS A QUESTION ABOUT MONEY. Read what they actually "
        "sent before reaching for a number.\n"
        "- A greeting is a greeting. 'hey' gets 'hey, what do you want to know?' "
        "and nothing else. No prices, no windows, no unprompted status report.\n"
        "- Same for thanks, ok, cool, small talk, or anything off-topic: reply "
        "like a person and stop. Quoting rates at someone who said hello is the "
        "single most robotic thing you can do.\n"
        "- Only bring in prices, kWh or timing when the message is genuinely "
        "about cost, usage or when to run something.\n"
        "- Never open a conversation by announcing the current rate. Wait to be "
        "asked.\n\n"
        "TALK LIKE A PERSON, NOT A READOUT. This matters as much as being right. "
        "You are the friend who happens to know this stuff, texting back.\n"
        "- Contractions always. Everyday words: 'costs' not 'incurs', 'about' not "
        "'approximately', 'after 4' not 'from 16:00 onward'.\n"
        "- Opening with a plain 'yeah', 'nah', 'not really' is good. It answers them "
        "before they finish reading.\n"
        "- Work the number into the sentence instead of stacking figures like a "
        "display. One or two numbers is plenty; you do not need every bound.\n"
        "- Match how they write. Lowercase and loose? Be loose back. Careful full "
        "sentences? Match that instead.\n"
        "- Banned because they read like a machine: 'What matters is...', 'Note "
        "that', 'Based on the data', 'In summary', and any sentence that restates "
        "the question before answering it.\n"
        "- Warmth is fine, filler is not. Never add a sentence that carries no "
        "information just to sound friendly.\n"
        "Robotic: 'No - weather does not affect the rate. What matters is the "
        "time of day, as pricing is higher during the afternoon window.'\n"
        "Human: 'Nah, weather barely touches it - it is the timing that gets "
        "you. You are in the cheap stretch now, gets pricey after 4.'\n"
        "Same facts, one of them sounds like a person.\n\n"
        "LENGTH IS THE HARDEST RULE HERE. One or two sentences is the whole answer, "
        "not an opening paragraph.\n"
        "- First sentence: the answer. Add a number only if the question is about "
        "cost or timing AND that number changes what they would do. Plenty of "
        "good replies contain no figures at all.\n"
        "- Answer ONLY what was asked. No adjacent facts, no related tips, no what "
        "they might want next.\n"
        "- Never explain your reasoning or your inputs, and never say why something "
        "is not relevant. If weather does not change the answer, say nothing about "
        "weather at all.\n"
        "- No caveats or disclaimers unless they change the actual number.\n"
        "- No preamble, no sign-off, no offers of further help.\n"
        "- No headers, bullets or tables unless they ask for a breakdown.\n"
        "'Run it now, it is cheaper until 4.' is a complete answer. Anything "
        "longer for that question is a worse answer.\n"
        "Go past two sentences only for a genuinely multi-part question, and even "
        "then keep it to the two or three points that matter.\n\n"
        "RESOLVE, DON'T DEFLECT: vague or open-ended questions still get a real answer. "
        "Pick the most likely reading, say the assumption in a few words ('assuming a "
        "typical electric dryer'), then give a clear "
        "recommendation. Never end a reply by handing the question back — no 'what would "
        "you like to know?', no 'let me know if...'. If something is genuinely ambiguous, "
        "answer the likeliest version fully first, then note the alternative in one line. "
        "Every reply should leave them with an answer, not homework.\n\n"
        f"What you know about this specific person: {data_note}\n\n"
        f"Their energy setup: {assets_note}\n\n"
        f"Live readings from their own device/location: {live_note}\n\n"
        f"{rate_note}\n"
        f"Device control: {device_note}\n"
        + (
            f"\nROUTED TO: {route.label()}. Domain context follows.\n"
            + ("\n\n").join(domain_blocks)
            if domain_blocks else ""
        )
    )

    messages = _build_messages(req)
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }

    spoken: list[str] = []

    async with httpx.AsyncClient(timeout=60) as client:
        for _ in range(MAX_TOOL_ROUNDS):
            body: dict = {
                "model": MODEL,
                "max_tokens": 1000,
                "system": system_prompt,
                "messages": messages,
            }
            # The profile tool is always available; device tools only when real
            # hardware is registered, so the model can never claim control it lacks.
            body["tools"] = [PROFILE_TOOL] + (TOOLS if registry else [])

            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=body,
            )
            payload = r.json()

            if payload.get("type") == "error" or "content" not in payload:
                return payload

            spoken.extend(_text_blocks(payload))

            if payload.get("stop_reason") != "tool_use":
                if spoken:
                    return _as_text_payload(payload, spoken)
                # Finished without saying anything — force a spoken answer.
                return await _final_text_call(client, headers, system_prompt, messages)

            messages.append({"role": "assistant", "content": payload["content"]})
            results = []
            for block in payload["content"]:
                if block.get("type") != "tool_use":
                    continue
                text, is_error = await _run_tool(
                    block.get("name", ""), block.get("input") or {}, db, req.household_id
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": text,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})

        # Tool budget exhausted. Use whatever it already said, or ask once more
        # with no tools so the user never gets a blank message bubble.
        if spoken:
            return _as_text_payload(payload, spoken)
        return await _final_text_call(client, headers, system_prompt, messages)
