# Kroven — full project (frontend + backend)

## What's here
- `frontend/` — your current v14 Netlify site (React, bundled, plus the 2 thin
  Netlify functions). Keep deploying this to Netlify as-is for now.
- `backend/` — the new FastAPI service. See `backend/README.md` for the full
  setup walkthrough (Supabase + Railway/Render deploy steps).

## The 4 gaps this closes
1. Backend/API — `backend/app/main.py`
2. Database — `backend/schema.sql` (run in Supabase SQL editor)
3. Model serving — `backend/app/model.py` (wraps your LSTM; falls back to a
   naive average until you drop in your trained `.keras` file)
4. Rate-aware recommendations — `backend/app/routers/rates.py` — this is the
   "charge now, rates spike in 1hr, saves $X" logic. Replace `RATE_SCHEDULE`
   with your real utility's peak/off-peak windows.

## Fastest path from here (in Cursor)
1. Unzip this into your Kroven project folder, or clone your GitHub repo and
   drop `backend/` in alongside your existing frontend code.
2. `cd backend && pip install -r requirements.txt`
3. Create a Supabase project → SQL editor → run `schema.sql`
4. Copy `.env.example` to `.env`, fill in real Supabase + Anthropic keys
5. `uvicorn app.main:app --reload --port 8000` to test locally
6. Push to GitHub, deploy `backend/` on Railway or Render
7. Update `frontend/index.html`'s API calls to point at your new backend URL
8. Push — Netlify auto-redeploys the frontend

## Still needs you
- Your actual trained LSTM weights (currently using a naive fallback)
- Your real utility's TOU rate schedule (currently using an example PG&E-style one)
- Supabase + Railway account creation and API keys (can't be automated for you)
