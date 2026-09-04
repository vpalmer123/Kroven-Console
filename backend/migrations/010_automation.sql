-- Automated load shedding: consent, and a record of every automated action.
--
-- This is the first thing in Kroven that switches real hardware with nobody
-- watching. Reading data wrongly produces a bad answer; cutting power wrongly
-- interrupts whatever was running. So two things exist that the manual path
-- never needed:
--
--   consent   recorded per device, with the exact wording the user agreed to
--             and when. Not a global setting — agreeing that a lamp may be
--             cut is not agreeing that a console may be.
--   audit     one row per automated actuation, including the power reading
--             that justified it. If something is interrupted, the question is
--             immediately "what did it see, and when", and that has to be
--             answerable afterwards rather than reconstructed from logs that
--             may have rotated.
--
-- Manual actions are logged too, so the timeline is complete: "did Kroven do
-- this or did I" is otherwise unanswerable.

create table if not exists public.automation_events (
    id              uuid primary key default gen_random_uuid(),
    household_id    text not null,
    device_id       text not null,
    device_name     text,

    -- 'shed' | 'restore' | 'skipped' | 'failed'
    action          text not null,
    -- true when Kroven decided; false when a person pressed something.
    automated       boolean not null default true,

    -- The evidence at the moment of the decision. Nullable because a failure
    -- can occur before a reading is obtained.
    power_w         double precision,
    idle_seconds    integer,

    -- Why it acted, or why it declined to.
    reason          text,

    created_at      timestamptz not null default now()
);

create index if not exists automation_events_household_idx
    on public.automation_events (household_id, created_at desc);
create index if not exists automation_events_device_idx
    on public.automation_events (device_id, created_at desc);

alter table public.automation_events enable row level security;

drop policy if exists automation_events_own on public.automation_events;
create policy automation_events_own on public.automation_events
    for all
    using (
        household_id::text in (
            select household_id::text from public.households
            where owner_id = auth.uid()
        )
    )
    with check (
        household_id::text in (
            select household_id::text from public.households
            where owner_id = auth.uid()
        )
    );

-- Consent and thresholds live on the device row itself, in meta:
--
--   auto_shed: {
--     enabled:        boolean,
--     consented_at:   timestamptz,
--     consent_version: text,      -- which wording was agreed to
--     idle_watts:     number,     -- treat below this as idle
--     idle_minutes:   number      -- ...sustained for this long
--   }
--
-- Kept there rather than in a column so the consent travels with the device
-- and disappears with it. A device that is removed and re-paired must be
-- consented to again, which is the correct default for something that cuts
-- power.

select
    c.relname        as table_name,
    c.relrowsecurity as rls_enabled,
    (select count(*) from pg_policies p
      where p.schemaname = 'public' and p.tablename = c.relname) as policy_count
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
order by c.relname;
