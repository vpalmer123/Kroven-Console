"""
Replaces netlify/functions/chat.js. The difference: this version pulls
real household data and the latest forecast from the DB and injects it
into the prompt, so agents answer from actual numbers instead of just
whatever the user typed in that turn.
"""

import os
import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from app.db import get_db
from app.routers.rates import recommend, RecommendationRequest

router = APIRouter()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


class ChatRequest(BaseModel):
    household_id: str
    message: str
    device_battery_pct: float | None = None


@router.post("")
async def chat(req: ChatRequest):
    db = get_db()

    forecast_row = (
        db.table("forecasts")
        .select("*")
        .eq("household_id", req.household_id)
        .order("generated_at", desc=True)
        .limit(1)
        .execute()
    )
    forecast_context = forecast_row.data[0] if forecast_row.data else None

    rec = recommend(
        RecommendationRequest(
            household_id=req.household_id,
            device_battery_pct=req.device_battery_pct,
        )
    )

    system_prompt = (
        "You are Kroven's Energy Lead agent. Answer using the real data below — "
        "give a plain-English recommendation with the actual dollar figure, the way "
        "a person would explain it to a friend. Be concise, no hedging filler.\n\n"
        f"Latest forecast: {forecast_context}\n"
        f"Current rate recommendation: {rec.model_dump()}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": req.message}],
            },
        )
    return r.json()
