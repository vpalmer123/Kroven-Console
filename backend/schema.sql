-- Run this in the Supabase SQL editor to create the tables Kroven needs.
-- This is the "database" layer that's currently missing.

create table if not exists energy_readings (
  id bigint generated always as identity primary key,
  household_id text not null,
  recorded_at timestamptz not null,
  kwh_consumed numeric,
  kwh_solar_generated numeric,
  battery_soc_pct numeric,          -- battery state of charge, 0-100
  grid_import_kwh numeric,
  source text default 'manual',     -- 'manual' | 'utility_api' | 'device'
  created_at timestamptz default now()
);

create index if not exists idx_energy_readings_household_time
  on energy_readings (household_id, recorded_at desc);

create table if not exists forecasts (
  id bigint generated always as identity primary key,
  household_id text not null,
  forecast_for timestamptz not null,   -- the hour/day being predicted
  predicted_kwh numeric not null,
  lower_bound numeric,                 -- for showing a confidence band, not a fake-precise number
  upper_bound numeric,
  model_version text default 'lstm-v1',
  generated_at timestamptz default now()
);

create index if not exists idx_forecasts_household_time
  on forecasts (household_id, forecast_for desc);

-- Cache agent/chat responses so repeat questions don't hit the LLM API every time
create table if not exists agent_cache (
  id bigint generated always as identity primary key,
  cache_key text unique not null,     -- hash of the question + relevant data window
  response jsonb not null,
  created_at timestamptz default now(),
  expires_at timestamptz
);
