"""
LLM intent classifier for the single agent.

app/intent.py already routes by keyword, and still does — it decides which
context blocks get assembled, it is free, and it is right often enough for
that. This module exists for the one decision keywords are not good enough
for: whether a message should actuate real hardware.

"can you kill the ps5", "i'm done gaming", "that thing's been on all night"
all mean turn it off, and none of them contain a switch verb. Getting that
wrong in either direction is expensive — a missed command looks broken, and a
false positive cuts power to something in someone's house. So this one call is
worth its latency.

Four categories, deliberately few:

    DEVICE_CONTROL   switch something now
    STATUS_QUERY     what is on / how much is it drawing
    FORECAST_QUERY   what happens later, when should I run this
    GENERAL_CHAT     everything else

The household's real devices are injected on every call, so the model can only
name hardware that exists. Its answer is then re-checked against the registry by
fuzzy match — the model proposes, the registry disposes. Low confidence or an
unresolved name produces a question, never a guess.
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

from app.device_registry import list_devices, resolve_device

logger = logging.getLogger("kroven.device_router")

MODEL = "claude-sonnet-4-6"
CONFIDENCE_FLOOR = 0.70
TIMEOUT = 12.0

CATEGORIES = ("DEVICE_CONTROL", "STATUS_QUERY", "FORECAST_QUERY", "GENERAL_CHAT")

SYSTEM = """You classify one message from a home energy assistant's user.

Return ONLY a JSON object, no prose, no code fence:
{"category": "...", "device": "...", "action": "...", "confidence": 0.0,
 "confirming": false}

category must be exactly one of:
  DEVICE_CONTROL - they want something switched on or off RIGHT NOW
  STATUS_QUERY   - they want the current state or draw of something
  FORECAST_QUERY - they ask about later, timing, cost ahead, or what to expect
  GENERAL_CHAT   - anything else, including greetings and general questions

device: the device they mean, copied from THEIR DEVICES below. Use the exact
  name from that list when you can tell which one they mean. Use "" if they
  named nothing, or if more than one device would fit equally well.
action: "on", "off", or "toggle" for DEVICE_CONTROL; "" otherwise.
confidence: 0.0-1.0, how sure you are of category AND device together.
confirming: true ONLY when the previous assistant turn asked this user to
  confirm a specific device action and this message agrees to it — "yes",
  "do it", "go ahead", "yeah turn it off". It is false for a fresh command,
  however clearly phrased, and false when nothing was proposed to confirm.
  Earlier turns are provided above for exactly this judgement.

Rules:
  - Only ever name a device from THEIR DEVICES. Never invent one.
  - Intent counts, not phrasing. "i'm done gaming", "kill it", "that can go
    off now" are DEVICE_CONTROL/off. "did you turn it off?" is STATUS_QUERY.
  - Hypotheticals and questions about capability are not commands. "can you
    turn off the ps5?" IS a command; "could you ever control my ps5?" is not.
  - If they mean a device that is not in the list, set device to "" and drop
    confidence below 0.5.
  - Typos and shorthand are normal. Read through them.
  - When genuinely torn between two devices or two categories, say so with a
    confidence below 0.7 rather than picking."""


async def classify(message: str, household_id: str,
                   history: list[dict] | None = None) -> dict:
    """Classify one message against this household's real devices.

    Always returns a dict; never raises. On any failure it degrades to
    GENERAL_CHAT, which routes to ordinary conversation — the safe direction,
    since the cost of missing a command is a re-ask and the cost of a false
    positive is real power switching off.
    """
    devices = list_devices(household_id)
    result = {
        "confirming": False,
        "category": "GENERAL_CHAT",
        "device": None,
        "device_id": None,
        "action": None,
        "confidence": 0.0,
        "needs_clarification": False,
        "clarification": None,
        "devices": devices,
        "source": "fallback",
    }

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not (message or "").strip():
        return result

    try:
        raw = await _ask_model(api_key, message, devices, history or [])
    except Exception as e:
        logger.warning("classifier call failed (%s); treating as chat", type(e).__name__)
        return result

    category = str(raw.get("category") or "").strip().upper()
    if category not in CATEGORIES:
        category = "GENERAL_CHAT"

    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    result.update({
        "confirming": bool(raw.get("confirming")) if category == "DEVICE_CONTROL" else False,
        "category": category,
        "action": (str(raw.get("action") or "").strip().lower() or None),
        "confidence": round(confidence, 2),
        "source": "llm",
    })

    if category not in ("DEVICE_CONTROL", "STATUS_QUERY"):
        return result

    # --- the model proposed a device; the registry decides if it is real ---
    spoken = str(raw.get("device") or "").strip()
    match = resolve_device(household_id, spoken) if spoken else {
        "status": "unknown", "device": None, "score": 0.0, "candidates": devices
    }
    result["match_score"] = round(match.get("score", 0.0), 2)

    if match["status"] == "resolved":
        result["device"] = match["device"]["name"]
        result["device_id"] = match["device"]["id"]

    if confidence < CONFIDENCE_FLOOR or match["status"] != "resolved":
        result["needs_clarification"] = True
        result["clarification"] = _question(match, devices, category, confidence)

    # A control request that cannot name its target is not a control request.
    if category == "DEVICE_CONTROL" and result["action"] not in ("on", "off", "toggle"):
        result["needs_clarification"] = True
        result["clarification"] = result["clarification"] or "Do you want that on or off?"

    return result


def _question(match: dict, devices: list[dict], category: str, confidence: float) -> str:
    """The clarifying question to ask instead of guessing. Plain speech."""
    names = [str(d.get("name")) for d in devices]

    if match["status"] == "empty" or not names:
        return "I don't have any of your devices connected yet, so there's nothing I can switch."

    if match["status"] == "ambiguous":
        options = [str(d.get("name")) for d in match.get("candidates") or []]
        return f"Which one do you mean — {_join(options)}?"

    if match["status"] == "unknown":
        return f"I'm not sure which one you mean. I've got {_join(names)}."

    # Resolved, but the model itself was unsure of the intent.
    target = match["device"]["name"]
    if category == "DEVICE_CONTROL":
        return f"Just to check — you want the {target} switched?"
    return f"Did you mean the {target}?"


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" or {items[-1]}"


async def _ask_model(api_key: str, message: str, devices: list[dict],
                     history: list[dict] | None = None) -> dict:
    listing = "\n".join(
        f"  - {d.get('name')} ({d.get('kind')}, "
        f"{'switchable' if d.get('controllable', True) else 'read-only'})"
        for d in devices
    ) or "  (none registered)"

    # A short tail of the conversation, so a bare "yes" can be understood as
    # agreeing to whatever was just proposed rather than as its own request.
    turns = []
    for m in (history or [])[-4:]:
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"] += "\n\n" + content.strip()
            else:
                turns.append({"role": role, "content": content.strip()[:600]})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    if turns and turns[-1]["role"] == "user":
        turns.pop()

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 200,
                "system": SYSTEM + f"\n\nTHEIR DEVICES:\n{listing}",
                "messages": turns + [{"role": "user", "content": message.strip()}],
            },
        )
    r.raise_for_status()
    payload = r.json()
    text = "".join(
        b.get("text", "") for b in payload.get("content") or []
        if b.get("type") == "text"
    ).strip()
    return _parse_json(text)


def _parse_json(text: str) -> dict:
    """Models occasionally wrap JSON in a fence or a sentence. Dig it out."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}
