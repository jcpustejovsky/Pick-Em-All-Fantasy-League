"""Verifies the reconstructed sync.py against real schedule data + mocked Sleeper."""
import csv
import json
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")

# Load sync.py without running main()
src = open("sync_test_target.py", encoding="utf-8").read()
head = src.split("# ----------------------------------------------------------------\n# Main")[0]
sync = types.ModuleType("sync_under_test")
exec(compile(head, "sync.py", "exec"), sync.__dict__)

pass_n = fail_n = 0
def check(label, actual, expected):
    global pass_n, fail_n
    if actual == expected:
        pass_n += 1
        print(f"  PASS  {label}")
    else:
        fail_n += 1
        print(f"  FAIL  {label}\n        got {actual!r}\n        want {expected!r}")

sched = [r for r in csv.DictReader(open("sched.csv", encoding="utf-8"))
         if r["season"] == "2026" and r["game_type"] == "REG"]

print("\n=== week flips after SUNDAY, not Monday night ===")
def at(ts):
    return sync.compute_current_week(
        sched, datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=ET))

check("Sunday wk2 midday",            at("2026-09-20 12:00"), 2)
check("Sunday wk2 during night game", at("2026-09-20 23:00"), 2)
check("Monday 1am (just ended)",      at("2026-09-21 01:00"), 2)
check("Monday 3am -> week 3",         at("2026-09-21 03:00"), 3)
check("Monday 6am -> week 3",         at("2026-09-21 06:00"), 3)
check("Monday night MNF -> still 3",  at("2026-09-21 21:00"), 3)
check("Tuesday -> 3",                 at("2026-09-22 09:00"), 3)
check("before season -> 1",           at("2026-08-01 12:00"), 1)
check("after season -> 18",           at("2027-03-01 12:00"), 18)

print("\n=== lock time is the FIRST Sunday kickoff ===")
wk2 = [g for g in sched if int(g["week"]) == 2 and g["weekday"] == "Sunday"]
first = min(datetime.strptime(f"{g['gameday']} {g['gametime']}", "%Y-%m-%d %H:%M")
            .replace(tzinfo=ET) for g in wk2)
check("wk2 locks at 1:00 PM ET Sunday", first.strftime("%Y-%m-%d %H:%M"), "2026-09-20 13:00")
wk4 = [g for g in sched if int(g["week"]) == 4 and g["weekday"] == "Sunday"]
first4 = min(datetime.strptime(f"{g['gameday']} {g['gametime']}", "%Y-%m-%d %H:%M")
             .replace(tzinfo=ET) for g in wk4)
check("wk4 locks early (intl game)", first4.hour < 13, True)

print("\n=== name normalization ===")
check("suffix stripped",   sync.norm_name("Marvin Harrison Jr."), "marvin harrison")
check("apostrophe",        sync.norm_name("Aidan O'Connell"), "aidan oconnell")
check("periods collapsed", sync.norm_name("D.J. Moore"), "dj moore")
check("empty safe",        sync.norm_name(None), "")
check("Rams normalized",   sync.norm_team("LA"), "LAR")

print("\n=== projection matching ===")
players = [
    {"id": "g1", "name": "Josh Palmer",    "team": "BUF", "pos": "WR"},
    {"id": "g2", "name": "Matthew Hibner", "team": "BAL", "pos": "TE"},
    {"id": "g3", "name": "Josh Allen",     "team": "BUF", "pos": "QB"},
    {"id": "g4", "name": "Kyle Allen",     "team": "BUF", "pos": "QB"},
    {"id": "g5", "name": "Bijan Robinson", "team": "ATL", "pos": "RB"},
    {"id": "g6", "name": "Brian Robinson", "team": "ATL", "pos": "RB"},
    {"id": "g7", "name": "Puka Nacua",     "team": "LAR", "pos": "WR"},
]

class FakeResponse:
    def __init__(self, payload): self._p = payload; self.status_code = 200; self.text = ""
    def json(self): return self._p
    def raise_for_status(self): pass

def run(cases, scoring="pts_ppr"):
    smap, rows = {}, []
    for i, (gsis, name, team, pos) in enumerate(cases):
        sid = f"s{i}"
        smap[sid] = {"gsis": gsis, "name": sync.norm_name(name), "team": team, "pos": pos}
        rows.append({"player_id": sid, "stats": {scoring: 10.0 + i}})
    sync._SLEEPER_MAP_CACHE = smap
    og = requests.get
    requests.get = lambda *a, **k: FakeResponse(rows)
    try:
        return sync.fetch_projections(2, scoring, players)
    finally:
        requests.get = og

out = run([("g1", "Josh Palmer", "BUF", "WR")])
check("pass 1: matches by NFL ID", set(out), {"g1"})

out = run([(None, "Josh Palmer", "BUF", "WR")])
check("pass 2: matches by name+team", set(out), {"g1"})

out = run([(None, "Puka Nacua", "WRONGTEAM", "WR")])
check("pass 3: traded player by name alone", set(out), {"g7"})

out = run([(None, "Joshua Palmer", "BUF", "WR")])
check("pass 4: nickname via last name", set(out), {"g1"})

out = run([(None, "Matt Hibner", "BAL", "TE")])
check("pass 4: Matt -> Matthew", set(out), {"g2"})

out = run([(None, "Joshua Allen", "BUF", "QB")])
check("AMBIGUOUS last name refused (Kyle Allen)", out, {})

out = run([(None, "Bryan Robinson", "ATL", "RB")])
check("AMBIGUOUS refused (Brian Robinson)", out, {})

out = run([(None, "Nobody Here", "KC", "WR")])
check("unknown player ignored", out, {})

print("\n=== response shapes and bad data ===")
sync._SLEEPER_MAP_CACHE = {"s0": {"gsis": "g1", "name": "josh palmer", "team": "BUF", "pos": "WR"}}
og = requests.get
requests.get = lambda *a, **k: FakeResponse({"s0": {"stats": {"pts_ppr": 12.0}}})
try:
    out = sync.fetch_projections(2, "pts_ppr", players)
finally:
    requests.get = og
check("dict-shaped response", out, {"g1": 12.0})

requests.get = lambda *a, **k: FakeResponse([{"player_id": "s0", "pts_ppr": 8.0}])
try:
    out = sync.fetch_projections(2, "pts_ppr", players)
finally:
    requests.get = og
check("flat rows (no nested stats)", out, {"g1": 8.0})

requests.get = lambda *a, **k: FakeResponse([{"player_id": "s0", "stats": {"pts_std": 4.0}}])
try:
    out = sync.fetch_projections(2, "pts_ppr", players)
finally:
    requests.get = og
check("falls back to pts_std", out, {"g1": 4.0})

requests.get = lambda *a, **k: FakeResponse(
    [{"player_id": "s0", "stats": {"pts_ppr": None}},
     {"player_id": "s0", "stats": {"pts_ppr": "junk"}},
     "not a dict"])
try:
    out = sync.fetch_projections(2, "pts_ppr", players)
finally:
    requests.get = og
check("bad rows skipped, no crash", out, {})

def boom(*a, **k):
    raise requests.ConnectionError("simulated outage")
requests.get = boom
try:
    out = sync.fetch_projections(2, "pts_ppr", players)
finally:
    requests.get = og
check("network outage degrades gracefully", out, {})

# The bug that shipped once: player records missing a column.
thin = [{"id": "g1", "team": "BUF", "pos": "WR"}]
sync._SLEEPER_MAP_CACHE = {"s0": {"gsis": "g1", "name": "x", "team": "BUF", "pos": "WR"}}
requests.get = lambda *a, **k: FakeResponse([])
try:
    out = sync.fetch_projections(2, "pts_ppr", thin)
finally:
    requests.get = og
check("missing 'name' column doesn't crash", isinstance(out, dict), True)

print("\n=== scoring conversion ===")
def score(std, ppr, mode):
    if mode == "ppr":
        return ppr if ppr is not None else std
    if mode == "half":
        return (std + ppr) / 2 if ppr is not None else std
    return std
check("standard", score(10.0, 15.0, "standard"), 10.0)
check("ppr",      score(10.0, 15.0, "ppr"), 15.0)
check("half",     score(10.0, 15.0, "half"), 12.5)
check("ppr missing falls back", score(10.0, None, "ppr"), 10.0)

print(f"\n{pass_n} passed, {fail_n} failed")
sys.exit(1 if fail_n else 0)
