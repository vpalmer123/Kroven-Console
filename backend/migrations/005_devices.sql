-- Device registry.
--
-- Until now "the device" was whatever SHELLY_* env vars happened to be set, so
-- there was exactly one, it could not be named, and nothing could be added
-- without a redeploy. Control needs a real registry: a stable id per device,
-- the name the person actually calls it, and how to reach it.
--
-- signal_type is the important column and is not about transport:
--
--   dedicated  one load on the plug, so its power trace IS that appliance.
--              Safe to feed a per-device model.
--   aggregate  several loads behind one meter (a shared extension cord), so
--              the trace is a sum. A per-device model trained on it would be
--              learning a fiction. Routed to occupancy/correlation/HAR work
--              instead, where a mixed signal is the point rather than a flaw.
--
-- Consumers must filter on it rather than assuming every row is comparable.

create table if not exists devices (
    id                uuid primary key default gen_random_uuid(),
    household_id      uuid not null,

    -- What the person calls it out loud. Fuzzy-matched against speech, so it
    -- should be their word ("PS5"), not a model number.
    name              text not null,
    kind              text not null,                    -- 'kasa' | 'shelly'
    host              text,                             -- LAN address or base URL
    channel           integer not null default 0,

    signal_type       text not null default 'dedicated'
                      check (signal_type in ('dedicated', 'aggregate')),

    -- False when the hardware cannot actually be switched, so the agent can
    -- say so instead of trying and failing in front of the user.
    controllable      boolean not null default true,

    -- Last known switch state. Written after every actuation and every poll.
    state             text check (state in ('on', 'off', 'unknown')),
    state_source      text,                             -- 'actuation' | 'poll'
    power_w           double precision,
    state_updated_at  timestamptz,
    last_seen_at      timestamptz,

    -- Free-form: model, firmware, what shares the circuit, why it is aggregate.
    meta              jsonb not null default '{}'::jsonb,
    created_at        timestamptz not null default now(),

    unique (household_id, name)
);

create index if not exists devices_household_idx on devices (household_id);
create index if not exists devices_signal_type_idx on devices (household_id, signal_type);

-- Supabase exposes every public table through PostgREST, reachable with the
-- anon key, which ships in browsers and is not a secret. RLS is the only thing
-- guarding it. This table holds household ids, device names and the plugs' LAN
-- addresses; omitting this line left all of it anonymously readable.
--
-- No permissive policy is created on purpose: the backend uses the service role
-- key, which bypasses RLS, so service_role keeps full access and anon gets
-- nothing. See 006_rls_lockdown.sql, which applies this across the schema.
alter table public.devices enable row level security;
