"""Build site/: the dashboard (docs/index.html + data.json + series/), brief.md and export/.

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
from tracker.collect import DATA, ROOT, TRACK_DAYS, DENSE_DAYS, load, parse, now_utc  # noqa: E402
from tracker.brief import write_brief  # noqa: E402
from tracker.export import write_export  # noqa: E402
from tracker.tags import tags as game_tags  # noqa: E402

KEEP_DAYS = 14    # games found in the last 2 weeks stay on the dashboard
EXPORT_DAYS = 60  # games found in the last 2 months go into the brief and the download
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


def mid_price(items):
    ps = sorted(p["price"] for p in items or [] if p.get("price"))
    return ps[len(ps) // 2] if ps else None


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
    voted = [r for r in rows if "u" in r]
    up, down = (voted[-1]["u"] or 0, voted[-1]["d"] or 0) if voted else (0, 0)
    # Average session length from Little's law: players online = arrivals per hour x hours per visit.
    # Uses the dense first week only (hourly samples); later snapshots are daily and miss the daily cycle.
    session = None
    dense = [r for h, r in pts if h <= DENSE_DAYS * 24]
    dense_h = (parse(dense[-1]["t"]) - t0).total_seconds() / 3600
    gained = dense[-1]["v"] - rows[0]["v"]
    if dense_h >= 12 and gained >= 50:
        session = avg([r["p"] for r in dense]) / (gained / dense_h) * 60
        session = round(min(session, 600), 1)
    passes = g.get("passes")
    cg = g.get("creator_games")
    products = g.get("products")
    hist = g.get("history") or []
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
        "session": session,
        "updates": len(g.get("updates") or []),
        "passes": None if passes is None else len(passes),
        "passMed": mid_price(passes),
        "products": None if products is None else len(products),
        "productMed": mid_price(products),
        "groupMembers": g.get("group_members"),
        "badges": None if "badges" not in g else len(g["badges"]),
        "tags": game_tags(g.get("name"), g.get("description")),
        "minAgeFound": g.get("min_age_found", g.get("min_age")),
        "ageChanges": [[h[0], h[2]] for h in hist if h[1] == "min_age"],
        "creatorCreated": (g.get("creator_created") or "")[:10] or None,
        "media": g.get("media"),
        "sponsored": g.get("sponsored_seen"),
        "nameChanges": sum(h[1] == "name" for h in hist),
        "iconChanges": sum(h[1] == "icon" for h in hist),
        "creatorGames": None if cg is None else cg["count"],
        "creatorBest": None if cg is None else cg["best_visits"],
        "verified": g.get("creator_verified"),
        "sp": sparkline([r["p"] for r in rows]),
        # loaded on demand from series/N.json, not part of data.json
        "_detail": {
            "s": [[round(h, 1), r["p"], r["v"]] for h, r in pts],
            "desc": g.get("description") or "",
            "passes": passes,
            "products": products,
            "history": hist,
            "creatorBestName": None if cg is None else cg.get("best_name"),
        },
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
    for path in glob.glob(os.path.join(DATA, "archive", "games-*.json")):  # finished games
        with open(path, encoding="utf-8") as f:
            for k, g in json.load(f).items():
                games.setdefault(k, g)
    state = load("state.json", {"runs": []})
    since = t - dt.timedelta(days=EXPORT_DAYS + TRACK_DAYS)
    series = series_by_game(since)
    every = []
    for g in games.values():
        rows = series.get(g["id"])
        if not rows or t - parse(g["first_seen"]) > dt.timedelta(days=EXPORT_DAYS):
            continue
        every.append(metrics(g, rows, t))
    details = {g["id"]: g.pop("_detail") for g in every}
    cutoff = (t - dt.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = [g for g in every if g["first"] >= cutoff]
    runs = state.get("runs", [])
    site = os.path.join(ROOT, "site")
    shutil.rmtree(site, ignore_errors=True)
    os.makedirs(os.path.join(site, "series"))
    shutil.copy(os.path.join(ROOT, "docs", "index.html"), site)
    # the brief and the download cover every game of the last EXPORT_DAYS
    sub, pats = write_brief(every, details, games, site, t, DATA)
    files = write_export(site, every, details, games, series, state, sub, pats, t, since)
    # full time series go to SHARDS small files, loaded only when a row is opened
    shards = {}
    for g in out:
        shards.setdefault(g["id"] % SHARDS, {})[g["id"]] = details[g["id"]]
    for n, items in shards.items():
        with open(os.path.join(site, "series", f"{n}.json"), "w", encoding="utf-8") as f:
            json.dump(items, f, separators=(",", ":"))
    doc = {
        "generated": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "firstRun": runs[0]["t"] if runs else None,
        "runs": len(runs),
        "trackDays": DENSE_DAYS,
        "trackDaysTotal": TRACK_DAYS,
        "shards": SHARDS,
        "exportFiles": files,
        "exportGames": len(every),
        "games": out,
    }
    with open(os.path.join(site, "data.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(os.path.join(site, "data.json")) / 1e6
    print(f"dashboard data: {len(out)} games, data.json {size:.1f} MB")


if __name__ == "__main__":
    main()
