# Market tracker

Collects public listing stats on a schedule and shows how new entries grow over their
first week, broken down by category.

## Details

The tracker looks at Roblox games created in the last 30 days that are getting players,
and snapshots each one every 3 hours for 7 days.

## How games are found

1. **Charts.** Every chart on the Roblox Charts page (Top Trending, Up-and-Coming, Trending in
   each genre and others), read as all devices / all countries, US, and phone. Any game on a
   chart whose universe was created in the last 30 days is tracked.
2. **New-game scan.** Universe ids are handed out in order (about 170,000 a day, most of them
   empty private baseplates). Every id is checked twice, at 2 days old and at 10 days old. A
   public game with a real name is tracked if it has 1,000+ visits (or 3+ players online) at
   day 2, or 5,000+ visits (or 5+ online) at day 10.

## What each snapshot records

Players online, total visits, favorites, likes and dislikes. Genre, sub-genre, age rating,
creator, server size, chart ranks and icon are stored once per game.

## Files

| Path | What it is |
| --- | --- |
| `tracker/collect.py` | One run: charts, scan, snapshot. Settings are at the top. |
| `tracker/build_dashboard.py` | Works out the metrics and writes `docs/data.json`. |
| `tracker/roblox.py` | Public Roblox web API calls, paced to stay under rate limits. |
| `docs/index.html` | The dashboard (GitHub Pages serves the `docs` folder). |
| `data/games.json` | One entry per tracked game. |
| `data/state.json` | Scan positions and a log of recent runs. |
| `data/snapshots/*.jsonl` | One line per game per run, one file per day. |
| `.github/workflows/track.yml` | Runs everything every 3 hours and commits the data. |

## Run it by hand

```
python tracker/collect.py            # charts + scan + snapshot
python tracker/collect.py snapshot   # snapshot only
python tracker/build_dashboard.py
cd docs && python -m http.server     # then open http://localhost:8000
```

Only the Python standard library is used.
