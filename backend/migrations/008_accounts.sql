-- Real accounts, and data ownership that follows from them.
--
-- Until now a "household" was a UUID the browser invented and put in
-- localStorage. Nothing tied it to a person, so nothing could tell two users
-- apart, and the RLS enabled in 006/007 had no identity to scope on — which is
-- why it runs with zero policies and only the service-role backend can read
-- anything.
--
-- This adds the missing half: every household is owned by a row in
-- auth.users. Once ownership exists, RLS policies can express the rule that
-- actually matters — you may touch your own rows and no one else's — and the
-- database enforces it even if the application layer has a bug.
--
-- Ownership lives in one table rather than an owner_id column on every data
-- table. That way an existing household keeps its id when it gains an owner,
-- so the 1800 readings already collected stay attached to Victor's account
-- instead of needing to be rewritten.
--
-- SUPERSEDED IN PART BY 009. As written this failed on `devices`, whose
-- household_id was uuid while every other table's is text, so the ownership
-- subquery compared uuid against text (42883) and stopped halfway. 009
-- normalises the column and recreates every policy with an explicit cast.
-- Run 009 after this; it is idempotent and safe on a half-applied schema.

create table if not exists public.households (
    household_id  text primary key,
    owner_id      uuid not null references auth.users(id) on delete cascade,
    display_name  text,
    created_at    timestamptz not null default now()
);

create index if not exists households_owner_idx on public.households (owner_id);

alter table public.households enable row level security;

-- A user may see their own household rows and nothing else.
drop policy if exists households_own on public.households;
create policy households_own on public.households
    for all
    using (owner_id = auth.uid())
    with check (owner_id = auth.uid());

-- ---------------------------------------------------------------------------
-- Data tables: you may touch rows for a household you own.
--
-- Written as a loop so every household-scoped table gets the identical rule
-- and a table added later is one line away from being covered, rather than
-- silently unprotected — which is exactly how `devices` went wrong.
--
-- service_role still bypasses all of this, so the backend is unaffected.
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
                    household_id in (
                        select household_id from public.households
                        where owner_id = auth.uid()
                    )
                )
                with check (
                    household_id in (
                        select household_id from public.households
                        where owner_id = auth.uid()
                    )
                )
        $f$, t || '_own', t);

        raise notice 'ownership policy applied to %', t;
    end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- Claim the household that already has data.
--
-- Run AFTER signing up, replacing the email. Without this the 1800 readings
-- collected so far would belong to no account and would not appear once the
-- app starts scoping by user.
--
--   insert into public.households (household_id, owner_id, display_name)
--   select 'e99c9cfc-0671-4869-ad27-63c838ed884d', id, 'Home'
--   from auth.users where email = 'you@example.com'
--   on conflict (household_id) do update set owner_id = excluded.owner_id;
-- ---------------------------------------------------------------------------

select
    c.relname                as table_name,
    c.relrowsecurity         as rls_enabled,
    (select count(*) from pg_policies p
      where p.schemaname = 'public' and p.tablename = c.relname) as policy_count
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
order by c.relname;
