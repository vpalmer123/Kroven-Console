"""
Intent routing inside the single agent.

Kroven is one agent, not a supervisor handing off to sub-agents. What varies is
which domain logic runs before the model is called: a question about the EV
should assemble different context from a question about when to run the dryer.
This module decides that, and it is the thing the old "Supervisor: routing…"
copy pretended to do.

Deliberately rule-based, not a classifier call:
  * it adds no latency and no token cost, where a routing LLM call would roughly
    double both on every single message,
  * the decision is inspectable and testable, so a wrong route can be fixed by
    reading the code rather than re-prompting,
  * the domains are few and lexically distinct, which is exactly the case where
    keywords beat a model.

Multiple domains can fire at once — "should I charge the car now" is EV *and*
timing — and the caller assembles every matching context block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Domain(str, Enum):
    TIMING = "timing"          # when to run something, is now good
    USAGE = "usage"            # their own consumption, history, highs and lows
    COST = "cost"              # bills, spend, savings
    SOLAR = "solar"
    BATTERY = "battery"
    EV = "ev"
    DEVICE = "device"          # control a plug, what is on
    WEATHER = "weather"
    FORECAST = "forecast"      # what happens next
    SMALLTALK = "smalltalk"    # greetings, thanks, who are you
    GENERAL = "general"        # nothing matched


# Ordered so more specific patterns win ties.
PATTERNS: list[tuple[Domain, str]] = [
    (Domain.SMALLTALK, r"^\s*(hey|hi|hello|yo|sup|thanks|thank you|ty|ok|okay|cool|nice|got it|nvm|never mind)\b[\s!.?]*$"),
    (Domain.SMALLTALK, r"\b(who are you|what are you|what can you do|how does this work)\b"),

    (Domain.DEVICE, r"\b(turn (it |the .{0,14})?(on|off)|switch (it |the .{0,14})?(on|off)|shut (it |the .{0,14})?(off|down)|unplug|plug (it )?in|is (it|the .{0,14}) (on|off)|smart plug|outlet)\b"),
    (Domain.EV, r"\b(ev|electric car|my car|charge the car|charging the car|tesla|charger)\b"),
    (Domain.SOLAR, r"\b(solar|panels?|pv|inverter|export|net meter\w*|curtail\w*)\b"),
    (Domain.BATTERY, r"\b(batter\w+|powerwall|storage|state of charge|soc|discharg\w+)\b"),

    (Domain.WEATHER, r"\b(weather|temperature|degrees|rain\w*|wind\w*|heat wave|humid\w*|outside|(is it|its|it\'s|so|really|pretty) (hot|cold|warm|chilly)|how (hot|cold|warm))\b"),
    (Domain.FORECAST, r"\b(forecast\w*|predict\w*|expect\w*|tomorrow|later today|rest of (the )?(day|week)|next (hour|week|month)|going to (use|cost)|trend\w*)\b"),

    (Domain.USAGE, r"\b(usage|used|using|consumption|kwh|how much (power|energy|electricity)|my data|readings?|highest|lowest|peak usage|average|typical for me|history|last (night|week|month)|left .{0,18}(on|running)|all (night|day))\b"),
    (Domain.COST, r"\b(bill|cost\w*|spend\w*|spent|paying|pay|price|expensive|cheap\w*|save|saving\w*|money|rate|add(s)? up|worth it)\b"),
    (Domain.TIMING, r"\b(when|now or later|should i (run|wait|do)|good time|bad time|right now|wait|later|tonight|window|before \d|after \d|what time)\b"),
]


@dataclass(frozen=True)
class Route:
    domains: list[Domain]
    matched: dict[str, str]     # domain -> the text that triggered it

    @property
    def primary(self) -> Domain:
        return self.domains[0] if self.domains else Domain.GENERAL

    def has(self, *domains: Domain) -> bool:
        return any(d in self.domains for d in domains)

    def label(self) -> str:
        """Short human string, e.g. for the activity log."""
        return " + ".join(d.value for d in self.domains) if self.domains else "general"


def classify(message: str) -> Route:
    """Route one message to the domains whose logic should run."""
    text = (message or "").strip().lower()
    if not text:
        return Route([Domain.GENERAL], {})

    hits: list[Domain] = []
    matched: dict[str, str] = {}

    for domain, pattern in PATTERNS:
        m = re.search(pattern, text)
        if m and domain not in hits:
            hits.append(domain)
            matched[domain.value] = m.group(0).strip()

    # Smalltalk is exclusive: "hey" should not also drag in pricing context.
    if Domain.SMALLTALK in hits:
        return Route([Domain.SMALLTALK], {"smalltalk": matched.get("smalltalk", "")})

    if not hits:
        return Route([Domain.GENERAL], {})

    # A cost or usage question about "when" is really a timing question too, but
    # timing should not outrank the thing being asked about.
    hits.sort(key=lambda d: _PRIORITY.get(d, 50))
    return Route(hits, matched)


_PRIORITY = {
    Domain.DEVICE: 0,
    Domain.EV: 10,
    Domain.SOLAR: 10,
    Domain.BATTERY: 10,
    Domain.USAGE: 20,
    Domain.FORECAST: 25,
    Domain.COST: 30,
    Domain.WEATHER: 35,
    Domain.TIMING: 40,
    Domain.GENERAL: 90,
}
