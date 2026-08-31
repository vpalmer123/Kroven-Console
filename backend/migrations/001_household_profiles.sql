-- Stores what a user told Kroven about their home, once, so it never has to
-- ask again. Run this in the Supabase SQL editor.
--
-- Until this table exists, chat still works — _load_profile() treats a missing
-- table as "no profile yet" — but nothing is remembered between sessions.

create table if not exists public.household_profiles (
    household_id      text primary key,
    monthly_kwh       double precision,
    monthly_bill_usd  double precision,
    home_notes        text,
    updated_at        timestamptz not null default now()
);

-- Keep updated_at honest on upsert.
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

-- The backend connects with the service key, which bypasses RLS. RLS is enabled
-- with no permissive policy so that anon/public keys cannot read other people's
-- profiles if the table is ever exposed.
alter table public.household_profiles enable row level security;
