-- Pending device actions: the authorization record that must exist before any
-- provider command is dispatched.
--
-- WHY THIS TABLE EXISTS
-- Confirmation used to live in the conversation: the assistant asked, the user
-- said yes, and a classifier decided that "yes" meant agreement. That is not an
-- authorization boundary. An LLM deciding whether consent was given is an LLM
-- deciding whether to cut power to someone's home, and prompt wording is not a
-- control. It also means a replayed, mistimed, or misattributed "yes" can
-- actuate hardware.
--
-- So the decision moves to the server. A control request writes a row here and
-- nothing else happens. Dispatch requires a confirmation that resolves to this
-- exact row, for this exact user, before it has expired, and while the device
-- is still in the state the proposal was made against. The assistant can
-- describe the row. It cannot dispatch it.
--
-- Every transition is recorded in automation_events, so the full life of an
-- action — proposed, confirmed, dispatched, acknowledged, verified, failed,
-- expired, cancelled — is reconstructable afterwards.

create table if not exists public.pending_actions (
    id              uuid primary key default gen_random_uuid(),

    household_id    text not null,
    -- The authenticated identity that proposed it. A confirmation from any
    -- other user is refused even if it names the right action id.
    user_id         uuid,

    device_id       text not null,
    device_name     text,

    -- 'on' | 'off' | 'toggle'
    command         text not null,
    capability      text,

    -- What the device looked like when the proposal was made. If the device has
    -- moved since — someone hit the physical button, another session acted, an
    -- automation fired — the proposal no longer describes reality and must not
    -- be executable.
    expected_state  text,
    expected_power_w double precision,

    -- The sentence shown to the user. Stored so the audit record contains what
    -- they were actually told, not a later reconstruction of it.
    consequence     text not null,

    -- proposed | confirmed | dispatched | verified | failed | expired | cancelled
    status          text not null default 'proposed',

    created_at      timestamptz not null default now(),
    expires_at      timestamptz not null,
    resolved_at     timestamptz,

    -- Set the moment it is consumed. A confirmed action can never be replayed.
    consumed_at     timestamptz,

    provider_result text,
    verified_state  text,
    verified_at     timestamptz
);

create index if not exists pending_actions_open_idx
    on public.pending_actions (household_id, status, expires_at desc);
create index if not exists pending_actions_device_idx
    on public.pending_actions (device_id, created_at desc);

alter table public.pending_actions enable row level security;

drop policy if exists pending_actions_own on public.pending_actions;
create policy pending_actions_own on public.pending_actions
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

select
    c.relname        as table_name,
    c.relrowsecurity as rls_enabled,
    (select count(*) from pg_policies p
      where p.schemaname = 'public' and p.tablename = c.relname) as policy_count
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
order by c.relname;
