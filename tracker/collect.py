"""One collection run. The GitHub Action runs this every 3 hours.

Each run:
  1. Reads every chart on the Roblox Charts page and adds games created in the last
     MAX_AGE_DAYS days.
  2. Scans the newest universe ids (each id is checked when it is 2 days old and again
     at 10 days old) and adds new public games that already have players.
  3. Takes a snapshot (players online, visits, favorites, likes) of every game that is
     still inside its TRACK_DAYS window.

Data lives in data/: games.json (one entry per tracked game), state.json (scan
cursors), snapshots/YYYY-MM-DD.jsonl (one line per game per run).
"""

import datetime as dt
import json
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tracker import roblox  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# ---- settings ---------------------------------------------------------------
MAX_AGE_DAYS = 30          # a game counts as "new" if its universe was created this recently
TRACK_DAYS = 7             # how long each game is snapshotted after it is first found
RUN_HOURS = 3              # how often the Action runs (used to size the first scan)
# (device, country) views of the Charts page; the lists differ by region and device
CHART_VIEWS = [("all", "all"), ("phone", "all"), ("computer", "all"), ("console", "all")] + [
    ("all", c) for c in ("us", "gb", "ca", "au", "br", "mx", "ph", "id", "de", "fr", "es", "tr", "ru", "pl", "th", "vn", "jp", "kr")
]
# Each universe id is checked at several ages: (name, hours after creation, min visits, or min players online).
# A scanned game is tracked the first time it clears one of the two bars.
SCAN_PASSES = [
    ("scan-2d", 48, 300, 2),
    ("scan-10d", 240, 1_000, 3),
    ("scan-20d", 480, 2_000, 3),
]
ARCHIVE_AFTER_DAYS = 21     # finished games move out of games.json into data/archive/ after this
MAX_IDS_PER_PASS = 120_000  # cap per run so one run never takes too long (catches up later)
MAX_BEHIND_IDS = 600_000    # if a cursor falls further behind than this, skip ahead
MIN_VOTES = 2               # likes+dislikes needed before the slow details call
DEFAULT_FRONTIER = 10_777_000_000

DEFAULT_NAME = re.compile(
    r"(untitled|sem t[ií]tulo|sin t[ií]tulo|sans titre|ohne titel|senza titolo|"
    r"bez nazwy|başlıksız|без названия|'s place|place number|baseplate|^test\d*$)",
    re.I,
)


# ---- small helpers ----------------------------------------------------------
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts):
    """Roblox timestamps look like 2026-11-16T02:12:22.47Z; keep whole seconds only."""
    return dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)


def load(name, default):
    path = os.path.join(DATA, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save(name, obj):
    path = os.path.join(DATA, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0, separators=(",", ":"))
    os.replace(tmp, path)


def log(*a):
    print(*a, flush=True)


# ---- finding ids by time ----------------------------------------------------
def first_universe(start, width=100):
    d = roblox.universes(range(start, start + width))
    return min(d, key=lambda x: x["id"]) if d else None


def find_frontier(hint):
    """Highest id (to the nearest 100) that exists right now."""
    lo = hint if first_universe(hint) else hint - 2_000_000
    step = 5_000
    hi = lo + step
    while first_universe(hi):
        lo, step = hi, step * 2
        hi = lo + step
    while hi - lo > 100:
        mid = (lo + hi) // 2
        if first_universe(mid):
            lo = mid
        else:
            hi = mid
    return lo


def id_at_time(when, frontier):
    """Approximate id of the universe created at `when` (binary search on created dates)."""
    lo, hi = frontier - 20_000_000, frontier
    while hi - lo > 1_000:
        mid = (lo + hi) // 2
        u = first_universe(mid, 20)
        if u is None or parse(u["created"]) > when:
            hi = mid
        else:
            lo = mid
    return lo


# ---- tracking --------------------------------------------------------------
def add_game(games, detail, source, t, chart=None):
    gid = str(detail["id"])
    g = games.get(gid)
    if g is None:
        g = games[gid] = {
            "id": detail["id"],
            "place": detail.get("rootPlaceId"),
            "name": detail.get("name"),
            "creator": (detail.get("creator") or {}).get("name"),
            "creator_type": (detail.get("creator") or {}).get("type"),
            "created": detail.get("created"),
            "genre": detail.get("genre_l1") or detail.get("genre"),
            "subgenre": detail.get("genre_l2"),
            "max_players": detail.get("maxPlayers"),
            "first_seen": iso(t),
            "sources": [],
            "charts": {},
            "status": "tracking",
        }
        g["min_age"], g["maturity"] = roblox.age_rating(detail["id"])
        log(f"  + {g['name']!r} ({source}) created {g['created'][:10]} visits {detail.get('visits')}")
    if source not in g["sources"]:
        g["sources"].append(source)
    if chart and g["status"] == "tracking":
        sort_id, rank = chart
        c = g["charts"].setdefault(sort_id, {"first": iso(t), "best": rank})
        c["last"] = iso(t)
        c["best"] = min(c["best"], rank)
    return g


def young(created, t):
    return created and (t - parse(created)).days <= MAX_AGE_DAYS


def collect_charts(games, t):
    seen = {}  # universeId -> list of (sortId, rank)
    for device, country in CHART_VIEWS:
        try:
            for sort_id, _name, entries in roblox.discovery_sorts(device, country):
                for rank, e in enumerate(entries, 1):
                    seen.setdefault(e["universeId"], []).append((sort_id, rank))
        except Exception as e:  # one failing view should not stop the run
            log("  chart view failed", device, country, e)
    log(f"charts: {len(seen)} unique games")
    # created dates come from the fast endpoint; details only for the young ones
    created = {}
    for ch in roblox.chunks(seen, 100):
        for u in roblox.universes(ch):
            created[u["id"]] = u["created"]
    young_ids = [i for i in seen if young(created.get(i), t)]
    log(f"charts: {len(young_ids)} were created in the last {MAX_AGE_DAYS} days")
    for ch in roblox.chunks(young_ids):
        for d in roblox.games(ch):
            if d.get("id") not in seen:
                continue
            for sort_id, rank in seen[d["id"]]:
                add_game(games, d, "charts", t, (sort_id, rank))


def collect_scan(games, state, t, frontier):
    candidates = []
    bars = {name: (visits, playing) for name, _lag, visits, playing in SCAN_PASSES}
    for name, lag, _v, _p in SCAN_PASSES:
        end = id_at_time(t - dt.timedelta(hours=lag), frontier)
        cur = state["passes"].get(name)
        if cur is None:
            cur = id_at_time(t - dt.timedelta(hours=lag + RUN_HOURS), frontier)
        cur = max(cur, end - MAX_BEHIND_IDS)
        stop = min(end, cur + MAX_IDS_PER_PASS)
        n_pub = 0
        for start in range(cur, stop, 100):
            for u in roblox.universes(range(start, min(start + 100, stop))):
                if u["privacyType"] != "Public" or u.get("isArchived") or DEFAULT_NAME.search(u["name"] or ""):
                    continue
                n_pub += 1
                if str(u["id"]) not in games:
                    candidates.append((u["id"], name))
        log(f"{name}: scanned ids {cur}..{stop} ({stop - cur:,}), {n_pub} public with a real name"
            + ("" if stop == end else f", {end - stop:,} left for next run"))
        state["passes"][name] = stop

    # likes/dislikes are cheap to check; only games with some votes get the slow details call
    voted = []
    src = dict(candidates)
    for ch in roblox.chunks(src):
        for v in roblox.votes(ch):
            if v["upVotes"] + v["downVotes"] >= MIN_VOTES:
                voted.append(v["id"])
    log(f"scan: {len(src)} candidates, {len(voted)} have {MIN_VOTES}+ votes")
    added = 0
    for ch in roblox.chunks(voted):
        for d in roblox.games(ch):
            if d.get("id") not in src:  # the API sometimes returns stray entries (id 0)
                continue
            min_visits, min_playing = bars[src[d["id"]]]
            if young(d.get("created"), t) and (d["visits"] >= min_visits or d["playing"] >= min_playing):
                add_game(games, d, src[d["id"]], t)
                added += 1
    log(f"scan: {added} added")


def snapshot(games, t):
    active = [g for g in games.values() if g["status"] == "tracking"]
    for g in active:
        if t - parse(g["first_seen"]) > dt.timedelta(days=TRACK_DAYS, hours=1):
            g["status"] = "done"
    active = [g["id"] for g in active if g["status"] == "tracking"]
    rows = {}
    for ch in roblox.chunks(active):
        for d in roblox.games(ch):
            if str(d.get("id")) not in games:
                continue
            g = games[str(d["id"])]
            g["name"], g["updated"] = d.get("name"), d.get("updated")
            rows[d["id"]] = {"t": iso(t), "id": d["id"], "p": d["playing"], "v": d["visits"], "f": d["favoritedCount"]}
        for v in roblox.votes(ch):
            if v["id"] in rows:
                rows[v["id"]].update(u=v["upVotes"], d=v["downVotes"])
    missing = [g for g in active if g not in rows]
    for gid in missing:  # deleted or made private
        games[str(gid)]["status"] = "gone"
    os.makedirs(os.path.join(DATA, "snapshots"), exist_ok=True)
    with open(os.path.join(DATA, "snapshots", t.strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8") as f:
        for r in rows.values():
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    # icons for games that do not have one yet
    need = [g["id"] for g in games.values() if g["status"] == "tracking" and not g.get("icon")]
    for ch in roblox.chunks(need):
        for i in roblox.icons(ch):
            if i.get("imageUrl") and str(i.get("targetId")) in games:
                games[str(i["targetId"])]["icon"] = i["imageUrl"]
    log(f"snapshot: {len(rows)} games ({len(missing)} gone)")


def archive(games, t):
    """Move finished games older than ARCHIVE_AFTER_DAYS into data/archive/games-YYYY-MM.json."""
    old = [k for k, g in games.items()
           if g["status"] != "tracking" and t - parse(g["first_seen"]) > dt.timedelta(days=ARCHIVE_AFTER_DAYS)]
    by_month = {}
    for k in old:
        by_month.setdefault(games[k]["first_seen"][:7], {})[k] = games.pop(k)
    os.makedirs(os.path.join(DATA, "archive"), exist_ok=True)
    for month, items in by_month.items():
        name = os.path.join("archive", f"games-{month}.json")
        merged = load(name, {})
        merged.update(items)
        save(name, merged)
    if old:
        log(f"archived {len(old)} finished games")


def main():
    t = now_utc()
    games = load("games.json", {})
    state = load("state.json", {"frontier": DEFAULT_FRONTIER, "passes": {}, "runs": []})
    steps = [a for a in sys.argv[1:]] or ["charts", "scan", "snapshot"]

    failed = []

    # One failing step must not stop the others; the snapshot matters most.
    if "charts" in steps:
        try:
            collect_charts(games, t)
            save("games.json", games)
        except Exception:
            traceback.print_exc()
            failed.append("charts")
    if "scan" in steps:
        passes_before = dict(state["passes"])
        try:
            frontier = find_frontier(state["frontier"])
            state["frontier"] = frontier
            log(f"newest universe id ~{frontier:,}")
            collect_scan(games, state, t, frontier)
            save("games.json", games)
        except Exception:
            traceback.print_exc()
            state["passes"] = passes_before  # rescan the same ids next run
            failed.append("scan")
        save("state.json", state)
    if "snapshot" in steps:
        try:
            snapshot(games, t)
            archive(games, t)
            save("games.json", games)
        except Exception:
            traceback.print_exc()
            failed.append("snapshot")

    state["runs"] = (state.get("runs", []) + [{
        "t": iso(t), "seconds": round((now_utc() - t).total_seconds()),
        "calls": roblox.stats["calls"], "retries": roblox.stats["retries"],
        "tracking": sum(1 for g in games.values() if g["status"] == "tracking"),
        "failed": failed,
    }])[-200:]
    save("state.json", state)
    log("run:", state["runs"][-1])
    if failed:
        sys.exit(1)  # data is saved; the workflow still publishes, but the run shows red


if __name__ == "__main__":
    main()
