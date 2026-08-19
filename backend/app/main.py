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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import forecast, energy_data, chat, rates

app = FastAPI(title="Kroven API", version="0.1.0")

# Lock this down to your real frontend domain before going to production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: replace with ["https://krovens.netlify.app"]
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(energy_data.router, prefix="/api/energy", tags=["energy"])
app.include_router(forecast.router, prefix="/api/forecast", tags=["forecast"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(rates.router, prefix="/api/rates", tags=["rates"])


@app.get("/health")
def health():
    return {"status": "ok"}
