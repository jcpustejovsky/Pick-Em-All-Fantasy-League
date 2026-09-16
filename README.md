# Pick 'Em All — v3

## The format

- Everyone can pick anyone. Two managers can roster the same player.
- **8 players, no bench.** All 8 score.
  - 2 QB
  - 3 RB
  - 3 WR/TE (tight ends count in the WR group)
- **No kickers.**
- **Sunday games only.** Players on teams playing Thursday or Monday
  aren't in the pool that week.
- **Everything locks at the first Sunday kickoff.** Change picks as
  often as you like before then; after it, rosters are final for
  everyone, including players in later games.
- Each week, teams above the league median score get a win, below get
  a loss, exactly at it a tie.
- Empty roster spots score zero. Nothing is auto-filled.

---

## Updating from v2

### 1. Run the migration

Paste `migration_v3.sql` into Supabase's **SQL Editor** and run it.

**This deletes all existing picks and weekly points.** Player IDs are
changing from `p1, p2, …` to the NFL's own player IDs so that stats can
be matched to players automatically — old picks point at IDs that stop
existing. Accounts, team names and commissioner status are untouched.

### 2. Re-import the player list

**Table Editor > players**. Delete any remaining rows, then
**Insert > Import data from CSV** with `players_v3.csv`.
497 players, no kickers.

### 3. Add the sync files to your repo

Copy into `C:\Dev\Pick-Em-All-Fantasy-League`:

- `sync.py` → repo root
- `.github/workflows/sync.yml` → keep that exact folder path

Then:

```powershell
git add .
git commit -m "v3: new format, automated sync"
git push
```

### 4. Add two GitHub secrets

Repo → **Settings > Secrets and variables > Actions > New repository secret**:

| Name | Value |
|---|---|
| `SUPABASE_URL` | your project URL |
| `SUPABASE_SERVICE_KEY` | the **service_role** key (Project Settings > API) |

The service_role key bypasses all access rules — that's why the sync
job can write scores. Never put it in `index.html` or anywhere in the
frontend; GitHub secrets are not readable from the repo.

### 5. Run the sync once by hand

Repo → **Actions** tab → **Sync NFL data** → **Run workflow**.

Watch the log. It tells you what it found. Until this runs, the pick
list will be empty — the app has no schedule or projection data yet.

### 6. Re-upload `index.html`

Drop the new one into your repo folder, then `git add/commit/push`.
Vercel redeploys on its own.

---

## Where the data comes from

**Schedules and final stats — nflverse** (github.com/nflverse/nflverse-data).
Free, open, actively maintained. I verified directly that its stats
file carries a precomputed `fantasy_points` column for standard and PPR
scoring, plus each player's opponent — so scoring doesn't depend on me
reimplementing the scoring rules by hand.

Its limit: **stats only appear after games finish**, and the dataset
refreshes on a cadence rather than live. Sunday scores generally land
overnight, so standings update Monday morning, not during the games.

**Projections — Sleeper.** This is the weak link, and worth being
straight about: the projections endpoint is **not in Sleeper's
published documentation**. It's one the developer community found and
several client libraries rely on. It could change or disappear without
warning, and I couldn't test it from where I built this — so treat the
first sync run as the real test.

That's why the sync script treats projections as optional. If the fetch
fails, it logs a warning, skips them, and everything else still syncs;
the pick list falls back to alphabetical order. **Projections only
control sort order** — they never touch scoring or standings. If they
break, the league still runs.

If you'd rather not depend on an undocumented endpoint, FantasyPros
launched an official API with a documented projections endpoint. Their
own pricing page states the free tier is for **non-production use**,
with personal production keys bundled into a FantasyPros MVP/HOF
membership at $5.99/mo. I haven't tested it, so I can't vouch for the
data shape — but it's documented and supported, which the Sleeper
endpoint isn't. Swapping it in means rewriting one function in
`sync.py` (`fetch_projections`).

---

## Two things I decided that you didn't ask for

Flagging these because they're game-design choices, not technical ones,
and both are easy to reverse:

1. **Rosters are hidden from other managers until the week locks.**
   Since everyone can pick anyone, visible rosters before lock would
   let people copy the best one at the last minute. After lock,
   everything is public. To make rosters always visible, replace the
   last policy in `migration_v3.sql` with
   `for select using (auth.uid() is not null)`.

2. **Lock time is the first Sunday kickoff, which isn't always 1pm ET.**
   Week 4 this season locks at **9:30am ET** because of an
   international game. The app shows the real lock time on the picks
   screen rather than assuming. Worth telling your league — it's an
   easy way to get burned.

---

## Known limitations

- **No live scoring.** Scores appear after nflverse refreshes, usually
  overnight. Live in-game updates would mean a paid data provider.
- **No live sync between browsers.** If someone else changes something
  while your app is open, you'll see it on refresh.
- **GitHub Actions schedules are best-effort** and can run late under
  load. The sync is scheduled well away from kickoff so a delay doesn't
  matter, but if a run is late, you can always trigger it manually from
  the Actions tab.
- **~18 stat rows per week belong to players not in the pool**
  (practice-squad call-ups and similar). The sync skips them and logs
  the count. They're players nobody would have been able to pick.
- **The player list is still a snapshot.** Re-running the nflverse pull
  refreshes teams after trades; the sync job doesn't currently update
  the `players` table itself.
