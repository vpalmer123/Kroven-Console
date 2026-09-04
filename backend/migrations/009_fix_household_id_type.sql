-- Repair 008, which failed partway through.
--
--   ERROR: 42883: operator does not exist: uuid = text
--
-- 005_devices.sql declared devices.household_id as uuid. Every other table
-- stores the same value as text, and so does households.household_id, so the
-- ownership subquery compared a uuid against text and Postgres refused.
--
-- 008 is not transactional across statements the way it was written: the
-- households table and the policies for energy_readings, observations and
-- household_profiles were created before the error, so this has to be safe to
-- run on a half-applied schema. Everything below is idempotent.
--
-- Two things are fixed, not one:
--
--   1. devices.household_id becomes text, so one concept has one type. A cast
--      in the policy alone would have worked and left the mismatch in place to
--      break the next join someone writes.
--   2. The policies cast explicitly anyway. Belt and braces: if another table
--      is ever added with uuid, the policy still applies rather than failing
--      the whole migration halfway, which is what happened here.

-- ---------------------------------------------------------------------------
-- 0. Make sure the ownership table exists.
--
-- Worth knowing what actually survived 008: its policies were all inside a
-- single do $$ ... $$ block, and a block is one statement, so the error on
-- `devices` rolled back the WHOLE block. The "ownership policy applied to
-- energy_readings" notices printed before the rollback and did not survive it,
-- so despite what the output suggested, no data-table policy was created. The
-- statements before the block (the table, its RLS, its own policy) are
-- separate statements and did commit.
--
-- Repeated here anyway so this file stands alone and can be run first.
-- ---------------------------------------------------------------------------
create table if not exists public.households (
    household_id  text primary key,
    owner_id      uuid not null references auth.users(id) on delete cascade,
    display_name  text,
    created_at    timestamptz not null default now()
);

create index if not exists households_owner_idx on public.households (owner_id);
alter table public.households enable row level security;

drop policy if exists households_own on public.households;
create policy households_own on public.households
    for all
    using (owner_id = auth.uid())
    with check (owner_id = auth.uid());

-- ---------------------------------------------------------------------------
-- 1. Normalise the column type.
--    uuid -> text is always valid, so no data can be lost. Dependent policies
--    are dropped first because a policy referencing the column blocks the
--    alter; they are recreated below.
-- ---------------------------------------------------------------------------
do $$
begin
    if exists (
        select 1 from information_schema.columns
        where table_schema = 'public' and table_name = 'devices'
          and column_name = 'household_id' and data_type = 'uuid'
    ) then
        drop policy if exists devices_own on public.devices;
        alter table public.devices
            alter column household_id type text using household_id::text;
        raise notice 'devices.household_id: uuid -> text';
    else
        raise notice 'devices.household_id already text, nothing to change';
    end if;
end
$$;

-- ---------------------------------------------------------------------------
-- 2. Ownership policies, applied uniformly with an explicit cast.
--    service_role bypasses all of this, so the backend is unaffected.
-- ---------------------------------------------------------------------------
do $$
declare
    t text;
begin
    foreach t in array array[
        'energy_readings', 'observations', 'household_profiles',
        'devices', 'forecasts'
    ]
    loop
        if to_regclass(format('public.%I', t)) is null then
            raise notice 'skipping %, does not exist', t;
            continue;
        end if;

        execute format('alter table public.%I enable row level security', t);
        execute format('drop policy if exists %I on public.%I', t || '_own', t);
        execute format($f$
            create policy %I on public.%I
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
                )
        $f$, t || '_own', t);

        raise notice 'ownership policy applied to %', t;
    end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- 3. Verify. Every household-scoped table should now show policy_count = 1.
--    agent_cache has no household_id and stays at 0 with RLS on, which means
--    service_role only — correct, since nothing in the app reads it.
-- ---------------------------------------------------------------------------
select
    c.relname                                    as table_name,
    c.relrowsecurity                             as rls_enabled,
    (select count(*) from pg_policies p
      where p.schemaname = 'public'
        and p.tablename = c.relname)             as policy_count,
    (select data_type from information_schema.columns i
      where i.table_schema = 'public' and i.table_name = c.relname
        and i.column_name = 'household_id')      as household_id_type
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
order by c.relname;
