"""Starter analysis for the market-research bundle. Run it from the unzipped folder:

    pip install pandas
    python starter.py > starter-output.md

It prints the tables most decisions start from, as Markdown. Check them, then go deeper.
"""

import pandas as pd

pd.set_option("display.width", 200)
pd.set_option("display.max_colwidth", 40)

games = pd.read_csv("games.csv", encoding="utf-8-sig")
daily = pd.read_csv("daily.csv", encoding="utf-8-sig")
HIT = 100


def show(title, df):
    print(f"\n## {title}\n")
    print(df.to_markdown(index=False) if len(df) else "(no rows yet)")


games["demand"] = games["visits_per_day_last_24h"].fillna(games["visits_per_day"])
games["hit"] = games["players_peak"] >= HIT
games["scan"] = games["found_by"].fillna("").str.contains("scan")
games["scan_hit"] = games["hit"] & games["scan"]
games["low_age"] = games["min_age"].fillna(0) <= 9
low = games[games["low_age"]]
print(f"# Starter tables\n\n{len(games)} games, {len(low)} rated All ages or 9+. Hit = {HIT}+ players online at peak.")

# 1. tags, all ages and 9+
rows = low.assign(tag=low["tags"].fillna("(none)").str.split(";")).explode("tag").reset_index(drop=True)
by_tag = rows.groupby("tag").agg(
    games=("id", "count"), hits=("hit", "sum"),
    scan_games=("scan", "sum"),
    scan_hits=("scan_hit", "sum"),
    median_visits_day=("demand", "median"),
    median_fav_1k=("favorites_per_1k_visits", "median"),
    median_like=("like_pct", "median"),
    median_returning=("returning_visit_share_badges", "median"),
    median_session=("session_minutes_est", "median"),
).reset_index()
by_tag["scan_hit_rate_%"] = (100 * by_tag["scan_hits"] / by_tag["scan_games"].where(by_tag["scan_games"] >= 5)).round(1)
show("Tags, All ages and 9+ (scan hit rate needs 5+ scan-found games)",
     by_tag.sort_values("median_visits_day", ascending=False).round(2))

# 2. launch curves: players by game age, top 25% and median, by tag
d = daily.merge(games[["id", "tags", "low_age"]], on="id")
d = d[d["low_age"]].assign(tag=d["tags"].fillna("(none)").str.split(";")).explode("tag")
d["day"] = d["game_age_days"].round().astype(int)
marks = [1, 3, 7, 14, 30]
curve = (d[d["day"].isin(marks)].groupby(["tag", "day"])["players_avg"]
         .quantile(0.75).unstack("day").reindex(columns=marks).round(0).reset_index())
show("Top-25% players online by game age in days (All ages and 9+)", curve)

# 3. age rating moves
moved = games[games["age_rating_changes"].notna()]
show("Games whose age rating changed while tracked", moved[["name", "min_age_when_found", "age_rating_changes",
                                                            "players_peak", "created"]].head(40))

# 4. charts before or after growth (hits with hourly data)
try:
    hourly = pd.read_csv("hourly_hits.csv", encoding="utf-8-sig", parse_dates=["time_utc"])
    charts = pd.read_csv("charts.csv", encoding="utf-8-sig", parse_dates=["time_utc"])
    out = []
    for gid, h in hourly.groupby("id"):
        h = h.sort_values("time_utc")
        if h["players"].max() < HIT or h["players"].iloc[0] > 0.5 * h["players"].max():
            continue  # already big when found: the start of growth is not in the data
        took_off = h.loc[h["players"] >= 0.5 * h["players"].max(), "time_utc"].min()
        c = charts[charts["id"] == gid]
        # charts.csv starts when per-read chart logging began; first_chart_at in games.csv goes back further
        firsts = [x for x in (c["time_utc"].min() if len(c) else None,
                              pd.to_datetime(games.loc[games["id"] == gid, "first_chart_at"].squeeze(), utc=True))
                  if x is not None and not pd.isna(x)]
        first_chart = min(firsts) if firsts else None
        out.append({"id": gid, "name": games.loc[games["id"] == gid, "name"].squeeze(),
                    "reached_half_peak": took_off, "first_on_a_chart": first_chart,
                    "first_chart": c.sort_values("time_utc")["chart"].iloc[0] if len(c) else None,
                    "chart_hours_before_growth": None if first_chart is None else
                    round((took_off - first_chart).total_seconds() / 3600, 1)})
    show("Did chart placement come before growth? (positive hours = chart first)", pd.DataFrame(out))
except FileNotFoundError:
    pass

# 5. search terms with demand and weak results
try:
    sr = pd.read_csv("search_results.csv", encoding="utf-8-sig")
    last = sr["time_utc"].max()
    sr = sr[sr["time_utc"] == last]
    top10 = sr[sr["rank"] <= 10].groupby("query").agg(
        popularity=("query_popularity", "first"), new=("query_is_new", "first"),
        players_top10=("players", "sum"), best_result=("players", "max"),
        young_in_top10=("created_if_tracked", lambda s: int(s.notna().sum()))).reset_index()
    show("Search terms: popular but weakly served (sorted by popularity / players in top 10)",
         top10.assign(opening=top10["popularity"] / (1 + top10["players_top10"] / 1000))
         .sort_values("opening", ascending=False).head(40).round(2))
except FileNotFoundError:
    pass
