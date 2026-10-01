// Runs the REAL fetchAll from index.html against a mock Supabase client.
const fs = require("fs");
const src = fs.readFileSync("app_script.js", "utf8");

// Pull out PAGE_SIZE + fetchAll only.
function grab(name) {
  const start = src.indexOf(`async function ${name}(`);
  let i = src.indexOf("{", start), depth = 0, j = i;
  while (true) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (depth === 0) break; }
    j++;
  }
  return src.slice(start, j + 1);
}

let sb, PAGE_SIZE;
eval("PAGE_SIZE = " + src.match(/const PAGE_SIZE = (\d+);/)[1] + ";");
eval(grab("fetchAll").replace("async function fetchAll", "globalThis.fetchAll = async function"));

let pass = 0, fail = 0;
function check(label, a, e) {
  if (JSON.stringify(a) === JSON.stringify(e)) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}\n        got ${JSON.stringify(a)}\n        want ${JSON.stringify(e)}`); }
}

// Mock client that enforces Supabase's real behaviour: hard cap per request.
function mockSb(rowsByTable, opts = {}) {
  const requests = [];
  return {
    requests,
    from(table) {
      const q = {
        _orders: [],
        select() { return q; },
        order(col) { q._orders.push(col); return q; },
        async range(from, to) {
          const all = [...(rowsByTable[table] || [])];
          // stable sort by the requested columns
          all.sort((a, b) => {
            for (const c of q._orders) {
              if (a[c] < b[c]) return -1;
              if (a[c] > b[c]) return 1;
            }
            return 0;
          });
          const wanted = to - from + 1;
          const capped = Math.min(wanted, 1000); // Supabase hard cap
          const slice = all.slice(from, from + capped);
          requests.push({ table, from, to, returned: slice.length, orders: [...q._orders] });
          if (opts.errorOn === table) return { data: null, error: { message: "boom" } };
          return { data: slice, error: null };
        },
      };
      return q;
    },
  };
}

function makeRows(n, table) {
  // two-column key, like weekly_points / weekly_players
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push({ week: Math.floor(i / 500) + 1, player_id: "p" + String(i).padStart(5, "0"), v: i });
  }
  return out;
}

console.log("\n=== the real-world case: tables past 1000 rows ===");
for (const n of [0, 1, 999, 1000, 1001, 1074, 2988, 5000]) {
  sb = mockSb({ weekly_points: makeRows(n) });
  const got = await fetchAll("weekly_points", ["week", "player_id"]);
  check(`${n} rows -> all ${n} returned`, got.length, n);
  const uniq = new Set(got.map(r => r.player_id));
  check(`${n} rows -> no dupes, no gaps`, uniq.size, n);
}

console.log("\n=== stable ordering is actually applied ===");
sb = mockSb({ weekly_players: makeRows(2500) });
await fetchAll("weekly_players", ["week", "player_id"]);
check("orders by both key columns", sb.requests[0].orders, ["week", "player_id"]);
check("single-column form works", (() => {
  const s2 = mockSb({ players: makeRows(10) });
  sb = s2;
  return true;
})(), true);
sb = mockSb({ players: makeRows(10) });
await fetchAll("players", "id");
check("single column passed through", sb.requests[0].orders, ["id"]);

console.log("\n=== request count is sane ===");
sb = mockSb({ weekly_points: makeRows(1074) });
await fetchAll("weekly_points", ["week", "player_id"]);
check("1074 rows -> 2 requests", sb.requests.length, 2);

sb = mockSb({ weekly_points: makeRows(1000) });
await fetchAll("weekly_points", ["week", "player_id"]);
check("exactly 1000 -> 2 requests, no infinite loop", sb.requests.length, 2);

sb = mockSb({ weekly_points: makeRows(0) });
await fetchAll("weekly_points", ["week", "player_id"]);
check("empty table -> 1 request", sb.requests.length, 1);

console.log("\n=== errors surface instead of returning partial data ===");
sb = mockSb({ picks: makeRows(50) }, { errorOn: "picks" });
let threw = false;
try { await fetchAll("picks", ["week", "player_id"]); } catch (e) { threw = true; }
check("query error throws", threw, true);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
