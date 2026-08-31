-- Per-device columns for the energy logger.
--
-- energy_readings already existed with: id, household_id, recorded_at,
-- kwh_consumed, kwh_solar_generated, battery_soc_pct, grid_import_kwh,
-- source, created_at. That is enough to log against, but without these three
-- the device label has to be squeezed into `source` and instantaneous power
-- is lost entirely.
--
-- The logger runs correctly whether or not this has been applied — it detects
-- which columns exist and only sends those. Applying it just means richer data.
--
-- Safe to re-run.

alter table public.energy_readings
    add column if not exists device_name text,
    add column if not exists watts       double precision,
    add column if not exists kwh_today   double precision;

comment on column public.energy_readings.device_name is
    'Human label for the source device, e.g. "PS5". Distinguishes plugs once more are added.';
comment on column public.energy_readings.watts is
    'Instantaneous power draw at the moment of the reading.';
comment on column public.energy_readings.kwh_today is
    'The device''s own cumulative counter as reported. kwh_consumed holds the per-interval delta derived from it.';

-- The forecaster reads a household's recent readings in time order; this is
-- the index that query wants once the table has hours of data in it.
create index if not exists energy_readings_household_time_idx
    on public.energy_readings (household_id, recorded_at desc);

-- Same reasoning as the other tables: the backend uses the service key and
-- bypasses RLS, and no permissive policy exists so anon keys cannot read
-- another household's readings.
alter table public.energy_readings enable row level security;
