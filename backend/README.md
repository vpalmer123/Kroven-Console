# Kroven Backend — what this is

This fills the 4 gaps in v14:

1. **Backend/API** — `app/main.py` + routers. Real FastAPI service instead of two
   stateless Netlify functions.
2. **Database** — `schema.sql` (run it in Supabase's SQL editor). Stores
   readings and forecasts instead of losing everything on refresh.
3. **Model serving** — `app/model.py` loads your LSTM once and reuses it.
   Right now it falls back to a naive average until you export your
   trained model into `app/models/lstm_model.keras`.
4. **Frontend → backend** — your React app needs to call these endpoints
   instead of talking to `/api/chat` and `/api/polymarket` directly.

## Setup

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
uvicorn app.main:app --reload --port 8000
```

Then in Supabase: SQL editor → paste `schema.sql` → run.

## Endpoints

- `POST /api/energy/readings` — add one reading
- `POST /api/energy/readings/bulk` — add many at once (for your paste/upload box)
- `GET /api/energy/readings/{household_id}` — recent readings
- `GET /api/forecast/{household_id}` — cached-or-fresh forecast with a confidence band
- `POST /api/chat` — agent chat, now grounded in real forecast data + rate recommendation
- `POST /api/rates/recommend` — the "charge now, rates spike in 1hr, saves $X" logic.
  Uses a static TOU schedule in `app/routers/rates.py` — replace `RATE_SCHEDULE`
  with your actual utility's real peak/off-peak windows and rates.

## Deploying

Netlify functions can't run this (they're stateless per-request lambdas —
that's part of why nothing persisted before). Deploy this FastAPI app
separately on Render, Railway, or Fly.io (all have free tiers), then
point your Netlify frontend's fetch calls at that URL instead of
`/api/chat`.

## Next real step

Export your trained LSTM weights from wherever the notebook lives into
`app/models/lstm_model.keras`, and adjust `FEATURE_COLUMNS` /
`SEQUENCE_LENGTH` in `app/model.py` to match what you actually trained on.
Until then every forecast uses the naive-average fallback — which is
honest and won't break, but isn't your real model yet.
