"""
Live, location-specific energy signals.

Every value returned here is either read directly from a named public API or
computed from one by a formula stated in the payload. There is no fallback,
no default, and no estimate: when a source is unreachable the signal comes back
with status "unavailable" and the reason, and the UI shows that instead of a
number.

Sources
  - National Weather Service (api.weather.gov) — point forecast and active
    alerts for the caller's exact coordinates. Public, no key, requires a
    User-Agent identifying the app.
  - California ISO (caiso.com) — live system demand vs day-ahead forecast for
    the grid serving the whole PG&E footprint. Public CSV, no key.

Deliberately NOT here: an outage probability. No public source publishes a
calibrated outage likelihood for a household, so Kroven reports the real
precursors (active alerts, wind, grid load) and leaves the inference to the
reader rather than inventing a percentage.
"""

import csv
import io
import time
from datetime import datetime
from typing import Any

import httpx

USER_AGENT = "Kroven Energy Console (https://krovens.netlify.app)"
NWS_BASE = "https://api.weather.gov"
CAISO_DEMAND_URL = "https://www.caiso.com/outlook/current/demand.csv"

_cache: dict[str, tuple[float, Any]] = {}


async def _cached(key: str, ttl: float, factory):
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = await factory()
    _cache[key] = (now, value)
    return value


def _signal(
    key: str,
    label: str,
    kind: str,
    source: str,
    source_url: str,
    value: float | None = None,
    display: str = "",
    basis: str = "",
    status: str = "ok",
    detail: str = "",
) -> dict:
    """kind: probability | certainty | measure | state.

    Only 'probability' means a real published likelihood. 'measure' is an
    observed quantity, 'certainty' a scheduled fact, 'state' a yes/no condition.
    The UI must not render any of them as if they were forecasts of risk.
    """
    return {
        "key": key,
        "label": label,
        "kind": kind,
        "value": value,
        "display": display,
        "basis": basis,
        "source": source,
        "source_url": source_url,
        "status": status,
        "detail": detail,
    }


async def _nws_grid(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    r = await client.get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
    r.raise_for_status()
    return r.json()["properties"]


async def _weather_signals(lat: float, lon: float) -> list[dict]:
    src = "National Weather Service"
    url = "https://api.weather.gov"
    try:
        async with httpx.AsyncClient(
            timeout=20, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
        ) as client:
            props = await _cached(f"pt:{lat:.3f},{lon:.3f}", 86400, lambda: _nws_grid(client, lat, lon))
            hourly_url = props.get("forecastHourly")
            city = ((props.get("relativeLocation") or {}).get("properties") or {}).get("city", "")

            fr = await client.get(hourly_url)
            fr.raise_for_status()
            periods = fr.json()["properties"]["periods"][:12]

            ar = await client.get(f"{NWS_BASE}/alerts/active", params={"point": f"{lat:.4f},{lon:.4f}"})
            ar.raise_for_status()
            alerts = ar.json().get("features", [])
    except Exception as e:
        reason = f"{type(e).__name__}"
        return [
            _signal("precip", "Rain in the next 12h", "probability", src, url,
                    status="unavailable", detail=f"NWS unreachable ({reason})"),
            _signal("wind", "Peak wind next 12h", "measure", src, url,
                    status="unavailable", detail=f"NWS unreachable ({reason})"),
            _signal("alerts", "Weather alerts for your area", "state", src, url,
                    status="unavailable", detail=f"NWS unreachable ({reason})"),
        ]

    pops = [
        (p.get("probabilityOfPrecipitation") or {}).get("value")
        for p in periods
        if (p.get("probabilityOfPrecipitation") or {}).get("value") is not None
    ]
    max_pop = max(pops) if pops else None

    winds = []
    for p in periods:
        raw = (p.get("windSpeed") or "").split()
        for token in raw:
            if token.isdigit():
                winds.append(int(token))
    max_wind = max(winds) if winds else None

    where = f" near {city}" if city else ""
    out = [
        _signal(
            "precip", f"Rain in the next 12h{where}", "probability", src,
            "https://api.weather.gov",
            value=float(max_pop) if max_pop is not None else None,
            display=f"{max_pop}%" if max_pop is not None else "",
            basis="Highest hourly probability of precipitation published by NWS for your forecast grid point, next 12 hours.",
            status="ok" if max_pop is not None else "unavailable",
            detail="" if max_pop is not None else "NWS returned no probability for this grid point",
        ),
        _signal(
            "wind", "Strongest wind forecast, next 12h", "measure", src,
            "https://api.weather.gov",
            value=min(float(max_wind) / 50 * 100, 100) if max_wind is not None else None,
            display=f"{max_wind} mph" if max_wind is not None else "",
            basis="Highest forecast wind speed in the next 12 hourly NWS periods. Bar is scaled against 50 mph, not a probability. High wind is a documented trigger for precautionary shutoffs.",
            status="ok" if max_wind is not None else "unavailable",
        ),
    ]

    if alerts:
        names = ", ".join(sorted({a["properties"].get("event", "alert") for a in alerts}))
        out.append(_signal(
            "alerts", "Active weather alerts for your location", "state", src,
            "https://api.weather.gov/alerts/active",
            value=100.0, display=names,
            basis="NWS alerts currently in force for your exact coordinates.",
        ))
    else:
        out.append(_signal(
            "alerts", "Active weather alerts for your location", "state", src,
            "https://api.weather.gov/alerts/active",
            value=0.0, display="None in force",
            basis="NWS reports no active alerts for your exact coordinates right now.",
        ))
    return out


async def _caiso_signal() -> dict:
    src = "California ISO"
    url = "https://www.caiso.com/todays-outlook"

    async def fetch():
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(CAISO_DEMAND_URL)
            r.raise_for_status()
            return r.text

    try:
        text = await _cached("caiso", 300, fetch)
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception as e:
        return _signal("grid_load", "Grid demand vs today's forecast peak", "measure", src, url,
                       status="unavailable", detail=f"CAISO unreachable ({type(e).__name__})")

    def num(row, col):
        raw = (row.get(col) or "").strip()
        try:
            return float(raw)
        except ValueError:
            return None

    current = None
    current_time = ""
    for row in rows:
        v = num(row, "Current demand")
        if v is not None:
            current, current_time = v, (row.get("Time") or "").strip()

    forecasts = [num(r, "Day ahead forecast") for r in rows]
    peak = max([f for f in forecasts if f is not None], default=None)

    if current is None or not peak:
        return _signal("grid_load", "Grid demand vs today's forecast peak", "measure", src, url,
                       status="unavailable", detail="CAISO feed had no current demand value yet")

    pct = current / peak * 100
    return _signal(
        "grid_load", "Grid demand vs today's forecast peak", "measure", src, url,
        value=min(pct, 100.0),
        display=f"{pct:.0f}% of peak",
        basis=(
            f"CAISO live system demand {current:,.0f} MW at {current_time} divided by today's "
            f"highest day-ahead forecast {peak:,.0f} MW. Covers the whole California grid, not "
            f"one neighbourhood, and is a load level — not a probability of anything."
        ),
    )


async def location_signals(lat: float, lon: float) -> dict:
    weather = await _weather_signals(lat, lon)
    grid = await _caiso_signal()
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "signals": [*weather, grid],
        "omitted": [
            {
                "label": "Outage probability",
                "why": (
                    "No public source publishes a calibrated outage likelihood for a "
                    "household, so Kroven does not show one. The real precursors are "
                    "above: active alerts, forecast wind, and grid load."
                ),
            }
        ],
    }
