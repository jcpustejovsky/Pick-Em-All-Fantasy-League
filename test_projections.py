"""
Exercises fetch_projections against mocked Sleeper responses.

The point: call it with player records shaped exactly the way
sb_select returns them, so a missing column fails here instead of
in production.
"""
import csv
import json
import sys
import types

import requests

# Load sync.py without running main()
src = open("sync.py", encoding="utf-8").read()
head = src.split("# ----------------------------------------------------------------\n# Main")[0]
sync = types.ModuleType("sync_under_test")
sync.__dict__["__name__"] = "sync_under_test"
exec(compile(head, "sync.py", "exec"), sync.__dict__)

# Player records EXACTLY as sb_select returns them (this is the part
# that broke: the query must include every column the code reads).
real = list(csv.DictReader(open("players_v3.csv", encoding="utf-8")))
players = [{"id": r["id"], "name": r["name"], "team": r["team"], "pos": r["pos"]}
           for r in real]

pass_n = fail_n = 0
def check(label, actual, expected):
    global pass_n, fail_n
    if actual == expected:
        pass_n += 1
        print(f"  PASS  {label}")
    else:
        fail_n += 1
        print(f"  FAIL  {label}\n        got {actual!r}\n        want {expected!r}")


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)[:200]
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def build_sleeper_map(players, gsis_fraction):
    """Mimic Sleeper: only some records carry gsis_id."""
    out = {}
    for i, p in enumerate(players):
        sid = f"s{i}"
        has = (i % 100) < (gsis_fraction * 100)
        out[sid] = {
            "gsis": p["id"] if has else None,
            "name": sync.norm_name(p["name"]),
            "team": p["team"],
        }
    return out


def run(projection_rows, gsis_fraction=0.32, scoring="pts_ppr"):
    sync._SLEEPER_MAP_CACHE = build_sleeper_map(players, gsis_fraction)
    orig_get = requests.get
    requests.get = lambda *a, **k: FakeResponse(projection_rows)
    try:
        return sync.fetch_projections(2, scoring, players)
    finally:
        requests.get = orig_get


print("\n=== the bug that shipped: player records missing a column ===")
sync._SLEEPER_MAP_CACHE = build_sleeper_map(players, 0.32)
thin = [{"id": p["id"], "team": p["team"], "pos": p["pos"]} for p in players]  # no 'name'
orig = requests.get
requests.get = lambda *a, **k: FakeResponse([])
try:
    res = sync.fetch_projections(2, "pts_ppr", thin)
finally:
    requests.get = orig
check("missing column no longer raises", isinstance(res, dict), True)

print("\n=== list-shaped response, 68% missing NFL ID (the real case) ===")
rows = []
for i, p in enumerate(players[:300]):
    rows.append({"player_id": f"s{i}", "stats": {"pts_ppr": 10.0 + (i % 17)}})
out = run(rows)
check("all 300 matched despite most lacking an NFL ID", len(out), 300)
check("keyed by our player id", all(k in {p['id'] for p in players} for k in out), True)

print("\n=== dict-shaped response ===")
as_dict = {f"s{i}": {"stats": {"pts_ppr": 5.0}} for i in range(50)}
out = run(as_dict)
check("dict shape handled", len(out), 50)

print("\n=== flat rows (no nested 'stats') ===")
flat = [{"player_id": f"s{i}", "pts_ppr": 7.5} for i in range(40)]
out = run(flat)
check("flat rows handled", len(out), 40)

print("\n=== scoring field fallback ===")
std_only = [{"player_id": f"s{i}", "stats": {"pts_std": 3.0}} for i in range(25)]
out = run(std_only, scoring="pts_ppr")
check("falls back to pts_std when ppr absent", len(out), 25)
check("fallback value used", set(out.values()), {3.0})

print("\n=== rows with no projection are skipped, not crashed on ===")
mixed = [{"player_id": "s0", "stats": {"pts_ppr": 12.0}},
         {"player_id": "s1", "stats": {}},
         {"player_id": "s2", "stats": {"pts_ppr": None}},
         {"player_id": "s3", "stats": {"pts_ppr": "nonsense"}},
         "not even a dict"]
out = run(mixed)
check("only the valid row survives", len(out), 1)

print("\n=== unknown players don't pollute results ===")
sync._SLEEPER_MAP_CACHE = {"zz": {"gsis": None, "name": "fake person", "team": "KC"}}
orig = requests.get
requests.get = lambda *a, **k: FakeResponse([{"player_id": "zz", "stats": {"pts_ppr": 99.0}}])
try:
    out = sync.fetch_projections(2, "pts_ppr", players)
finally:
    requests.get = orig
check("player not in our pool is ignored", out, {})

print("\n=== network failure degrades gracefully ===")
sync._SLEEPER_MAP_CACHE = build_sleeper_map(players, 0.32)
orig = requests.get
def boom(*a, **k):
    raise requests.ConnectionError("simulated outage")
requests.get = boom
try:
    out = sync.fetch_projections(2, "pts_ppr", players)
finally:
    requests.get = orig
check("outage returns empty dict, no crash", out, {})

print(f"\n{pass_n} passed, {fail_n} failed")
sys.exit(1 if fail_n else 0)
