"""
Access-code exchange for the private preview.

The console is a static page on a public URL, so anything it knows, a visitor
knows — reading the source is enough. A gate implemented purely in the browser
is therefore decoration: it hides the UI without protecting anything behind it.

So the browser never holds the credential that matters. It sends a code here,
and only a correct code gets the household id back. Without the code there is
no id, and without the id chat refuses (see app.security.household_permitted),
so the model is never called and nothing is spent.

That makes the code a real gate rather than a cosmetic one, while keeping the
demo openable by anyone Victor chooses to give it to — no accounts, no signup,
one string to share.

    POST /api/access  {"code": "..."}  ->  {"ok": true, "household_id": "..."}

Set KROVEN_ACCESS_CODE and KROVEN_DEMO_HOUSEHOLD to enable. With the code
unset the endpoint reports that it is disabled and hands nothing out.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("kroven.access")
router = APIRouter()

# A short shared code is guessable if it can be tried indefinitely, so attempts
# are capped well below what a script needs and far above what a person typing
# it wrongly a few times would hit.
MAX_ATTEMPTS_PER_HOUR = int(os.environ.get("KROVEN_ACCESS_ATTEMPTS", "12") or 12)
_attempts: dict[str, deque[float]] = defaultdict(deque)


class AccessRequest(BaseModel):
    code: str


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (
        request.client.host if request.client else "unknown")


def _too_many(ip: str) -> bool:
    now = time.monotonic()
    q = _attempts[ip]
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= MAX_ATTEMPTS_PER_HOUR:
        return True
    q.append(now)
    return False


@router.post("")
async def exchange(req: AccessRequest, request: Request):
    expected = os.environ.get("KROVEN_ACCESS_CODE", "").strip()
    household = os.environ.get("KROVEN_DEMO_HOUSEHOLD", "").strip()

    if not expected or not household:
        return JSONResponse(
            {"ok": False, "detail": "Access codes are not enabled on this server."},
            status_code=503,
        )

    ip = _client_ip(request)
    if _too_many(ip):
        logger.warning("access-code attempts exhausted for %s", ip)
        return JSONResponse(
            {"ok": False, "detail": "Too many attempts. Try again later."},
            status_code=429,
        )

    # Constant-time: a plain == leaks the code one character at a time to
    # anyone who can measure how long the comparison takes.
    if not hmac.compare_digest(req.code.strip(), expected):
        logger.info("bad access code from %s", ip)
        return JSONResponse(
            {"ok": False, "detail": "That code isn't right."}, status_code=401
        )

    logger.info("access granted to %s", ip)
    return {"ok": True, "household_id": household}


@router.get("/enabled")
async def enabled():
    """Lets the gate know whether to show itself at all."""
    on = bool(os.environ.get("KROVEN_ACCESS_CODE", "").strip()
              and os.environ.get("KROVEN_DEMO_HOUSEHOLD", "").strip())
    return {"enabled": on}
