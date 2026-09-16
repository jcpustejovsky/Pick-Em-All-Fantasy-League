#!/usr/bin/env python3
"""
Pick-Em-All sync.

Pulls three things into Supabase:
  1. week_info      - when each week's picks lock (first Sunday kickoff)
  2. weekly_players - each player's Sunday opponent, kickoff, projection
  3. weekly_points  - actual fantasy points, once games are final

Run by GitHub Actions on a schedule. Safe to run repeatedly; every
write is an upsert.

Data sources
------------
Schedules and stats: nflverse (github.com/nflverse/nflverse-data).
  Free, open, actively maintained. Stats only appear after games
  finish and the dataset refreshes, so this is final-score data, not
  live in-game scoring.

Projections: Sleeper's projections endpoint. This endpoint is NOT in
  Sleeper's published docs - it's one the developer community found
  and relies on. It may change or disappear without notice. That is
  why projections are treated as OPTIONAL here: if the fetch fails,
  the script logs it, skips projections, and everything else still
  syncs. Projections only control the sort order of the pick list,
  so losing them degrades convenience, not the league itself.
"""

import csv
import io
import os
import re
import sys
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

SEASON = int(os.environ.get("SEASON", "2026"))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
SCHEDULE_URL = f"{NFLVERSE}/schedules/games.csv"
STATS_URL = f"{NFLVERSE}/stats_player/stats_player_week_{SEASON}.csv"

SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
SLEEPER_PROJECTIONS = "https://api.sleeper.com/projections/nfl/{season}/{week}"

ET = ZoneInfo("America/New_York")


def log(msg):
    print(msg, flush=True)


def fail(msg):
    log(f"ERROR: {msg}")
    sys.exit(1)


def compute_current_week(sched, now=None):
    """
    The week the league should be on right now.

    A week stays current until roughly 6 hours after its last game
    kicks off (so Monday night finishing rolls it to the next week).
    Before the season starts this returns week 1; after it ends, the
    final week.
    """
    from datetime import timedelta

    now = now or datetime.now(tz=ET)
    last_kick = {}
    for g in sched:
        if not g["gameday"] or not g["gametime"]:
            continue
        try:
            dt = datetime.strptime(
                f"{g['gameday']} {g['gametime']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=ET)
        except ValueError:
            continue
        wk = int(g["week"])
        if wk not in last_kick or dt > last_kick[wk]:
            last_kick[wk] = dt

    if not last_kick:
        return None
    for wk in sorted(last_kick):
        if last_kick[wk] + timedelta(hours=6) > now:
            return wk
    return max(last_kick)


def norm_team(t):
    """nflverse uses LA for the Rams; the app uses LAR."""
    return "LAR" if t == "LA" else t


# ----------------------------------------------------------------
# Supabase helpers
# ----------------------------------------------------------------
def sb_headers(extra=None):
    h = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def sb_select(table, params=None):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=sb_headers(),
        params=params or {"select": "*"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def sb_upsert(table, rows, on_conflict):
    """Upsert in batches. Service key bypasses row-level security."""
    if not rows:
        return
    for i in range(0, len(rows), 500):
        batch = rows[i : i + 500]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            params={"on_conflict": on_conflict},
            json=batch,
            timeout=120,
        )
        if r.status_code >= 300:
            fail(f"upsert into {table} failed ({r.status_code}): {r.text[:500]}")
    log(f"  upserted {len(rows)} rows into {table}")


def fetch_csv(url, label):
    log(f"Downloading {label}...")
    r = requests.get(url, timeout=180)
    if r.status_code != 200:
        fail(f"could not download {label} ({r.status_code}) from {url}")
    return list(csv.DictReader(io.StringIO(r.text)))


# ----------------------------------------------------------------
# Projections (optional - see module docstring)
# ----------------------------------------------------------------
def norm_name(n):
    """
    Normalize a player name for matching across data sources.
    Handles accents, suffixes (Jr./III), periods (D.J. vs DJ) and
    apostrophes (O'Connell).
    """
    if not n:
        return ""
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[.'`]", "", n)
    n = re.sub(r"[^a-z ]", " ", n)
    parts = [p for p in n.split() if p not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return " ".join(parts)


_SLEEPER_MAP_CACHE = None


def sleeper_player_map():
    """
    {sleeper_id: {"gsis": ..., "name": ..., "team": ...}}, fetched at
    most once per run.

    Sleeper's docs ask callers to hit this endpoint sparingly (it returns
    every player in the league and is a large download), so this is
    cached rather than re-fetched for each week.

    Name and team are kept because a large share of Sleeper records have
    no gsis_id — name matching is the fallback that recovers them.
    """
    global _SLEEPER_MAP_CACHE
    if _SLEEPER_MAP_CACHE is not None:
        return _SLEEPER_MAP_CACHE

    log("Fetching Sleeper player map (once per run)...")
    pr = requests.get(SLEEPER_PLAYERS, timeout=180)
    pr.raise_for_status()
    mapping = {}
    with_gsis = 0
    for sid, info in (pr.json() or {}).items():
        if not isinstance(info, dict):
            continue
        gsis = (info.get("gsis_id") or "").strip()
        name = info.get("full_name") or " ".join(
            x for x in (info.get("first_name"), info.get("last_name")) if x
        )
        mapping[str(sid)] = {
            "gsis": gsis or None,
            "name": norm_name(name),
            "team": norm_team((info.get("team") or "").strip().upper()),
        }
        if gsis:
            with_gsis += 1
    log(f"  {len(mapping)} Sleeper players ({with_gsis} carry an NFL ID)")
    _SLEEPER_MAP_CACHE = mapping
    return mapping


def fetch_projections(week, scoring_field, players):
    """
    Return {our_player_id: projected_points}, or {} if unavailable.

    Matching runs in three passes, most reliable first:
      1. Sleeper's gsis_id, when present
      2. normalized name + team
      3. normalized name alone (covers recent trades, where the two
         sources disagree on team)
    Pass 3 is only safe because names are unique in our player pool;
    the script checks that and skips the pass if they aren't.
    """
    try:
        smap = sleeper_player_map()

        by_gsis = {p["id"]: p["id"] for p in players}
        by_name_team = {}
        name_counts = {}
        for p in players:
            nn = norm_name(p["name"])
            by_name_team[(nn, p["team"])] = p["id"]
            name_counts[nn] = name_counts.get(nn, 0) + 1
        by_name = {
            norm_name(p["name"]): p["id"]
            for p in players
            if name_counts[norm_name(p["name"])] == 1
        }

        log(f"Fetching Sleeper projections for week {week}...")
        r = requests.get(
            SLEEPER_PROJECTIONS.format(season=SEASON, week=week),
            params=[
                ("season_type", "regular"),
                ("position[]", "QB"),
                ("position[]", "RB"),
                ("position[]", "WR"),
                ("position[]", "TE"),
                ("order_by", scoring_field),
            ],
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()

        # The endpoint has returned both a list of rows and a dict keyed
        # by player id at different times. Handle both.
        if isinstance(data, dict):
            rows = []
            for sid, v in data.items():
                if isinstance(v, dict):
                    row = dict(v)
                    row.setdefault("player_id", sid)
                    rows.append(row)
        else:
            rows = data

        out = {}
        hits = {"gsis": 0, "name_team": 0, "name": 0}
        no_value = 0
        unmatched = []

        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("player_id", ""))
            stats = row.get("stats") if isinstance(row.get("stats"), dict) else row
            val = stats.get(scoring_field)
            if val is None:
                val = stats.get("pts_std")
            if val is None:
                no_value += 1
                continue
            try:
                val = float(val)
            except (TypeError, ValueError):
                no_value += 1
                continue

            info = smap.get(sid) or {}
            pid = None
            if info.get("gsis") and info["gsis"] in by_gsis:
                pid = by_gsis[info["gsis"]]
                hits["gsis"] += 1
            elif info.get("name"):
                key = (info["name"], info.get("team") or "")
                if key in by_name_team:
                    pid = by_name_team[key]
                    hits["name_team"] += 1
                elif info["name"] in by_name:
                    pid = by_name[info["name"]]
                    hits["name"] += 1

            if pid:
                # Keep the best value if a player somehow appears twice.
                if pid not in out or val > out[pid]:
                    out[pid] = val
            elif info.get("name"):
                unmatched.append(f"{info['name']} ({info.get('team') or '?'})")

        log(f"  {len(rows)} rows returned, {len(out)} matched to our players")
        log(f"    by NFL ID: {hits['gsis']}, by name+team: {hits['name_team']}, by name: {hits['name']}")
        log(f"    {no_value} rows had no projection value (expected — most players project zero)")
        if unmatched:
            log(f"    {len(unmatched)} projected players not in our pool, e.g. {', '.join(unmatched[:5])}")
        return out

    except Exception as e:
        log(f"WARNING: projections unavailable ({type(e).__name__}: {e}).")
        log("         Continuing without them - the pick list will fall back")
        log("         to alphabetical order. Nothing else is affected.")
        return {}


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
def main():
    if not SUPABASE_URL or not SERVICE_KEY:
        fail("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.")

    meta_rows = sb_select("league_meta", {"select": "*", "id": "eq.1"})
    if not meta_rows:
        fail("league_meta row not found - run the schema/migrations first.")
    meta = meta_rows[0]
    current_week = int(meta.get("current_week") or 1)
    scoring_mode = (meta.get("scoring_mode") or "standard").lower()
    log(f"League: week {current_week}, scoring '{scoring_mode}'")

    players = sb_select("players", {"select": "id,team,pos"})
    if not players:
        fail("players table is empty - import the player CSV first.")
    by_team = {}
    for p in players:
        by_team.setdefault(p["team"], []).append(p)
    known_ids = {p["id"] for p in players}
    log(f"{len(players)} players across {len(by_team)} teams")

    # ---------------- schedule ----------------
    sched = [
        r for r in fetch_csv(SCHEDULE_URL, "schedules")
        if r["season"] == str(SEASON) and r["game_type"] == "REG"
    ]
    log(f"{len(sched)} regular season games for {SEASON}")

    # Advance the league's current week automatically.
    auto_week = compute_current_week(sched)
    if auto_week and auto_week != current_week:
        log(f"Advancing current week: {current_week} -> {auto_week}")
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/league_meta",
            headers=sb_headers({"Prefer": "return=minimal"}),
            params={"id": "eq.1"},
            json={"current_week": auto_week},
            timeout=60,
        )
        if r.status_code >= 300:
            log(f"WARNING: couldn't update current_week ({r.status_code}): {r.text[:300]}")
        else:
            current_week = auto_week
    elif auto_week:
        log(f"Current week is already {current_week}")

    weeks = sorted({int(r["week"]) for r in sched})
    week_rows = []
    weekly_player_rows = []

    for wk in weeks:
        games = [r for r in sched if int(r["week"]) == wk]
        sunday = [g for g in games if g["weekday"] == "Sunday"]

        kickoffs = []
        for g in sunday:
            if not g["gameday"] or not g["gametime"]:
                continue
            try:
                dt = datetime.strptime(
                    f"{g['gameday']} {g['gametime']}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=ET)
                kickoffs.append((dt, g))
            except ValueError:
                continue

        lock_time = min(k[0] for k in kickoffs) if kickoffs else None
        week_rows.append({
            "week": wk,
            "season": SEASON,
            "lock_time": lock_time.isoformat() if lock_time else None,
            "sunday_games": len(sunday),
        })

        # Only weeks near the present need per-player rows; syncing all 18
        # every run would be a lot of writes for no benefit.
        if not (current_week - 1 <= wk <= current_week + 2):
            continue

        projections = {}
        if wk >= current_week:
            field = {
                "ppr": "pts_ppr",
                "half": "pts_half_ppr",
                "standard": "pts_std",
            }.get(scoring_mode, "pts_std")
            projections = fetch_projections(wk, field, players)

        for g in games:
            is_sunday = g["weekday"] == "Sunday"
            kickoff = None
            if g["gameday"] and g["gametime"]:
                try:
                    kickoff = datetime.strptime(
                        f"{g['gameday']} {g['gametime']}", "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=ET).isoformat()
                except ValueError:
                    kickoff = None

            home, away = norm_team(g["home_team"]), norm_team(g["away_team"])
            for team, opp in ((home, away), (away, home)):
                for p in by_team.get(team, []):
                    weekly_player_rows.append({
                        "player_id": p["id"],
                        "week": wk,
                        "opponent": opp,
                        "kickoff": kickoff,
                        "is_sunday": is_sunday,
                        "projected_points": projections.get(p["id"]),
                    })

    log("Writing week_info...")
    sb_upsert("week_info", week_rows, "week")
    log("Writing weekly_players...")
    sb_upsert("weekly_players", weekly_player_rows, "player_id,week")

    # ---------------- coverage report ----------------
    # Does every Sunday team have a projected QB, RB, etc.? If the pick
    # list looks short, this says whether data is missing or whether the
    # pool is simply that size.
    pos_by_id = {p["id"]: p["pos"] for p in players}
    for wk in sorted({r["week"] for r in weekly_player_rows}):
        rows_wk = [r for r in weekly_player_rows if r["week"] == wk and r["is_sunday"]]
        if not rows_wk:
            continue
        teams_playing = len({
            norm_team(g["home_team"]) for g in sched
            if int(g["week"]) == wk and g["weekday"] == "Sunday"
        } | {
            norm_team(g["away_team"]) for g in sched
            if int(g["week"]) == wk and g["weekday"] == "Sunday"
        })
        log(f"  Week {wk} coverage — {teams_playing} teams play Sunday:")
        for pos in ("QB", "RB", "WR", "TE"):
            in_pool = [r for r in rows_wk if pos_by_id.get(r["player_id"]) == pos]
            with_proj = [r for r in in_pool if r["projected_points"] is not None]
            teams_with_any = len({
                p["team"] for p in players
                if p["pos"] == pos and any(
                    r["player_id"] == p["id"] for r in with_proj
                )
            })
            flag = ""
            if pos == "QB" and teams_with_any < teams_playing:
                flag = f"  <-- only {teams_with_any}/{teams_playing} teams have a projected QB"
            log(f"    {pos}: {len(in_pool)} in pool, {len(with_proj)} projected,"
                f" covering {teams_with_any}/{teams_playing} teams{flag}")

    # ---------------- actual results ----------------
    log("Downloading stats...")
    r = requests.get(STATS_URL, timeout=180)
    if r.status_code != 200:
        log(f"No stats file yet for {SEASON} ({r.status_code}) - skipping scoring.")
        log("Done.")
        return

    stats = list(csv.DictReader(io.StringIO(r.text)))
    log(f"{len(stats)} stat rows")

    point_rows = []
    skipped = 0
    for s in stats:
        pid = s.get("player_id", "").strip()
        if pid not in known_ids:
            skipped += 1
            continue
        std = s.get("fantasy_points")
        ppr = s.get("fantasy_points_ppr")
        try:
            std_v = float(std) if std not in (None, "", "NA") else None
            ppr_v = float(ppr) if ppr not in (None, "", "NA") else None
        except ValueError:
            continue
        if std_v is None:
            continue

        if scoring_mode == "ppr":
            pts = ppr_v if ppr_v is not None else std_v
        elif scoring_mode == "half":
            # half-PPR is exactly the midpoint of standard and full PPR
            pts = (std_v + ppr_v) / 2 if ppr_v is not None else std_v
        else:
            pts = std_v

        try:
            wk = int(s["week"])
        except (KeyError, ValueError):
            continue

        point_rows.append({
            "player_id": pid,
            "week": wk,
            "points": round(pts, 2),
        })

    log(f"{len(point_rows)} scored rows ({skipped} stat rows for players not in the pool)")
    log("Writing weekly_points...")
    sb_upsert("weekly_points", point_rows, "player_id,week")

    log("Done.")


if __name__ == "__main__":
    main()
