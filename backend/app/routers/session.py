"""
Sign-up and sign-in.

These proxy Supabase Auth rather than having the browser call it directly.
Direct browser-to-Supabase is the more usual shape and keeps passwords off our
server entirely, but it needs the project's anon key embedded in the page, and
the console is a prebuilt bundle we cannot safely edit. Proxying keeps the
frontend to one small script and puts nothing extra in the page.

The tradeoff is real and worth naming: passwords pass through this service on
their way to Supabase. They are never logged, never stored, and never written
to disk here — the request body goes straight out over TLS — but the hop
exists, which it would not if the browser called Supabase itself. Worth
revisiting if the frontend is ever rebuilt from source.

Sign-up deliberately does not create the household. That happens on first
authenticated request (app.auth.ensure_household), so an account that is
created but never confirmed leaves nothing behind.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

from app.auth import AuthError, ensure_household, verify_token

logger = logging.getLogger("kroven.session")
router = APIRouter()

# Password attempts are the classic brute-force target, so they are capped per
# IP well below what a script needs.
MAX_ATTEMPTS_PER_HOUR = int(os.environ.get("KROVEN_LOGIN_ATTEMPTS", "20") or 20)
_attempts: dict[str, deque[float]] = defaultdict(deque)

# Supabase enforces a minimum too; stating it here gives a better message than
# the raw API error.
MIN_PASSWORD = 8


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (
        request.client.host if request.client else "unknown")


def _throttled(ip: str) -> bool:
    now = time.monotonic()
    q = _attempts[ip]
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= MAX_ATTEMPTS_PER_HOUR:
        return True
    q.append(now)
    return False


def _cfg() -> tuple[str, str]:
    return os.environ.get("SUPABASE_URL", "").rstrip("/"), \
           os.environ.get("SUPABASE_SERVICE_KEY", "")


def _fail(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"ok": False, "detail": detail}, status_code=status)


@router.post("/signup")
async def signup(body: Credentials, request: Request):
    if _throttled(_ip(request)):
        return _fail(429, "Too many attempts. Try again later.")
    if len(body.password) < MIN_PASSWORD:
        return _fail(400, f"Use at least {MIN_PASSWORD} characters for your password.")

    url, key = _cfg()
    if not url or not key:
        return _fail(503, "Accounts are not configured on this server.")

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{url}/auth/v1/signup",
                headers={"apikey": key, "Content-Type": "application/json"},
                json={"email": body.email, "password": body.password},
            )
    except httpx.HTTPError:
        return _fail(503, "Couldn't reach the account service. Try again shortly.")

    data = r.json() if r.content else {}
    if r.status_code >= 400:
        msg = (data.get("msg") or data.get("error_description")
               or data.get("message") or "Could not create that account.")
        # Supabase says "User already registered"; make it actionable.
        if "already" in msg.lower():
            msg = "There's already an account with that email. Try signing in."
        return _fail(r.status_code, msg)

    # With email confirmation on, Supabase returns a user but no session.
    session = data.get("session") or (data if data.get("access_token") else None)
    if not session or not session.get("access_token"):
        return {
            "ok": True,
            "confirm_required": True,
            "detail": "Account created. Check your email for a confirmation link, "
                      "then sign in.",
        }

    return await _session_response(session)


@router.post("/signin")
async def signin(body: Credentials, request: Request):
    ip = _ip(request)
    if _throttled(ip):
        return _fail(429, "Too many attempts. Try again later.")

    url, key = _cfg()
    if not url or not key:
        return _fail(503, "Accounts are not configured on this server.")

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{url}/auth/v1/token",
                params={"grant_type": "password"},
                headers={"apikey": key, "Content-Type": "application/json"},
                json={"email": body.email, "password": body.password},
            )
    except httpx.HTTPError:
        return _fail(503, "Couldn't reach the account service. Try again shortly.")

    if r.status_code >= 400:
        data = r.json() if r.content else {}
        msg = (data.get("error_description") or data.get("msg")
               or data.get("message") or "")
        logger.info("failed sign-in from %s", ip)
        if "not confirmed" in msg.lower():
            return _fail(401, "Confirm your email address first — check your inbox.")
        # Same message for wrong password and unknown email, so the response
        # cannot be used to discover which addresses have accounts.
        return _fail(401, "Email or password is incorrect.")

    return await _session_response(r.json())


async def _session_response(session: dict) -> dict | JSONResponse:
    token = session.get("access_token")
    if not token:
        return _fail(500, "Sign-in succeeded but returned no session.")
    try:
        user = await verify_token(token)
        household = ensure_household(user["id"], user.get("email"))
    except AuthError as e:
        return _fail(503, str(e))

    return {
        "ok": True,
        "access_token": token,
        "refresh_token": session.get("refresh_token"),
        "expires_in": session.get("expires_in"),
        "household_id": household,
        "email": user.get("email"),
    }


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh")
async def refresh(body: RefreshRequest, request: Request):
    """Trade a refresh token for a fresh access token.

    Access tokens last about an hour. Without this, anyone returning to the
    console the next day — from history, a bookmark, a phone that never closes
    tabs — would find themselves signed out, which is not how a signed-in app
    is expected to behave. Nobody clears their cache; they just come back and
    expect to still be there.

    Refresh tokens rotate: Supabase returns a new one each time, and the old
    one stops working. The caller must store what comes back or the next
    refresh fails.
    """
    url, key = _cfg()
    if not url or not key:
        return _fail(503, "Accounts are not configured on this server.")

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{url}/auth/v1/token",
                params={"grant_type": "refresh_token"},
                headers={"apikey": key, "Content-Type": "application/json"},
                json={"refresh_token": body.refresh_token},
            )
    except httpx.HTTPError:
        return _fail(503, "Couldn't reach the account service. Try again shortly.")

    if r.status_code >= 400:
        # Expired or already-rotated token. Not an error worth alarming about;
        # the caller signs in again.
        return _fail(401, "Your session has ended. Sign in again.")

    return await _session_response(r.json())


@router.get("/me")
async def me(authorization: str | None = Header(default=None)):
    """Who the current session belongs to. Used to decide whether to show login."""
    from app.auth import bearer_token
    token = bearer_token(authorization)
    if not token:
        return {"ok": False, "signed_in": False}
    try:
        user = await verify_token(token)
        household = ensure_household(user["id"], user.get("email"))
    except AuthError as e:
        return {"ok": False, "signed_in": False, "detail": str(e)}
    return {"ok": True, "signed_in": True, "email": user.get("email"),
            "household_id": household}
