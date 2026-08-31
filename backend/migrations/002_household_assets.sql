-- Adds the energy-asset inventory to household profiles.
--
-- Safe to run whether or not 001 has been applied — it creates the table if it
-- is missing and only adds columns that aren't already there. Run this one file
-- in the Supabase SQL editor and you're current.
--
-- `assets` is jsonb rather than a column per asset because households differ a
-- lot: solar + battery, EV only, generator in a rural area, nothing but grid.
-- Storing the shape loosely means adding a new asset type later needs no
-- migration at all.
--
-- Expected shape (every key optional — absent means "unknown", not "no"):
--   {
--     "solar":     {"present": true, "size_kw": 6.4},
--     "battery":   {"present": true, "capacity_kwh": 13.5},
--     "ev":        {"present": true, "charges_per_week": 4},
--     "generator": {"present": false},
--     "heating":   {"type": "heat pump"},
--     "other":     "pool pump runs 6h/day"
--   }

create table if not exists public.household_profiles (
    household_id      text primary key,
    monthly_kwh       double precision,
    monthly_bill_usd  double precision,
    home_notes        text,
    updated_at        timestamptz not null default now()
);

alter table public.household_profiles
    add column if not exists assets jsonb not null default '{}'::jsonb;

create or replace function public.touch_household_profiles()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists household_profiles_touch on public.household_profiles;
create trigger household_profiles_touch
    before update on public.household_profiles
    for each row execute function public.touch_household_profiles();

alter table public.household_profiles enable row level security;
