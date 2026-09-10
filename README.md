# Pick Anyone — setup guide

Three pieces: a Supabase project (database + login), one static HTML file (the
app itself — no build step), and Vercel (hosting, via your GitHub).

## 1. Create a new Supabase project

In your existing Supabase account: **New project**. Give it any name (e.g.
"pick-anyone"). This keeps it separate from your other project's data.

## 2. Run the schema

Open **SQL Editor > New query**, paste in the entire contents of `schema.sql`,
and run it. This creates the tables and the access rules (who can see/edit
what).

## 3. Import the player list

Go to **Table Editor > players > Insert > Import data from CSV**, upload
`players_for_import.csv`. It's 529 real players (QB/RB/WR/TE/K) pulled from
nflverse's open roster dataset — a one-time snapshot, not a live feed, so a
trade next week won't show up until this file is regenerated and re-imported.

## 4. Turn off email confirmation

**Authentication > Providers > Email** (sometimes under Settings), find
**Confirm email** and turn it **off**. Without this, Supabase's free tier
email sending is rate-limited enough (commonly reported around 2 emails/hour)
that 10 people signing up the same day would likely start hitting errors.
With it off, signup/login never sends an email at all.

## 5. Get your project's URL and key

**Project Settings > API**. Copy the **Project URL** and the **anon public**
key. Open `index.html`, and near the top of the `<script>` block replace:

```js
const SUPABASE_URL = "YOUR_SUPABASE_PROJECT_URL";
const SUPABASE_ANON_KEY = "YOUR_SUPABASE_ANON_KEY";
```

with your real values. (The anon key is meant to be public in frontend code —
Supabase's access rules, not secrecy of this key, are what protect the data.)

## 6. Upload to GitHub (no terminal needed)

Create the repo:
1. On github.com, click the **+** in the top-right corner → **New repository**
2. Name it (e.g. `pick-anyone`), Public or Private both work, check **"Add a
   README file"**
3. Click **Create repository**

Upload the file:
4. On the repo's page, click **Add file** (above the file list) → **Upload
   files**
5. Drag `index.html` into the upload box
6. Click **Commit changes**

## 7. Deploy on Vercel

**Add New... > Project**, import that repo. It's a single static HTML file
with no `package.json`, so Vercel deploys it as a static site with no build
command needed. To update it later (e.g. after refreshing the player list),
repeat step 6 above with a new upload — Vercel redeploys automatically on
every commit.

## 8. Make yourself commissioner

Sign up in the live app like any manager would (email, password, team name).
Then in Supabase, **Table Editor > profiles**, find your row, and manually set
`is_commissioner` to `true`. There's no in-app way to do this — it's
intentionally only editable from the database, so nobody can just grant
themselves commissioner from the app.

## Known limitations, on purpose for v1

- **Player list is a snapshot**, not live — re-run the same nflverse pull to
  refresh it later.
- **No live sync between devices.** If the commissioner enters points while
  you have the app open, you won't see it until you refresh. Everyone's own
  actions (picks, etc.) update immediately for them.
- **Rapid-fire points entry** in the Commissioner tab might occasionally drop
  focus to the next field if you're tabbing through very fast, since each
  entry does a quick round-trip to the database. Functionally it still saves
  correctly — just flagging it in case it's annoying in practice, since I
  couldn't test this live myself before handing it over.
- **No real lock-at-kickoff yet.** The commissioner's "current week" number is
  the only gate on picks; it's not tied to real game times.
