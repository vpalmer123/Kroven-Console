"""
Edge hardening for a public URL.

The API is on the open internet with no login, because the product has no
accounts — a household is a UUID the browser generates. That is workable for a
demo, but it means every endpoint is reachable by anyone who has the link, and
the link has now been shared outside the team.

The expensive one is /api/chat. It calls Anthropic on the project's key with no
ceiling, so an open endpoint is an open budget: a loop from one machine can run
up a bill overnight, and nothing in the app would notice or stop it.

Three layers here, deliberately none of which require the people the link was
shared with to log in — a demo nobody can open is not a fixed demo:

  1. per-IP rate limit      stops one client hammering an endpoint
  2. global daily LLM cap   bounds total spend even from many IPs
  3. request size limit     stops a huge body burning tokens in one call

State is in-process. On a single Railway instance that is exact; if it is ever
scaled out, each instance keeps its own counters and the effective limits
multiply by the instance count. That is a real limitation, not a rounding
error — move to Redis before scaling, and until then keep the instance count
at one.

Everything is configurable, and the defaults are chosen to be invisible to a
person clicking around the console and obvious to a script.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("kroven.security")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# A person exploring the console sends a handful of messages a minute. A script
# sends hundreds. These sit far enough above real use to be unnoticeable.
CHAT_PER_MINUTE = _int("KROVEN_CHAT_PER_MINUTE", 8)
CHAT_PER_HOUR = _int("KROVEN_CHAT_PER_HOUR", 60)
API_PER_MINUTE = _int("KROVEN_API_PER_MINUTE", 120)

# Hard ceiling on model calls per UTC day across every caller. This is the
# backstop that a per-IP limit cannot provide, because IPs are cheap.
CHAT_DAILY_TOTAL = _int("KROVEN_CHAT_DAILY_TOTAL", 1500)

MAX_BODY_BYTES = _int("KROVEN_MAX_BODY_BYTES", 64 * 1024)

# Endpoints that cost money when called.
COSTLY_PREFIXES = ("/api/chat",)

# Endpoints that actuate physical hardware. Called out separately because the
# consequence of abuse is not a bill, it is something switching off in a house.
ACTUATION_MARKERS = ("/control", "/switch")


class _Window:
    """Fixed-window counters keyed by client, trimmed as it goes."""

    def __init__(self):
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        now = time.monotonic()
        q = self._hits[key]
        cutoff = now - window_s
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            return False, int(window_s - (now - q[0])) + 1
        q.append(now)
        return True, 0

    def sweep(self, older_than: float = 3600.0) -> None:
        """Drop idle clients so memory does not grow with unique IPs."""
        now = time.monotonic()
        for key in [k for k, q in self._hits.items() if not q or q[-1] < now - older_than]:
            self._hits.pop(key, None)


_minute = _Window()
_hour = _Window()
_daily_count = 0
_daily_day = ""
_last_sweep = 0.0


def _client_ip(request: Request) -> str:
    """Caller identity, trusting the platform's forwarding header.

    Railway terminates TLS and sets X-Forwarded-For, so the first entry is the
    real client. It is spoofable by anyone talking to the origin directly,
    which is why this is a rate limit and not an access control.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _harden(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _deny(status: int, detail: str, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return _harden(JSONResponse({"detail": detail}, status_code=status, headers=headers))


async def rate_limit_middleware(request: Request, call_next):
    """Bound what any one caller, and everyone together, can spend."""
    global _daily_count, _daily_day, _last_sweep

    path = request.url.path
    if request.method == "OPTIONS" or path in ("/health", "/"):
        # Exempt from limits so uptime checks and preflights are never blocked,
        # but still hardened — an early return that skipped the headers left
        # them off exactly the responses most likely to be probed.
        return _harden(await call_next(request))

    now = time.monotonic()
    if now - _last_sweep > 600:
        _minute.sweep()
        _hour.sweep()
        _last_sweep = now

    ip = _client_ip(request)

    if request.method in ("POST", "PUT", "PATCH"):
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > MAX_BODY_BYTES:
            return _deny(413, "Request body too large.")

    ok, retry = _minute.hit(f"any:{ip}", API_PER_MINUTE, 60)
    if not ok:
        return _deny(429, "Too many requests. Slow down and try again shortly.", retry)

    costly = any(path.startswith(p) for p in COSTLY_PREFIXES)
    if costly:
        ok, retry = _minute.hit(f"chat:{ip}", CHAT_PER_MINUTE, 60)
        if not ok:
            return _deny(429, "You're sending messages faster than Kroven can "
                              "answer. Give it a few seconds.", retry)
        ok, retry = _hour.hit(f"chat:{ip}", CHAT_PER_HOUR, 3600)
        if not ok:
            return _deny(429, "Hourly limit reached for this connection. "
                              "Try again later.", retry)

        today = _today()
        if today != _daily_day:
            _daily_day, _daily_count = today, 0
        if _daily_count >= CHAT_DAILY_TOTAL:
            logger.error("daily model-call cap (%d) reached; refusing further chat calls",
                         CHAT_DAILY_TOTAL)
            return _deny(503, "Kroven has hit its usage limit for today. "
                              "It'll be back tomorrow.")
        _daily_count += 1

    if any(m in path for m in ACTUATION_MARKERS):
        logger.warning("actuation request from %s: %s %s", ip, request.method, path)

    return _harden(await call_next(request))


def allowed_origins() -> list[str]:
    """Origins permitted to call the API from a browser.

    Set KROVEN_ALLOWED_ORIGINS to a comma-separated list in Railway. If it is
    unset this stays open, on purpose: silently denying every origin would take
    the live console down for the people the link was shared with, and a
    security change that breaks the demo gets reverted rather than fixed. The
    rate limits above apply either way, so the money is bounded regardless.
    """
    raw = os.environ.get("KROVEN_ALLOWED_ORIGINS", "").strip()
    if not raw:
        logger.warning(
            "KROVEN_ALLOWED_ORIGINS is not set - any website can call this API "
            "from a visitor's browser. Set it to the console's origin."
        )
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def docs_enabled() -> bool:
    """Whether to publish /docs and /openapi.json.

    FastAPI serves both by default, which hands anyone a complete map of the
    API — including the endpoints that switch hardware. Useful while building,
    not something to leave on a URL that has been shared around.
    """
    return os.environ.get("KROVEN_ENABLE_DOCS", "").strip().lower() in ("1", "true", "yes")
