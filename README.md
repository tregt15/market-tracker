# Market tracker

Collects public listing stats on a schedule and shows how new entries grow over their
first week, broken down by category.

## Details

The tracker looks at Roblox games created in the last 30 days that are getting players, and
follows each one for 30 days: hourly in its first week if it has players (every 3 hours if
not), then daily. The Action runs every hour; charts and the new-game scan run every 3 hours.

## How games are found

1. **Charts.** Every chart on the Roblox Charts page (Top Trending, Up-and-Coming, Trending in
   each genre and others), read across devices and 18 countries. Any game on a chart whose
   universe was created in the last 30 days is tracked, and every tracked game's chart
   positions are logged at every read.
2. **New-game scan.** Universe ids are handed out in order (about 170,000 a day, most of them
   empty private baseplates). Every id is checked at 2, 10 and 20 days old. A public game with
   a real name and 2+ votes is tracked if it clears that age's bar (300 / 1,000 / 2,000 visits,
   or 2 / 3 / 3 players online). Each scan logs how many ids it checked and how many passed.
3. **Random sample.** About 50 a day of 2-day-old public games that did not clear the bar, for
   the base rate of an ordinary launch.

## What is recorded

- Each snapshot: players online, total visits, favorites, likes and dislikes.
- Per game: genre, sub-genre, age rating, creator, server size, icon, thumbnail, description,
  every published update, and every change of title, description, icon or thumbnail.
- Weekly: game passes and developer products (names and prices). Once: the creator's other
  public games.
- Every 6 hours in week 1, then daily: badge award counts (games with 5+ players). Daily,
  biggest games first: public server fill (games with 10+ players online) and creator group
  member counts (both endpoints allow only a few calls a minute).
- These extras run inside a time budget each run, oldest first, so a backlog catches up over
  later runs.

## Outputs

- `index.html`: the dashboard, with a sub-genre table (demand, crowding, hit rate) and a
  sortable table of every game.
- `brief.md`: a written summary for reading or for an AI to analyse: demand by age rating,
  sub-genres, trending title patterns, hits from small creators, fastest risers and the top
  40 games with descriptions, monetization and creator history.
- `export/market-research.zip`: a download for handing the data to an AI agent: `PROMPT.md`
  (goals, constraints, data dictionary, method and deliverables; edit `docs/analysis-prompt.md`),
  the brief, and CSVs of games, daily and hourly numbers, charts, badges, servers, groups,
  changes, passes, products and the scan log. The
  brief and the download cover every game found in the last 60 days, including archived ones.
- Average session length is estimated as players online / visits per hour (Little's law).

## Files

| Path | What it is |
| --- | --- |
| `tracker/collect.py` | One run: charts, scan, snapshot. Settings are at the top. |
| `tracker/build_dashboard.py` | Works out the metrics and builds `site/` (dashboard data, brief, download). |
| `tracker/brief.py` | Writes `brief.md` and `brief.json`. |
| `tracker/export.py` | Writes the CSVs, `PROMPT.md` and the zip in `site/export/`. |
| `docs/analysis-prompt.md` | The analysis prompt template (`{{COVERAGE}}` is filled in at build time). |
| `tracker/roblox.py` | Public Roblox web API calls, paced to stay under rate limits. |
| `docs/index.html` | The dashboard page (copied into `site/`, which GitHub Pages serves). |
| `data/games.json` | One entry per tracked game. |
| `data/state.json` | Scan positions and a log of recent runs. |
| `data/snapshots/*.jsonl` | One line per game per snapshot, one file per day. |
| `data/charts`, `badges`, `servers`, `groups` | The same, for chart positions, badge counts, server fill and group members. |
| `data/scan_log.jsonl` | Per scan: ids checked, public games, games that cleared the bar, sampled. |
| `data/archive/` | Games that finished their 30 days, by month. |
| `.github/workflows/track.yml` | Runs everything every 3 hours and commits the data. |

## Run it by hand

```
python tracker/collect.py            # decides full (charts + scan) or light by itself
python tracker/collect.py snapshot   # one step only (charts, scan, snapshot, extras)
TRACKER_DATA=/tmp/copy python tracker/collect.py   # run against a copy of data/
python tracker/build_dashboard.py
cd site && python -m http.server     # then open http://localhost:8000
```

Only the Python standard library is used.
