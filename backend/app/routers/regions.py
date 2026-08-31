"""
GET /api/regions

Forecast for every county in the beta service area — one representative city
each, so the console can show conditions across the whole coverage footprint
rather than only where the user happens to be standing.

All of it is National Weather Service data for real coordinates. A county whose
lookup fails comes back with status "unavailable" and the reason; it is never
back-filled from a neighbour, because a forecast for the wrong county is worse
than an empty cell.

Requests are cached (grid points effectively never move, forecasts for 30
minutes) and run concurrently, so a full refresh is a handful of calls rather
than eighteen every time someone opens the tab.
"""

import asyncio
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter

from app.signals import USER_AGENT, _cached

router = APIRouter()

NWS = "https://api.weather.gov"

# One representative city per county in the nine-county beta area.
REGIONS = [
    {"county": "San Francisco", "city": "San Francisco", "lat": 37.7749, "lon": -122.4194},
    {"county": "Alameda",       "city": "Oakland",       "lat": 37.8044, "lon": -122.2712},
    {"county": "Santa Clara",   "city": "San Jose",      "lat": 37.3382, "lon": -121.8863},
    {"county": "San Mateo",     "city": "San Mateo",     "lat": 37.5630, "lon": -122.3255},
    {"county": "Contra Costa",  "city": "Concord",       "lat": 37.9780, "lon": -122.0311},
    {"county": "Marin",         "city": "San Rafael",    "lat": 37.9735, "lon": -122.5311},
    {"county": "Sonoma",        "city": "Santa Rosa",    "lat": 38.4404, "lon": -122.7141},
    {"county": "Napa",          "city": "Napa",          "lat": 38.2975, "lon": -122.2869},
    {"county": "Solano",        "city": "Fairfield",     "lat": 38.2494, "lon": -122.0400},
]

POINT_TTL = 60 * 60 * 24 * 7
FORECAST_TTL = 60 * 30


async def _one(client: httpx.AsyncClient, region: dict) -> dict:
    base = {"county": region["county"], "city": region["city"],
            "lat": region["lat"], "lon": region["lon"]}
    try:
        async def points():
            r = await client.get(f"{NWS}/points/{region['lat']:.4f},{region['lon']:.4f}")
            r.raise_for_status()
            return r.json()["properties"]

        props = await _cached(f"rp:{region['city']}", POINT_TTL, points)

        async def forecast():
            r = await client.get(props["forecast"])
            r.raise_for_status()
            return r.json()["properties"]["periods"]

        periods = await _cached(f"rf:{region['city']}", FORECAST_TTL, forecast)
    except Exception as e:
        base.update(status="unavailable",
                    detail=f"NWS lookup failed ({type(e).__name__})")
        return base

    if not periods:
        base.update(status="unavailable", detail="NWS returned no forecast periods")
        return base

    now = periods[0]
    upcoming = []
    for p in periods[:4]:
        upcoming.append({
            "name": p.get("name"),
            "temp": p.get("temperature"),
            "unit": p.get("temperatureUnit"),
            "is_daytime": p.get("isDaytime"),
            "short": p.get("shortForecast"),
            "wind": p.get("windSpeed"),
            "precip": (p.get("probabilityOfPrecipitation") or {}).get("value"),
        })

    # today's high/low from the first day/night pair
    highs = [p.get("temperature") for p in periods[:2] if p.get("isDaytime")]
    lows = [p.get("temperature") for p in periods[:2] if p.get("isDaytime") is False]

    base.update(
        status="ok",
        temp=now.get("temperature"),
        unit=now.get("temperatureUnit"),
        short=now.get("shortForecast"),
        wind=now.get("windSpeed"),
        precip=(now.get("probabilityOfPrecipitation") or {}).get("value"),
        high=highs[0] if highs else None,
        low=lows[0] if lows else None,
        periods=upcoming,
    )
    return base


@router.get("")
async def regions():
    async with httpx.AsyncClient(
        timeout=25,
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
    ) as client:
        results = await asyncio.gather(
            *[_one(client, r) for r in REGIONS], return_exceptions=True
        )

    out = []
    for region, res in zip(REGIONS, results):
        if isinstance(res, Exception):
            out.append({**region, "status": "unavailable",
                        "detail": f"{type(res).__name__}"})
        else:
            out.append(res)

    live = [r for r in out if r.get("status") == "ok"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "National Weather Service",
        "regions": out,
        "live_count": len(live),
        "total": len(out),
    }
