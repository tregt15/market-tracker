"""Turn data/ into docs/data.json for the dashboard (docs/index.html).

Per game it works out players online now / peak / average, visits per day over the
tracked window and over the last 24 hours, the players-online trend (last 24 h vs the
first 24 h tracked), like percent and favorites per 1,000 visits.
"""

import datetime as dt
import glob
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tracker.collect import DATA, ROOT, TRACK_DAYS, load, parse, now_utc  # noqa: E402

KEEP_DAYS = 14  # games found in the last 2 weeks stay on the dashboard
SHARDS = 64     # number of time-series files the dashboard loads from on demand


def series_by_game(since):
    out = {}
    for path in sorted(glob.glob(os.path.join(DATA, "snapshots", "*.jsonl"))):
        day = os.path.basename(path)[:10]
        if day < since.strftime("%Y-%m-%d"):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                out.setdefault(r["id"], []).append(r)
    return out


def avg(xs):
    return sum(xs) / len(xs) if xs else None


def metrics(g, rows, t):
    rows.sort(key=lambda r: r["t"])
    t0 = parse(rows[0]["t"])
    pts = [((parse(r["t"]) - t0).total_seconds() / 3600, r) for r in rows]
    span_h = pts[-1][0]
    last = rows[-1]
    created = parse(g["created"])
    age_days = max((t - created).total_seconds() / 86400, 0.1)

    def window(lo, hi):
        return [r for h, r in pts if lo <= h <= hi]

    if span_h >= 12:
        vpd = (last["v"] - rows[0]["v"]) / (span_h / 24)
        vpd_est = False
    else:  # not enough history yet: lifetime average
        vpd = last["v"] / age_days
        vpd_est = True
    vpd_24 = None
    if span_h >= 24:
        recent = window(span_h - 24, span_h)
        vpd_24 = (recent[-1]["v"] - recent[0]["v"]) * 24 / max(0.1, (parse(recent[-1]["t"]) - parse(recent[0]["t"])).total_seconds() / 3600)
    trend = None
    if span_h >= 48:
        first = avg([r["p"] for r in window(0, 24)])
        recent = avg([r["p"] for r in window(span_h - 24, span_h)])
        if first and first >= 1:
            trend = recent / first
    up, down = last.get("u") or 0, last.get("d") or 0
    return {
        "id": g["id"], "place": g["place"], "name": g["name"], "creator": g["creator"],
        "genre": g.get("genre") if g.get("genre") not in (None, "", "All") else "No genre set", "sub": g.get("subgenre") or "",
        "minAge": g.get("min_age"), "mat": g.get("maturity"),
        "created": g["created"][:10], "ageDays": round(age_days, 1),
        "first": g["first_seen"], "tracked": round(span_h / 24, 2), "status": g["status"],
        "src": g["sources"], "charts": sorted(([k, v["best"]] for k, v in g.get("charts", {}).items()), key=lambda x: x[1]),
        "icon": g.get("icon"), "maxPlayers": g.get("max_players"),
        "pNow": last["p"], "pPeak": max(r["p"] for r in rows), "pAvg": round(avg([r["p"] for r in rows]), 1),
        "vNow": last["v"], "vpd": round(vpd), "vpdEst": vpd_est,
        "vpd24": None if vpd_24 is None else round(vpd_24), "trend": None if trend is None else round(trend, 2),
        "likePct": round(100 * up / (up + down), 1) if up + down >= 10 else None,
        "votes": up + down,
        "fav1k": round(1000 * last["f"] / last["v"], 1) if last["v"] >= 100 else None,
        "sp": sparkline([r["p"] for r in rows]),
        "_series": [[round(h, 1), r["p"], r["v"]] for h, r in pts],
    }


def sparkline(values, n=16):
    """Players-online values squeezed to at most n points (bucket averages)."""
    if len(values) <= n:
        return values
    out = []
    for i in range(n):
        b = values[i * len(values) // n:(i + 1) * len(values) // n]
        out.append(round(sum(b) / len(b)))
    return out


def main():
    t = now_utc()
    games = load("games.json", {})
    state = load("state.json", {"runs": []})
    since = t - dt.timedelta(days=KEEP_DAYS + TRACK_DAYS)
    series = series_by_game(since)
    out = []
    for g in games.values():
        rows = series.get(g["id"])
        if not rows or t - parse(g["first_seen"]) > dt.timedelta(days=KEEP_DAYS):
            continue
        out.append(metrics(g, rows, t))
    runs = state.get("runs", [])
    site = os.path.join(ROOT, "site")
    shutil.rmtree(site, ignore_errors=True)
    os.makedirs(os.path.join(site, "series"))
    shutil.copy(os.path.join(ROOT, "docs", "index.html"), site)
    # full time series go to SHARDS small files, loaded only when a row is opened
    shards = {}
    for g in out:
        shards.setdefault(g["id"] % SHARDS, {})[g["id"]] = g.pop("_series")
    for n, items in shards.items():
        with open(os.path.join(site, "series", f"{n}.json"), "w", encoding="utf-8") as f:
            json.dump(items, f, separators=(",", ":"))
    doc = {
        "generated": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "firstRun": runs[0]["t"] if runs else None,
        "runs": len(runs),
        "trackDays": TRACK_DAYS,
        "shards": SHARDS,
        "games": out,
    }
    with open(os.path.join(site, "data.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(os.path.join(site, "data.json")) / 1e6
    print(f"dashboard data: {len(out)} games, data.json {size:.1f} MB")


if __name__ == "__main__":
    main()
