"""
Account authentication, and the ownership check that makes isolation real.

Kroven is multi-tenant now: anyone can sign up, and each account gets its own
household. The rule is simply that you may touch your own data and nobody
else's — but where that rule is *enforced* matters.

Migration 008 expresses it as RLS policies, which is the right backstop.
However the backend connects with the service role key, and service_role
bypasses RLS entirely, so those policies protect a browser talking to Supabase
directly and do nothing for requests coming through this API. Since every
request comes through this API, the enforcement that actually counts is here.

So: a request carries a Supabase access token, this module turns it into a
user id, and the caller checks that the household being asked about is one that
user owns. A household id in the request body is a claim, not a credential —
before accounts, sending someone else's id was enough to read their data.

Tokens are verified by asking Supabase, not by decoding locally. Local HS256
verification would be faster but needs the project's JWT secret deployed as
another secret to leak, and gets subtly wrong the things that matter — expiry,
revocation, users deleted mid-session. Results are cached briefly so a burst of
requests from one page load is a single verification.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from app.db import get_db

logger = logging.getLogger("kroven.auth")

# Long enough to collapse one page's burst of calls, short enough that a
# deleted or signed-out user stops working promptly.
TOKEN_CACHE_SECONDS = 60
_token_cache: dict[str, tuple[float, dict]] = {}
_household_cache: dict[str, tuple[float, list[str]]] = {}


class AuthError(Exception):
    """Raised when a token is absent, malformed, expired, or rejected."""


def _base() -> str:
    return os.environ.get("SUPABASE_URL", "").rstrip("/")


def bearer_token(header_value: str | None) -> str | None:
    """Pull the token out of an Authorization header, if there is one."""
    if not header_value:
        return None
    parts = header_value.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


async def verify_token(token: str) -> dict[str, Any]:
    """Exchange an access token for the user it belongs to.

    Raises AuthError for anything that is not a live, valid session.
    """
    if not token:
        raise AuthError("No access token supplied.")

    hit = _token_cache.get(token)
    now = time.monotonic()
    if hit and now < hit[0]:
        return hit[1]

    url, key = _base(), os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise AuthError("Authentication is not configured on this server.")

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                f"{url}/auth/v1/user",
                headers={"apikey": key, "Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as e:
        # Deliberately not treated as "signed in": failing open here would let
        # anyone in whenever Supabase blips.
        raise AuthError(f"Could not verify the session ({type(e).__name__}).") from e

    if r.status_code != 200:
        raise AuthError("Your session has expired. Sign in again.")

    user = r.json()
    if not user.get("id"):
        raise AuthError("That session is not valid.")

    _token_cache[token] = (now + TOKEN_CACHE_SECONDS, user)
    if len(_token_cache) > 2000:            # bound memory on a public endpoint
        for k in list(_token_cache)[:1000]:
            _token_cache.pop(k, None)
    return user


def households_for(user_id: str) -> list[str]:
    """Household ids this user owns, best first.

    Order matters and used not to be specified. A user can end up owning more
    than one household — signing up creates an empty one, and claiming an
    existing household adds a second — and with no ordering the database was
    free to return either first. Whichever came back became "their" household,
    so the same account could land on 2000 readings one day and a blank console
    the next.

    So the one that actually holds data wins, and ties break on age. Ordering
    alone would not have been enough: the empty household is created first, so
    "oldest" would reliably pick the wrong one.
    """
    hit = _household_cache.get(user_id)
    now = time.monotonic()
    if hit and now < hit[0]:
        return hit[1]
    try:
        db = get_db()
        rows = (
            db.table("households").select("household_id,created_at")
            .eq("owner_id", user_id).order("created_at").execute().data
        ) or []
        ids = [r["household_id"] for r in rows]

        if len(ids) > 1:
            def reading_count(hid: str) -> int:
                try:
                    r = (db.table("energy_readings").select("id", count="exact")
                         .eq("household_id", hid).limit(1).execute())
                    return r.count or 0
                except Exception:
                    return 0
            # Stable: sorted() keeps created_at order among equal counts.
            ids = sorted(ids, key=lambda h: -reading_count(h))
    except Exception as e:
        # Almost always means migration 008 has not been applied. Say so,
        # because the symptom otherwise is "sign-in silently fails" with
        # nothing pointing at the cause.
        logger.warning(
            "household lookup failed (%s) - has migrations/008_accounts.sql been run?",
            type(e).__name__,
        )
        return []
    _household_cache[user_id] = (now + TOKEN_CACHE_SECONDS, ids)
    return ids


def ensure_household(user_id: str, email: str | None = None) -> str:
    """The user's household, created on first sign-in if they have none.

    New accounts start genuinely empty — no devices, no readings, and nothing
    copied from anyone else. An empty console is the honest state for someone
    who has not paired hardware yet; seeding it with representative numbers
    would put figures on screen that are not theirs.
    """
    existing = households_for(user_id)
    if existing:
        return existing[0]

    # Household id is the user id: one owner, one household, no extra mapping
    # to drift out of sync. Existing households keep their original ids, which
    # is why ownership lives in its own table rather than being derived.
    household_id = user_id
    try:
        get_db().table("households").upsert({
            "household_id": household_id,
            "owner_id": user_id,
            "display_name": (email or "").split("@")[0] or "Home",
        }, on_conflict="household_id").execute()
    except Exception as e:
        logger.error(
            "could not create household for %s (%s) - if this is a fresh deploy, "
            "run migrations/008_accounts.sql to create the households table",
            user_id, type(e).__name__,
        )
        raise AuthError("Could not set up your account. Try again shortly.") from e

    _household_cache.pop(user_id, None)
    return household_id


async def require_household(authorization: str | None, claimed: str | None) -> str:
    """Authenticate, then confirm the caller owns the household they named.

    Returns the household id to use. Raises AuthError otherwise.

    `claimed` comes from the request body and is untrusted — checking it
    against ownership is the whole point. A caller who omits it gets their own
    household, which is what the app does on a fresh sign-in.
    """
    token = bearer_token(authorization)
    if not token:
        raise AuthError("Sign in to use Kroven.")

    user = await verify_token(token)
    user_id = user["id"]
    owned = households_for(user_id) or [ensure_household(user_id, user.get("email"))]

    if not claimed:
        return owned[0]
    if claimed in owned:
        return claimed

    logger.warning("user %s requested household %s which they do not own", user_id, claimed)
    raise AuthError("That household isn't yours.")


def auth_required() -> bool:
    """Whether endpoints should demand a session.

    Off by default so that deploying this code does not instantly lock out the
    running console before accounts exist. Turn on with KROVEN_REQUIRE_AUTH=1
    once you have signed up and claimed the existing household.
    """
    return os.environ.get("KROVEN_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes")
