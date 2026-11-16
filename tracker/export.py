"""Writes site/export/: a download bundle for handing the data to an AI agent.

  PROMPT.md       the analysis prompt (docs/analysis-prompt.md with the coverage filled in)
  brief.md        the written summary (copied from site/)
  games.csv       one row per tracked game, every metric
  daily.csv       one row per game per UTC day: players online and visits gained
  hourly_hits.csv every snapshot of games that reached HIT players: time-of-day and launch curves
  charts.csv      every chart position of every tracked game at every chart read
  badges.csv      badge award counts over time (a retention and funnel measure)
  servers.csv     public server fill over time (solo vs social play)
  groups.csv      creator group member counts over time
  history.csv     title, description, icon and thumbnail changes
  passes.csv      one row per game pass for sale
  products.csv    one row per developer product for sale
  monetization.csv  pass and product counts at each weekly check
  scan_log.csv    how many new games were checked and how many passed (the denominator)
  subgenres.csv   the sub-genre table from the brief
  patterns.csv    the title-pattern table from the brief
  market-research.zip   all of the above
"""

import bisect
import csv
import datetime as dt
import glob
import json
import os
import shutil
import zipfile
from collections import Counter

from tracker.collect import DATA, ROOT, DENSE_DAYS, parse, iso
from tracker.brief import HIT

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
    ("first_chart_at", lambda g, d: d["firstChart"]),
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
    ("products_for_sale", lambda g, d: g["products"]),
    ("product_price_median", lambda g, d: g["productMed"]),
    ("badges", lambda g, d: g["badges"]),
    ("name_changes", lambda g, d: g["nameChanges"]),
    ("icon_changes", lambda g, d: g["iconChanges"]),
    ("group_members", lambda g, d: g["groupMembers"]),
    ("creator", lambda g, d: g["creator"]),
    ("creator_type", lambda g, d: d["creatorType"]),
    ("creator_id", lambda g, d: d["creatorId"]),
    ("creator_verified", lambda g, d: None if g["verified"] is None else int(bool(g["verified"]))),
    ("creator_other_games", lambda g, d: g["creatorGames"]),
    ("creator_best_other_visits", lambda g, d: g["creatorBest"]),
    ("creator_best_other_name", lambda g, d: d.get("creatorBestName")),
    ("icon_url", lambda g, d: g["icon"]),
    ("thumbnail_url", lambda g, d: d["thumb"]),
    ("description", lambda g, d: " ".join((d.get("desc") or "").split())),
]


def read_logs(folder, since):
    """Rows from data/<folder>/YYYY-MM-DD.jsonl files on or after `since`."""
    out = []
    for path in sorted(glob.glob(os.path.join(DATA, folder, "*.jsonl"))):
        if os.path.basename(path)[:10] >= since.strftime("%Y-%m-%d"):
            with open(path, encoding="utf-8") as f:
                out += [json.loads(line) for line in f if line.strip()]
    return out


def visits_24h_before(rows, times, when):
    """Visits gained in the 24 hours before `when`, from the snapshot nearest each end (within 3 hours)."""
    def near(x):
        i = bisect.bisect_left(times, x)
        best = min((j for j in (i - 1, i) if 0 <= j < len(times)), key=lambda j: abs(times[j] - x), default=None)
        return rows[best] if best is not None and abs(times[best] - x) <= 3 * 3600 else None
    a, b = near(when - 86400), near(when)
    return None if a is None or b is None else b["v"] - a["v"]


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


def coverage(games, raw_games, state, t, scan):
    runs = state.get("runs", [])
    n_scan = sum(any(s.startswith("scan") for s in g["src"]) for g in games)
    charts = sum("charts" in g["src"] for g in games)
    sample = [g for g in games if "sample" in g["src"]]
    week = sum(g["tracked"] >= DENSE_DAYS - 0.2 for g in games)
    month = sum(g["tracked"] >= 29.5 for g in games)
    failed = sum(bool(r.get("failed")) for r in runs)
    firsts = sorted(g["first"] for g in games)
    days = (t - parse(runs[0]["t"])).total_seconds() / 86400 if runs else 0
    ages = Counter("unknown" if g["minAge"] is None else "All ages" if g["minAge"] == 0 else f"{g['minAge']}+"
                   for g in games)
    lines = [
        f"- Generated: {iso(t)}. Collection has run for {days:.1f} days ({len(runs)} runs; {failed} had a failed step).",
        f"- Games: {len(games)}. {week} have been tracked for 7+ days and {month} for the full 30.",
        f"- Found by: charts {charts}, new-game scan {n_scan}, random below-the-bar sample {len(sample)} "
        "(a game can be found by more than one way)",
        f"- First seen between {firsts[0] if firsts else '?'} and {firsts[-1] if firsts else '?'}",
        "- Age ratings: " + ", ".join(f"{k} {v}" for k, v in ages.most_common()),
    ]
    two = [r for r in scan if r["pass"] == "scan-2d"]
    if two:
        pub = sum(r["public_named"] for r in two)
        days_scanned = sum(r["ids"] for r in two) / 170_000
        lines.append(f"- New-game scan at 2 days old: {sum(r['ids'] for r in two):,} universe ids checked (about "
                     f"{days_scanned:.1f} days of new universes), {pub:,} public with a real name, "
                     f"{sum(r['added'] for r in two):,} cleared the bar ({100 * sum(r['added'] for r in two) / max(pub, 1):.1f}%).")
    if sample:
        lines.append(f"- Random sample below the bar: {len(sample)} games; {sum(g['pPeak'] >= 5 for g in sample)} later reached "
                     f"5+ players online and {sum(g['pPeak'] >= HIT for g in sample)} reached {HIT}+.")
    if days < 7:
        lines.append(f"- Note: only {days:.1f} days of collection so far, so few games have a full week. "
                     "Weigh growth and trend numbers lightly, and say so in your answer.")
    return "\n".join(lines)


def write_export(site, games, details, raw_games, series, state, sub_rows, pat_rows, t, since):
    out = os.path.join(site, "export")
    os.makedirs(out, exist_ok=True)
    ids = {g["id"] for g in games}
    files = {}

    def write(name, header, rows):
        _write_csv(os.path.join(out, name), header, rows)
        files[name] = len(rows)

    game_rows, day_rows, pass_rows, prod_rows, hist_rows, mon_rows, hourly = [], [], [], [], [], [], []
    for g in sorted(games, key=lambda g: -(g["vpd24"] if g["vpd24"] is not None else g["vpd"])):
        raw = raw_games.get(str(g["id"]), {})
        d = dict(details[g["id"]])
        prices = sorted(p["price"] for p in d.get("passes") or [] if p.get("price"))
        created = parse(raw.get("created") or g["created"] + "T00:00:00")
        firsts = [c["first"] for c in (raw.get("charts") or {}).values()]
        d.update(priceMin=prices[0] if prices else None, priceMax=prices[-1] if prices else None,
                 creatorType=raw.get("creator_type"), creatorId=raw.get("creator_id"), thumb=raw.get("thumb"),
                 firstChart=min(firsts) if firsts else None,
                 ageFound=round((parse(g["first"]) - created).total_seconds() / 86400, 1))
        game_rows.append([f(g, d) for _, f in GAME_COLS])
        rows = series.get(g["id"], [])
        day_rows += daily_rows(g, rows, created)
        if g["pPeak"] >= HIT:
            hourly += [[g["id"], r["t"], r["p"], r["v"], r.get("f"), r.get("u"), r.get("d")] for r in rows]
        for p in d.get("passes") or []:
            pass_rows.append([g["id"], g["name"], p.get("name"), p.get("price")])
        for p in raw.get("products") or []:
            prod_rows.append([g["id"], g["name"], p.get("name"), p.get("price")])
        for h in raw.get("history") or []:
            hist_rows.append([g["id"], h[0], h[1], " ".join(str(h[2]).split())])
        for m in raw.get("mon_log") or []:
            mon_rows.append([g["id"], m["t"], m["passes"], m["passMed"], m["products"], m["productMed"]])

    write("games.csv", [c for c, _ in GAME_COLS], game_rows)
    write("daily.csv", ["id", "date_utc", "game_age_days", "snapshots", "hours_covered", "players_avg", "players_max",
                        "visits_total_end", "visits_gained"], day_rows)
    write("hourly_hits.csv", ["id", "time_utc", "players", "visits_total", "favorites", "likes", "dislikes"], hourly)

    chart_rows = []
    for r in read_logs("charts", since):
        if r["id"] in ids:
            for sort_id, (best, views) in sorted(r["r"].items(), key=lambda x: x[1][0]):
                chart_rows.append([r["id"], r["t"], sort_id, best, views, ";".join(r.get("c") or [])])
    write("charts.csv", ["id", "time_utc", "chart", "best_rank", "views_listing_it", "countries"], chart_rows)

    badge_rows = []
    times = {}
    for r in read_logs("badges", since):
        if r["id"] not in ids:
            continue
        rows = series.get(r["id"], [])
        if r["id"] not in times:
            times[r["id"]] = [parse(x["t"]).timestamp() for x in rows]
        gained = visits_24h_before(rows, times[r["id"]], parse(r["t"]).timestamp())
        meta = raw_games.get(str(r["id"]), {}).get("badges") or {}
        for bid, awarded, past_day in r["b"]:
            m = meta.get(str(bid)) or ["", "", ""]
            badge_rows.append([r["id"], r["t"], bid, m[0], " ".join(m[1].split()), m[2], awarded, past_day, r.get("v"), gained])
    write("badges.csv", ["id", "time_utc", "badge_id", "badge_name", "badge_description", "badge_created", "awarded_total",
                         "awarded_past_day", "game_visits_total", "game_visits_past_24h"], badge_rows)

    write("servers.csv", ["id", "time_utc", "servers_listed", "more_servers_than_listed", "max_per_server", "players_listed",
                          "fill", "solo_share", "full_share", "ping_ms", "fps"],
          [[r["id"], r["t"], r["n"], int(r["more"]), r["max"], r["players"], r["fill"], r["solo"], r["full"], r["ping"], r["fps"]]
           for r in read_logs("servers", since) if r["id"] in ids])
    groups = {raw_games.get(str(i), {}).get("creator_id") for i in ids}
    write("groups.csv", ["group_id", "time_utc", "members"],
          [[r["group"], r["t"], r["m"]] for r in read_logs("groups", since) if r["group"] in groups])
    write("history.csv", ["id", "time_utc", "field", "new_value"], hist_rows)
    write("passes.csv", ["id", "game_name", "pass_name", "price_robux"], pass_rows)
    write("products.csv", ["id", "game_name", "product_name", "price_robux"], prod_rows)
    write("monetization.csv", ["id", "time_utc", "passes", "pass_price_median", "products", "product_price_median"], mon_rows)
    scan = []
    path = os.path.join(DATA, "scan_log.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            scan = [json.loads(x) for x in f if x.strip()]
    scan_cols = ["t", "pass", "ids", "public_named", "not_yet_tracked", "voted", "added", "sampled"]
    write("scan_log.csv", scan_cols, [[r.get(c) for c in scan_cols] for r in scan if r["t"] >= iso(since)])
    sub_cols = ["subgenre", "games", "scanFound", "hitRate", "hits", "bigHits", "visitsPerDay", "perGame",
                "medianVisitsPerDay", "medianSession", "medianLike", "allAgesShare", "top"]
    write("subgenres.csv", sub_cols, [[r[c] for c in sub_cols] for r in sub_rows])
    write("patterns.csv", ["pattern", "games", "hits", "visitsPerDay", "examples"],
          [[r["pattern"], r["games"], r["hits"], r["visitsPerDay"], " | ".join(r["examples"])] for r in pat_rows])
    shutil.copy(os.path.join(site, "brief.md"), out)

    with open(os.path.join(ROOT, "docs", "analysis-prompt.md"), encoding="utf-8") as f:
        prompt = f.read().replace("{{COVERAGE}}", coverage(games, raw_games, state, t, scan))
    with open(os.path.join(out, "PROMPT.md"), "w", encoding="utf-8") as f:
        f.write(prompt)

    order = ["PROMPT.md", "brief.md"] + list(files)
    with zipfile.ZipFile(os.path.join(out, "market-research.zip"), "w", zipfile.ZIP_DEFLATED) as z:
        for n in order:
            z.write(os.path.join(out, n), n)
    sizes = {n: os.path.getsize(os.path.join(out, n)) for n in order + ["market-research.zip"]}
    print("export: " + ", ".join(f"{n} {c:,}" for n, c in files.items())
          + f"; zip {sizes['market-research.zip'] / 1e6:.1f} MB")
    return sizes
