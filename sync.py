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
import sys
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
def fetch_projections(week, scoring_field):
    """Return {gsis_id: projected_points}, or {} if unavailable."""
    try:
        log("Fetching Sleeper player map...")
        pr = requests.get(SLEEPER_PLAYERS, timeout=180)
        pr.raise_for_status()
        sleeper_players = pr.json()

        sleeper_to_gsis = {}
        for sid, info in sleeper_players.items():
            gsis = (info or {}).get("gsis_id")
            if gsis:
                sleeper_to_gsis[str(sid)] = gsis.strip()
        log(f"  mapped {len(sleeper_to_gsis)} Sleeper players to NFL IDs")

        log(f"Fetching Sleeper projections for week {week}...")
        r = requests.get(
            SLEEPER_PROJECTIONS.format(season=SEASON, week=week),
            params={"season_type": "regular"},
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
        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("player_id", ""))
            stats = row.get("stats") if isinstance(row.get("stats"), dict) else row
            val = stats.get(scoring_field)
            if val is None:
                val = stats.get("pts_std")
            gsis = sleeper_to_gsis.get(sid)
            if gsis and val is not None:
                try:
                    out[gsis] = float(val)
                except (TypeError, ValueError):
                    pass
        log(f"  got {len(out)} projections")
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
            projections = fetch_projections(wk, field)

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
