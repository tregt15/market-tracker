"""Writes site/export/: a download bundle for handing the data to an AI agent.

  PROMPT.md       the analysis prompt (docs/analysis-prompt.md with the coverage filled in)
  brief.md        the written summary (copied from site/)
  games.csv       one row per tracked game, every metric
  daily.csv       one row per game per UTC day: players online and visits gained
  passes.csv      one row per game pass for sale
  subgenres.csv   the sub-genre table from the brief
  patterns.csv    the title-pattern table from the brief
  market-research.zip   all of the above
"""

import csv
import datetime as dt
import os
import shutil
import zipfile
from collections import Counter

from tracker.collect import ROOT, parse, iso

GAME_COLS = [
    ("id", lambda g, d: g["id"]),
    ("name", lambda g, d: g["name"]),
    ("url", lambda g, d: f"https://www.roblox.com/games/{g['place']}"),
    ("genre", lambda g, d: g["genre"]),
    ("subgenre", lambda g, d: g["sub"]),
    ("min_age", lambda g, d: g["minAge"]),
    ("maturity", lambda g, d: g["mat"]),
    ("max_players_per_server", lambda g, d: g["maxPlayers"]),
    ("created", lambda g, d: g["created"]),
    ("first_seen", lambda g, d: g["first"]),
    ("age_when_found_days", lambda g, d: d["ageFound"]),
    ("days_tracked", lambda g, d: g["tracked"]),
    ("status", lambda g, d: g["status"]),
    ("found_by", lambda g, d: ";".join(g["src"])),
    ("charts_best_rank", lambda g, d: ";".join(f"{k}:{v}" for k, v in g["charts"])),
    ("players_last", lambda g, d: g["pNow"]),
    ("players_peak", lambda g, d: g["pPeak"]),
    ("players_avg", lambda g, d: g["pAvg"]),
    ("visits_total", lambda g, d: g["vNow"]),
    ("visits_per_day", lambda g, d: g["vpd"]),
    ("visits_per_day_is_lifetime_estimate", lambda g, d: int(g["vpdEst"])),
    ("visits_per_day_last_24h", lambda g, d: g["vpd24"]),
    ("trend_last24h_vs_first24h", lambda g, d: g["trend"]),
    ("like_pct", lambda g, d: g["likePct"]),
    ("votes", lambda g, d: g["votes"]),
    ("favorites_per_1k_visits", lambda g, d: g["fav1k"]),
    ("session_minutes_est", lambda g, d: g["session"]),
    ("updates_while_tracked", lambda g, d: g["updates"]),
    ("passes_for_sale", lambda g, d: g["passes"]),
    ("pass_price_min", lambda g, d: d["priceMin"]),
    ("pass_price_median", lambda g, d: g["passMed"]),
    ("pass_price_max", lambda g, d: d["priceMax"]),
    ("creator", lambda g, d: g["creator"]),
    ("creator_type", lambda g, d: d["creatorType"]),
    ("creator_verified", lambda g, d: None if g["verified"] is None else int(bool(g["verified"]))),
    ("creator_other_games", lambda g, d: g["creatorGames"]),
    ("creator_best_other_visits", lambda g, d: g["creatorBest"]),
    ("creator_best_other_name", lambda g, d: d.get("creatorBestName")),
    ("description", lambda g, d: " ".join((d.get("desc") or "").split())),
]


def _write_csv(path, header, rows):
    # utf-8-sig so Excel shows emoji and accents correctly
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])


def daily_rows(g, rows, created):
    """Per UTC day: snapshots, hours the visit gain spans, players avg/max, visits at day end and gained that day."""
    by_day = {}
    for r in sorted(rows, key=lambda r: r["t"]):
        by_day.setdefault(r["t"][:10], []).append(r)
    out, prev = [], None
    for day, rs in sorted(by_day.items()):
        t_last = parse(rs[-1]["t"])
        # visits_gained runs from the previous day's last snapshot (or this day's first) to this day's last
        start = prev if prev is not None else rs[0]
        hours = (t_last - parse(start["t"])).total_seconds() / 3600
        ps = [r["p"] for r in rs]
        out.append([g["id"], day, round((t_last - created).total_seconds() / 86400, 1), len(rs), round(hours, 1),
                    round(sum(ps) / len(ps), 1), max(ps), rs[-1]["v"], rs[-1]["v"] - start["v"]])
        prev = rs[-1]
    return out


def coverage(games, raw_games, state, t):
    runs = state.get("runs", [])
    scan = sum(any(s.startswith("scan") for s in g["src"]) for g in games)
    charts = sum("charts" in g["src"] for g in games)
    full = sum(g["tracked"] >= 6.8 for g in games)
    failed = sum(bool(r.get("failed")) for r in runs)
    firsts = sorted(g["first"] for g in games)
    ages = Counter("unknown" if g["minAge"] is None else "All ages" if g["minAge"] == 0 else f"{g['minAge']}+"
                   for g in games)
    lines = [
        f"- Generated: {iso(t)}",
        f"- Collection runs: {len(runs)} (first {runs[0]['t'] if runs else '?'}, every 3 hours; {failed} had a failed step)",
        f"- Games: {len(games)} ({full} tracked a full 7 days; the rest are still being tracked)",
        f"- Found by: charts {charts}, new-game scan {scan} (a game can be found by both)",
        f"- First seen between {firsts[0] if firsts else '?'} and {firsts[-1] if firsts else '?'}",
        "- Age ratings: " + ", ".join(f"{k} {v}" for k, v in ages.most_common()),
    ]
    if len(runs) < 56:
        lines.append(f"- Note: the tracker has run for about {len(runs) * 3 / 24:.1f} days so far. Fewer than 7 days of "
                     "collection means few games have a full week, so weigh growth and trend numbers lightly.")
    return "\n".join(lines)


def write_export(site, games, details, raw_games, series, state, sub_rows, pat_rows, t):
    out = os.path.join(site, "export")
    os.makedirs(out, exist_ok=True)

    game_rows, day_rows, pass_rows = [], [], []
    for g in sorted(games, key=lambda g: -(g["vpd24"] if g["vpd24"] is not None else g["vpd"])):
        raw = raw_games.get(str(g["id"]), {})
        d = dict(details[g["id"]])
        prices = sorted(p["price"] for p in d.get("passes") or [] if p.get("price"))
        created = parse(raw.get("created") or g["created"] + "T00:00:00")
        d.update(priceMin=prices[0] if prices else None, priceMax=prices[-1] if prices else None,
                 creatorType=raw.get("creator_type"),
                 ageFound=round((parse(g["first"]) - created).total_seconds() / 86400, 1))
        game_rows.append([f(g, d) for _, f in GAME_COLS])
        day_rows += daily_rows(g, series.get(g["id"], []), created)
        for p in d.get("passes") or []:
            pass_rows.append([g["id"], g["name"], p.get("name"), p.get("price")])

    _write_csv(os.path.join(out, "games.csv"), [c for c, _ in GAME_COLS], game_rows)
    _write_csv(os.path.join(out, "daily.csv"),
               ["id", "date_utc", "game_age_days", "snapshots", "hours_covered", "players_avg", "players_max",
                "visits_total_end", "visits_gained"], day_rows)
    _write_csv(os.path.join(out, "passes.csv"), ["id", "game_name", "pass_name", "price_robux"], pass_rows)
    sub_cols = ["subgenre", "games", "scanFound", "hitRate", "hits", "bigHits", "visitsPerDay", "perGame",
                "medianVisitsPerDay", "medianSession", "medianLike", "allAgesShare", "top"]
    _write_csv(os.path.join(out, "subgenres.csv"), sub_cols, [[r[c] for c in sub_cols] for r in sub_rows])
    _write_csv(os.path.join(out, "patterns.csv"), ["pattern", "games", "hits", "visitsPerDay", "examples"],
               [[r["pattern"], r["games"], r["hits"], r["visitsPerDay"], " | ".join(r["examples"])] for r in pat_rows])
    shutil.copy(os.path.join(site, "brief.md"), out)

    with open(os.path.join(ROOT, "docs", "analysis-prompt.md"), encoding="utf-8") as f:
        prompt = f.read().replace("{{COVERAGE}}", coverage(games, raw_games, state, t))
    with open(os.path.join(out, "PROMPT.md"), "w", encoding="utf-8") as f:
        f.write(prompt)

    names = ["PROMPT.md", "brief.md", "games.csv", "daily.csv", "passes.csv", "subgenres.csv", "patterns.csv"]
    with zipfile.ZipFile(os.path.join(out, "market-research.zip"), "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.write(os.path.join(out, n), n)
    files = {n: os.path.getsize(os.path.join(out, n)) for n in names + ["market-research.zip"]}
    print(f"export: {len(game_rows)} games, {len(day_rows)} daily rows, {len(pass_rows)} passes, "
          f"zip {files['market-research.zip'] / 1e6:.1f} MB")
    return files
