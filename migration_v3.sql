-- ============================================================
-- Migration v3 — new format.
-- Run ONCE in Supabase SQL Editor. Safe to re-run.
--
-- WARNING: this DELETES all existing picks and weekly points.
-- Player IDs are changing (from p1, p2, ... to the NFL's own
-- player IDs) so that stats can be matched automatically. Old
-- picks point at IDs that will no longer exist, so they can't
-- be carried over. Nothing else is lost — accounts, team names
-- and commissioner status all stay.
-- ============================================================

-- 1. Clear data that references old player IDs.
delete from picks;
delete from weekly_points;
delete from players;

-- 2. League settings: add scoring mode.
alter table league_meta add column if not exists scoring_mode text not null default 'standard';

-- 3. Per-week info: when picks lock for that week.
create table if not exists week_info (
  week int primary key,
  season int not null,
  lock_time timestamptz,          -- kickoff of the FIRST Sunday game that week
  sunday_games int not null default 0
);

-- 4. Per-player, per-week context: who they play, when, and projection.
create table if not exists weekly_players (
  player_id text not null references players(id) on delete cascade,
  week int not null,
  opponent text,
  kickoff timestamptz,
  is_sunday boolean not null default false,
  projected_points numeric,
  primary key (player_id, week)
);
create index if not exists weekly_players_week_idx on weekly_players (week);

alter table week_info enable row level security;
alter table weekly_players enable row level security;

drop policy if exists "week_info readable by everyone" on week_info;
create policy "week_info readable by everyone" on week_info for select using (true);

drop policy if exists "weekly_players readable by everyone" on weekly_players;
create policy "weekly_players readable by everyone" on weekly_players for select using (true);

-- 5. Roster rules, enforced in the database (not just the UI).
--    2 QB, 3 RB, 3 WR/TE (TEs count in the WR group). 8 total.
--    SECURITY DEFINER so these can read `picks` without the
--    row-level rules on `picks` recursing into themselves.

create or replace function week_unlocked(p_week int)
returns boolean
language sql
security definer
set search_path = public
as $$
  select coalesce((select lock_time from week_info where week = p_week) > now(), true);
$$;

create or replace function can_add_pick(p_user uuid, p_week int, p_player text)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  grp text;
  cap int;
  cnt int;
begin
  select case when pos in ('WR','TE') then 'WRTE' else pos end
    into grp from players where id = p_player;
  if grp is null then
    return false;                        -- unknown player
  end if;

  cap := case grp when 'QB' then 2 when 'RB' then 3 when 'WRTE' then 3 else 0 end;
  if cap = 0 then
    return false;                        -- position not used in this format
  end if;

  select count(*) into cnt
    from picks pk
    join players pl on pl.id = pk.player_id
   where pk.user_id = p_user
     and pk.week = p_week
     and (case when pl.pos in ('WR','TE') then 'WRTE' else pl.pos end) = grp;

  if cnt >= cap then
    return false;                        -- that position group is full
  end if;

  return week_unlocked(p_week);
end;
$$;

-- 6. Re-point the picks policies at those rules.
drop policy if exists "users can insert their own picks" on picks;
create policy "users can insert their own picks" on picks
  for insert with check (
    auth.uid() = user_id
    and can_add_pick(user_id, week, player_id)
  );

drop policy if exists "users can update their own picks" on picks;
create policy "users can update their own picks" on picks
  for update using (auth.uid() = user_id and week_unlocked(week));

drop policy if exists "users can delete their own picks" on picks;
create policy "users can delete their own picks" on picks
  for delete using (auth.uid() = user_id and week_unlocked(week));

-- 7. Picks stay visible to everyone (that's the format), but only
--    AFTER the week locks — before that, hiding them stops managers
--    from copying each other's rosters at the last minute.
drop policy if exists "picks are viewable by all logged-in users" on picks;
drop policy if exists "picks visible after lock or if your own" on picks;
create policy "picks visible after lock or if your own" on picks
  for select using (
    auth.uid() = user_id
    or not week_unlocked(week)
  );
