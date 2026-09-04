"""
Kroven backend — FastAPI app.

This is the missing middle layer between the Netlify frontend and
your data/model. It replaces the Netlify function pattern (thin proxy,
no memory) with a real service that:
  - persists energy data to a database (Supabase/Postgres)
  - serves LSTM forecasts from a model that's actually loaded once and reused
  - runs the agent routing logic server-side instead of client -> Claude directly

Run locally:
    uvicorn app.main:app --reload --port 8000

Deploy: any host that runs a Python ASGI app (Render, Railway, Fly.io,
or a Netlify background function pointed at this via a separate service).
Netlify's own functions are NOT a good fit for this — they're short-lived
per-request lambdas, which is exactly why your model was never "served."
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import forecast, energy_data, chat, rates, devices, signals, activity, dashboard, mapconfig, regions, access
from app.security import allowed_origins, docs_enabled, rate_limit_middleware

# /docs and /openapi.json are off unless KROVEN_ENABLE_DOCS is set. FastAPI
# publishes both by default, which hands any visitor a complete map of the API,
# including the endpoints that switch physical hardware.
app = FastAPI(
    title="Kroven API",
    version="0.1.0",
    docs_url="/docs" if docs_enabled() else None,
    redoc_url="/redoc" if docs_enabled() else None,
    openapi_url="/openapi.json" if docs_enabled() else None,
)

# Bounds what any one caller, and everyone together, can spend. /api/chat calls
# Anthropic on our key, so an unlimited public endpoint is an unlimited bill.
# Added before CORS so that CORS headers still land on a 429.
app.middleware("http")(rate_limit_middleware)

# Set KROVEN_ALLOWED_ORIGINS in Railway to the console's origin. Unset means
# open, which is logged loudly at startup rather than failing closed and taking
# the live demo down for everyone it was shared with.
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(energy_data.router, prefix="/api/energy", tags=["energy"])
app.include_router(forecast.router, prefix="/api/forecast", tags=["forecast"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(rates.router, prefix="/api/rates", tags=["rates"])
app.include_router(devices.router, prefix="/api/devices", tags=["devices"])
app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
app.include_router(activity.router, prefix="/api/activity", tags=["activity"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(mapconfig.router, prefix="/api/mapconfig", tags=["map"])
app.include_router(regions.router, prefix="/api/regions", tags=["regions"])
app.include_router(access.router, prefix="/api/access", tags=["access"])


@app.on_event("startup")
async def _start_device_polling():
    """Device pollers live here so they share the app's event loop.

    Shelly is on a private LAN address, so this only does anything on a host
    with a route to it. On Railway it logs why it is skipping and returns.
    """
    try:
        from shelly_poller import start_shelly_polling
        from app.device_writes import insert_device_reading
        start_shelly_polling(insert_device_reading)
    except Exception as e:  # never let a poller stop the API from booting
        logging.getLogger("kroven").warning(f"Shelly polling not started: {e}")


@app.get("/health")
def health():
    return {"status": "ok"}
