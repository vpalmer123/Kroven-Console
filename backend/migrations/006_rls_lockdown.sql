-- Enable row level security across the whole public schema.
--
-- WHY THIS EXISTS
-- Supabase publishes every table in `public` through PostgREST, reachable with
-- the anon key. The anon key is not a secret — it ships in browsers by design.
-- The only thing standing between it and the data is RLS. A table with RLS off
-- is world-readable and, unless privileges say otherwise, world-writable.
--
-- 005_devices.sql created `devices` without enabling RLS, so household ids,
-- device names and the LAN addresses of the plugs were exposed. That is what
-- triggered the security advisor. 003 also enables RLS on energy_readings but
-- was never applied, and energy_readings/forecasts predate the migrations, so
-- their state cannot be assumed either.
--
-- Rather than naming tables and being wrong again the next time one is added,
-- this loops over every base table in `public` and enables RLS on all of them.
-- It is idempotent: enabling RLS twice is a no-op, so it is safe to re-run.
--
-- WHY NO POLICIES ARE CREATED
-- Kroven's backend connects with the service role key, which bypasses RLS
-- entirely. With RLS on and zero permissive policies, service_role keeps full
-- access and anon gets nothing. That is exactly the desired shape while the
-- browser never talks to Supabase directly — it goes through the FastAPI
-- backend on Railway, which holds the service key server-side.
--
-- IF YOU LATER LET THE BROWSER QUERY SUPABASE DIRECTLY, this will block it,
-- and the fix is to add a policy scoped to the household, not to disable RLS.

do $$
declare
    t record;
begin
    for t in
        select schemaname, tablename
        from pg_tables
        where schemaname = 'public'
    loop
        execute format(
            'alter table %I.%I enable row level security',
            t.schemaname, t.tablename
        );
        raise notice 'RLS enabled: %.%', t.schemaname, t.tablename;
    end loop;
end
$$;

-- Belt and braces: take away the table-level grants PostgREST's anon role
-- would otherwise use. RLS alone is enough, but revoking means a future table
-- created with RLS off is still not reachable anonymously by default.
revoke all on all tables in schema public from anon;
revoke all on all sequences in schema public from anon;
alter default privileges in schema public revoke all on tables from anon;
alter default privileges in schema public revoke all on sequences from anon;

-- Verify. Every row should read rls_enabled = true.
select
    c.relname                as table_name,
    c.relrowsecurity         as rls_enabled,
    (select count(*) from pg_policies p
      where p.schemaname = 'public' and p.tablename = c.relname) as policy_count
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
order by c.relname;
