-- Finish the lockdown started in 006, and report the result.
--
-- 006 enabled RLS on every table and revoked anon's grants. This adds the
-- `authenticated` role, which 006 left alone.
--
-- RLS with zero policies already blocks `authenticated`, so this is belt and
-- braces rather than a hole being closed. It matters for one specific future:
-- if a table is ever created with RLS off (exactly how `devices` went wrong),
-- these revokes mean it still is not reachable by a logged-in browser client.
--
-- WHY THERE ARE STILL NO POLICIES
-- Kroven has no Supabase Auth. There is no login, no JWT, no auth.uid(). A
-- household is a UUID the browser generates into localStorage. So there is no
-- identity for a policy to test: `using (household_id = auth.uid())` would
-- compare against null and deny everything, while looking like security.
--
-- All real access is the FastAPI backend using the service role key, which
-- bypasses RLS by design. RLS on + zero policies is therefore the correct
-- shape: service_role has full access, everyone else has none.
--
-- WHEN THIS MUST CHANGE
-- The moment a browser queries Supabase directly, add Supabase Auth and a
-- policy per table of the form
--     using (household_id = auth.uid()::text)
-- Do not instead write `using (true)` to make it work — that is the same as
-- having no RLS, while looking like it is protected.
--
-- Safe to re-run.

revoke all on all tables in schema public from authenticated;
revoke all on all sequences in schema public from authenticated;
alter default privileges in schema public revoke all on tables from authenticated;
alter default privileges in schema public revoke all on sequences from authenticated;

-- Re-assert RLS everywhere, in case a table was added since 006.
do $$
declare
    t record;
begin
    for t in select schemaname, tablename from pg_tables where schemaname = 'public'
    loop
        execute format('alter table %I.%I enable row level security',
                       t.schemaname, t.tablename);
    end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- AFTER STATE. Every row must show rls_enabled = true.
-- policy_count = 0 is CORRECT here — see the note above.
-- ---------------------------------------------------------------------------
select
    c.relname                                        as table_name,
    c.relrowsecurity                                 as rls_enabled,
    (select count(*) from pg_policies p
      where p.schemaname = 'public'
        and p.tablename = c.relname)                 as policy_count,
    has_table_privilege('anon',          format('public.%I', c.relname), 'SELECT') as anon_can_select,
    has_table_privilege('authenticated', format('public.%I', c.relname), 'SELECT') as auth_can_select
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
order by c.relname;
