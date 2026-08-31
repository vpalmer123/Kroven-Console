-- The general ingestion table. Run this in the Supabase SQL editor.
--
-- energy_readings only models energy: household, time, kWh. That is the wrong
-- shape for what is coming. A Shelly reports watts and on/off state, an ESP32
-- reports CSI amplitude variance and presence, and later domains will report
-- things nobody has named yet. Forcing all of that into a kWh column would mean
-- either losing it or lying about what it is.
--
-- So: one row per observation, tagged with where it came from and what kind of
-- signal it is. New device types and new domains are new `source` and
-- `signal_type` values, never a schema change.
--
--   source        "kasa:PS5", "shelly:heater", "esp32:livingroom"
--   signal_type   "power_w", "energy_kwh", "switch_state",
--                 "csi_variance", "presence", "correlation"
--
-- energy_readings stays as it is: it is what the forecaster already reads, and
-- the logger keeps writing kWh there. Observations is the wider pool that grows
-- as devices come online.

create table if not exists public.observations (
    id           bigserial primary key,
    household_id text        not null,
    observed_at  timestamptz not null,
    source       text        not null,
    signal_type  text        not null,
    value        double precision,
    meta         jsonb       not null default '{}'::jsonb,
    created_at   timestamptz not null default now()
);

-- The two queries this table exists to serve: "recent signals for this
-- household" and "recent readings from one device".
create index if not exists observations_household_time_idx
    on public.observations (household_id, observed_at desc);
create index if not exists observations_source_time_idx
    on public.observations (household_id, source, observed_at desc);
create index if not exists observations_type_time_idx
    on public.observations (household_id, signal_type, observed_at desc);

-- Same duplicate protection the importer needs: re-running an import must not
-- double-count a reading that is already stored.
create unique index if not exists observations_unique_point
    on public.observations (household_id, source, signal_type, observed_at);

-- Rolling baselines, one row per device per signal, updated in place as data
-- lands. Kept separate from the raw pool so reading the current threshold is a
-- single-row lookup rather than a scan over everything ever recorded.
create table if not exists public.device_baselines (
    household_id   text not null,
    source         text not null,
    signal_type    text not null,
    idle_value     double precision,
    active_value   double precision,
    threshold      double precision,
    sample_count   integer not null default 0,
    idle_count     integer not null default 0,
    active_count   integer not null default 0,
    confidence     double precision not null default 0,
    updated_at     timestamptz not null default now(),
    primary key (household_id, source, signal_type)
);

alter table public.observations enable row level security;
alter table public.device_baselines enable row level security;
