"""One collection run. The GitHub Action runs this every hour.

Every run:
  - Snapshots players online, visits, favorites, likes of tracked games that are due:
    hourly for games with players in their first DENSE_DAYS, every 3 hours for the rest of
    that window, then once a day until TRACK_DAYS.
  - Extras, within a time budget, oldest first: game passes and developer products, the
    creator's other games, badge award counts, public server fill, creator group members,
    icon and thumbnail changes.
Every FULL_EVERY_HOURS hours it also:
  - Reads every chart on the Roblox Charts page, adds games created in the last
    MAX_AGE_DAYS days and logs every tracked game's chart positions.
  - Scans the newest universe ids (each id is checked at 2, 10 and 20 days old), adds new
    public games that already have players, logs how many ids were checked and passed, and
    adds a small random sample of games that did not pass (the base rate).

Data lives in data/: games.json (one entry per tracked game), state.json (scan cursors,
run log), and one file per UTC day in snapshots/, charts/, badges/, servers/, groups/,
plus scan_log.jsonl.
"""

import datetime as dt
import json
import os
import random
import re
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tracker import roblox  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("TRACKER_DATA") or os.path.join(ROOT, "data")

# ---- settings ---------------------------------------------------------------
MAX_AGE_DAYS = 30          # a game counts as "new" if its universe was created this recently
TRACK_DAYS = 30            # how long each game is snapshotted after it is first found
DENSE_DAYS = 7             # first part of that window: hourly / 3-hourly snapshots, then daily
HOURLY_MIN_PEAK = 5        # games that have had this many players online get hourly snapshots
FULL_EVERY_HOURS = 3       # charts + scan this often (the Action itself runs hourly)
RUN_HOURS = 3              # used to size the first scan
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
SAMPLE_PER_RUN = 6          # random games below the bar tracked per full run (~50 a day): the base rate
ARCHIVE_AFTER_DAYS = 40     # finished games move out of games.json into data/archive/ after this
MAX_IDS_PER_PASS = 120_000  # cap per run so one run never takes too long (catches up later)
MAX_BEHIND_IDS = 600_000    # if a cursor falls further behind than this, skip ahead
MIN_VOTES = 2               # likes+dislikes needed before the slow details call
DEFAULT_FRONTIER = 10_777_000_000

# extras: how often per game (hours, in the dense window / after), who qualifies, seconds per run
DEEP_HOURS = (6, 23)
BADGE_MIN_PEAK = 5
SERVER_MIN_PLAYERS = 10
GROUP_MIN_PEAK = 5
MONETIZATION_RECHECK_DAYS = 7
ART_HOURS = 12
# servers and groups allow only ~4-8 calls a minute, so they cover the biggest games first, daily
BUDGET = {"enrich": 300, "badges": 180, "servers": 150, "groups": 90, "art": 60}  # halved on full runs

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


def hours_since(ts, t):
    return 1e9 if not ts else (t - parse(ts)).total_seconds() / 3600


def append_jsonl(folder, t, rows):
    """Append rows to data/<folder>/YYYY-MM-DD.jsonl (or data/<folder> itself if it ends in .jsonl)."""
    if not rows:
        return
    if folder.endswith(".jsonl"):
        path = os.path.join(DATA, folder)
    else:
        os.makedirs(os.path.join(DATA, folder), exist_ok=True)
        path = os.path.join(DATA, folder, t.strftime("%Y-%m-%d") + ".jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


def note_change(g, t, field, value):
    """Keep a history of title, description, icon and thumbnail changes."""
    h = g.setdefault("history", [])
    h.append([iso(t), field, value])
    del h[:-50]


# ---- tracking --------------------------------------------------------------
def refresh_details(g, d, t):
    """Fields that can change while a game is tracked (taken from a games.roblox.com entry)."""
    creator = d.get("creator") or {}
    name = d.get("name") or g["name"]
    if g.get("name") and name != g["name"]:
        note_change(g, t, "name", name)
    g["name"] = name
    g["creator_id"] = creator.get("id")
    g["creator_type"] = creator.get("type")
    g["creator_verified"] = creator.get("hasVerifiedBadge")
    g["genre"] = d.get("genre_l1") or g.get("genre")
    g["subgenre"] = d.get("genre_l2") or g.get("subgenre")
    desc = (d.get("description") or "").strip()
    desc = desc[:600] + ("…" if len(desc) > 600 else "")
    if "description" in g and desc != g["description"]:
        note_change(g, t, "description", desc)
    g["description"] = desc
    g["max_players"] = d.get("maxPlayers") or g.get("max_players")
    upd = d.get("updated")
    if upd:
        g["updated"] = upd
        # every distinct "updated" time seen after the game was found = one published update
        seen = g.setdefault("updates", [])
        if upd[:19] > g["first_seen"][:19] and upd[:19] not in seen:
            seen.append(upd[:19])
            del seen[:-60]


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
    refresh_details(g, detail, t)
    if source not in g["sources"]:
        g["sources"].append(source)
    if chart:
        note_chart(g, t, *chart)
    return g


def note_chart(g, t, sort_id, rank):
    if g["status"] != "tracking":
        return
    c = g["charts"].setdefault(sort_id, {"first": iso(t), "best": rank})
    c["last"] = iso(t)
    c["best"] = min(c["best"], rank)


def young(created, t):
    return created and (t - parse(created)).days <= MAX_AGE_DAYS


def collect_charts(games, t):
    seen = {}  # universeId -> list of (sortId, rank, country)
    for device, country in CHART_VIEWS:
        try:
            for sort_id, _name, entries in roblox.discovery_sorts(device, country):
                for rank, e in enumerate(entries, 1):
                    seen.setdefault(e["universeId"], []).append((sort_id, rank, country))
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
            for sort_id, rank, _c in seen[d["id"]]:
                add_game(games, d, "charts", t, (sort_id, rank))
    # every chart position of every tracked game, so chart placement can be compared with growth
    rows = []
    for uid, hits in seen.items():
        g = games.get(str(uid))
        if not g or g["status"] != "tracking":
            continue
        per_sort = {}
        for sort_id, rank, _c in hits:
            b = per_sort.setdefault(sort_id, [rank, 0])
            b[0] = min(b[0], rank)
            b[1] += 1
            note_chart(g, t, sort_id, rank)
        rows.append({"t": iso(t), "id": uid, "r": per_sort,
                     "c": sorted({c for _s, _r, c in hits if c != "all"})})
    append_jsonl("charts", t, rows)
    log(f"charts: positions logged for {len(rows)} tracked games")


def collect_scan(games, state, t, frontier):
    candidates = []
    bars = {name: (visits, playing) for name, _lag, visits, playing in SCAN_PASSES}
    stats = {}
    for name, lag, _v, _p in SCAN_PASSES:
        end = id_at_time(t - dt.timedelta(hours=lag), frontier)
        cur = state["passes"].get(name)
        if cur is None:
            cur = id_at_time(t - dt.timedelta(hours=lag + RUN_HOURS), frontier)
        cur = max(cur, end - MAX_BEHIND_IDS)
        stop = min(end, cur + MAX_IDS_PER_PASS)
        n_pub = n_new = 0
        for start in range(cur, stop, 100):
            for u in roblox.universes(range(start, min(start + 100, stop))):
                if u["privacyType"] != "Public" or u.get("isArchived") or DEFAULT_NAME.search(u["name"] or ""):
                    continue
                n_pub += 1
                if str(u["id"]) not in games:
                    n_new += 1
                    candidates.append((u["id"], name))
        log(f"{name}: scanned ids {cur}..{stop} ({stop - cur:,}), {n_pub} public with a real name"
            + ("" if stop == end else f", {end - stop:,} left for next run"))
        stats[name] = {"t": iso(t), "pass": name, "from": cur, "to": stop, "ids": stop - cur,
                       "public_named": n_pub, "not_yet_tracked": n_new, "voted": 0, "added": 0, "sampled": 0}
        state["passes"][name] = stop

    # likes/dislikes are cheap to check; only games with some votes get the slow details call
    voted = []
    src = dict(candidates)
    for ch in roblox.chunks(src):
        for v in roblox.votes(ch):
            if v.get("id") in src and v["upVotes"] + v["downVotes"] >= MIN_VOTES:
                voted.append(v["id"])
                stats[src[v["id"]]]["voted"] += 1
    log(f"scan: {len(src)} candidates, {len(voted)} have {MIN_VOTES}+ votes")
    added = 0
    for ch in roblox.chunks(voted):
        for d in roblox.games(ch):
            if d.get("id") not in src:  # the API sometimes returns stray entries (id 0)
                continue
            min_visits, min_playing = bars[src[d["id"]]]
            if young(d.get("created"), t) and (d["visits"] >= min_visits or d["playing"] >= min_playing):
                add_game(games, d, src[d["id"]], t)
                stats[src[d["id"]]]["added"] += 1
                added += 1
    log(f"scan: {added} added")

    # base rate: a random handful of 2-day-old public games that did not clear the bar
    pool = [i for i, name in candidates if name == "scan-2d" and str(i) not in games]
    pick = random.sample(pool, min(SAMPLE_PER_RUN, len(pool)))
    for ch in roblox.chunks(pick):
        for d in roblox.games(ch):
            if d.get("id") in pick and young(d.get("created"), t):
                add_game(games, d, "sample", t)
                stats["scan-2d"]["sampled"] += 1
    log(f"scan: {stats['scan-2d']['sampled']} random below-the-bar games added as a sample")
    append_jsonl("scan_log.jsonl", t, list(stats.values()))


def snapshot_due(g, t, full):
    since = hours_since(g.get("last_snap"), t)
    age_days = (t - parse(g["first_seen"])).total_seconds() / 86400
    if age_days <= DENSE_DAYS:
        if since < 0.75:
            return False
        return full or (g.get("peak") or 0) >= HOURLY_MIN_PEAK or since >= 2.75
    return since >= 22.5


def snapshot(games, t, full):
    for g in games.values():
        if g["status"] == "tracking" and t - parse(g["first_seen"]) > dt.timedelta(days=TRACK_DAYS, hours=1):
            g["status"] = "done"
    due = [g["id"] for g in games.values() if g["status"] == "tracking" and snapshot_due(g, t, full)]
    rows = {}
    for ch in roblox.chunks(due):
        for d in roblox.games(ch):
            if str(d.get("id")) not in games or d["id"] not in ch:
                continue
            g = games[str(d["id"])]
            refresh_details(g, d, t)
            g["peak"] = max(g.get("peak") or 0, d["playing"])
            g["last_p"], g["last_v"], g["last_snap"], g["miss"] = d["playing"], d["visits"], iso(t), 0
            rows[d["id"]] = {"t": iso(t), "id": d["id"], "p": d["playing"], "v": d["visits"], "f": d["favoritedCount"]}
        for v in roblox.votes(ch):
            if v.get("id") in rows:
                rows[v["id"]].update(u=v["upVotes"], d=v["downVotes"])
    gone = 0
    for gid in due:
        if gid not in rows:  # missing: deleted or made private (3 misses in a row, so one bad reply is not enough)
            g = games[str(gid)]
            g["miss"] = g.get("miss", 0) + 1
            if g["miss"] >= 3:
                g["status"] = "gone"
                gone += 1
    append_jsonl("snapshots", t, list(rows.values()))
    log(f"snapshot: {len(rows)} of {len(due)} due games ({gone} gone)")


# ---- extras (each runs oldest-first inside its own time budget) -------------
def deep_due(g, key, t):
    dense = (t - parse(g["first_seen"])).total_seconds() / 86400 <= DENSE_DAYS
    return hours_since(g.get(key), t) >= DEEP_HOURS[0 if dense else 1]


FULL_RUN = False


def run_budgeted(name, todo, fn):
    deadline = time.time() + BUDGET[name] * (0.5 if FULL_RUN else 1)
    done = 0
    for item in todo:
        if time.time() > deadline:
            break
        try:
            fn(item)
            done += 1
        except Exception as e:
            log(f"  {name}: {e}")
    log(f"{name}: {done} done" + (f", {len(todo) - done} left for later runs" if done < len(todo) else ""))


def _prices(items):
    ps = sorted(p["price"] for p in items or [] if p.get("price"))
    return ps[len(ps) // 2] if ps else None


def enrich(games, t):
    """Game passes and developer products for every tracked game (rechecked weekly), and the creator's other games."""
    todo = [g for g in games.values() if g["status"] == "tracking" and g.get("creator_id")
            and hours_since(g.get("mon_t"), t) >= MONETIZATION_RECHECK_DAYS * 24]
    todo.sort(key=lambda g: ("mon_t" in g, -(g.get("peak") or 0)))

    def one(g):
        g["passes"] = roblox.game_passes(g["id"])
        products = roblox.developer_products(g["id"])
        if products is not None:
            g["products"] = products
        g["mon_t"] = iso(t)
        log_ = g.setdefault("mon_log", [])
        log_.append({"t": iso(t), "passes": len(g["passes"]), "passMed": _prices(g["passes"]),
                     "products": None if products is None else len(products), "productMed": _prices(products)})
        if "creator_games" not in g:
            others = [x for x in roblox.creator_games(g["creator_type"], g["creator_id"]) if x.get("id") != g["id"]]
            visits = sorted((x.get("placeVisits") or 0 for x in others), reverse=True)
            g["creator_games"] = {
                "count": len(others),
                "total_visits": sum(visits),
                "best_visits": visits[0] if visits else 0,
                "best_name": max(others, key=lambda x: x.get("placeVisits") or 0)["name"] if others else None,
            }
    run_budgeted("enrich", todo, one)


def collect_badges(games, t):
    todo = [g for g in games.values() if g["status"] == "tracking" and (g.get("peak") or 0) >= BADGE_MIN_PEAK
            and deep_due(g, "badge_t", t)]
    todo.sort(key=lambda g: g.get("badge_t") or "")
    rows = []

    def one(g):
        bs = roblox.badges(g["id"])
        g["badge_t"] = iso(t)
        meta = g.setdefault("badges", {})
        for b in bs:
            meta.setdefault(str(b["id"]), [b["name"], b["description"], b["created"]])
        if bs:
            rows.append({"t": iso(t), "id": g["id"], "v": g.get("last_v"),
                         "b": [[b["id"], b["awarded"], b["pastDay"]] for b in bs]})
    run_budgeted("badges", todo, one)
    append_jsonl("badges", t, rows)


def collect_servers(games, t):
    todo = [g for g in games.values() if g["status"] == "tracking" and (g.get("last_p") or 0) >= SERVER_MIN_PLAYERS
            and g.get("place") and hours_since(g.get("server_t"), t) >= DEEP_HOURS[1]]
    todo.sort(key=lambda g: -(g.get("last_p") or 0))
    rows = []

    def one(g):
        srv, more = roblox.servers(g["place"])
        g["server_t"] = iso(t)
        if not srv:
            return
        playing = [s.get("playing") or 0 for s in srv]
        cap = max((s.get("maxPlayers") or 0) for s in srv) or g.get("max_players") or 1
        rows.append({"t": iso(t), "id": g["id"], "n": len(srv), "more": more, "max": cap,
                     "players": sum(playing), "fill": round(sum(playing) / (cap * len(srv)), 3),
                     "solo": round(sum(p <= 1 for p in playing) / len(srv), 3),
                     "full": round(sum(p >= cap for p in playing) / len(srv), 3),
                     "ping": round(sum(s.get("ping") or 0 for s in srv) / len(srv)),
                     "fps": round(sum(s.get("fps") or 0 for s in srv) / len(srv), 1)})
    run_budgeted("servers", todo, one)
    append_jsonl("servers", t, rows)


def collect_groups(games, state, t):
    seen = state.setdefault("groups", {})
    want = {}
    for g in games.values():
        if g["status"] == "tracking" and g.get("creator_type") == "Group" and g.get("creator_id") \
                and (g.get("peak") or 0) >= GROUP_MIN_PEAK:
            want.setdefault(str(g["creator_id"]), []).append(g)
    todo = [k for k in want if hours_since(seen.get(k), t) >= DEEP_HOURS[1]]
    todo.sort(key=lambda k: -max(g.get("peak") or 0 for g in want[k]))
    rows = []

    def one(gid):
        m = roblox.group_members(gid)
        seen[gid] = iso(t)
        rows.append({"t": iso(t), "group": int(gid), "m": m})
        for g in want[gid]:
            g["group_members"] = m
    run_budgeted("groups", todo, one)
    for k in [k for k, v in seen.items() if hours_since(v, t) > 24 * 45]:
        del seen[k]
    append_jsonl("groups", t, rows)


def collect_art(games, t):
    """Icon and first thumbnail per game; changes go into the game's history."""
    todo = [g for g in games.values() if g["status"] == "tracking" and hours_since(g.get("art_t"), t) >= ART_HOURS]
    todo.sort(key=lambda g: g.get("art_t") or "")
    by_id = {g["id"]: g for g in todo}

    def one(ch):
        for i in roblox.icons(ch):
            g = by_id.get(i.get("targetId"))
            if g and i.get("imageUrl"):
                if g.get("icon") and g["icon"] != i["imageUrl"]:
                    note_change(g, t, "icon", i["imageUrl"])
                g["icon"] = i["imageUrl"]
        for th in roblox.thumbnails(ch):
            g = by_id.get(th["universeId"])
            if g:
                if g.get("thumb") and g["thumb"] != th["url"]:
                    note_change(g, t, "thumbnail", th["url"])
                g["thumb"] = th["url"]
        for gid in ch:
            by_id[gid]["art_t"] = iso(t)
    run_budgeted("art", list(roblox.chunks(by_id)), one)


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
    full = hours_since(state.get("last_full"), t) >= FULL_EVERY_HOURS - 0.5
    steps = [a for a in sys.argv[1:]] or (["charts", "scan"] if full else []) + ["snapshot", "extras"]
    full = "charts" in steps
    global FULL_RUN
    FULL_RUN = full
    log(f"run: {'full' if full else 'light'} ({', '.join(steps)})")

    failed = []

    def step(name, fn):
        try:
            fn()
            save("games.json", games)
        except Exception:
            traceback.print_exc()
            failed.append(name)

    # One failing step must not stop the others; the snapshot matters most.
    if "charts" in steps:
        step("charts", lambda: collect_charts(games, t))
        state["last_full"] = iso(t)
    if "scan" in steps:
        passes_before = dict(state["passes"])

        def scan():
            frontier = find_frontier(state["frontier"])
            state["frontier"] = frontier
            log(f"newest universe id ~{frontier:,}")
            collect_scan(games, state, t, frontier)
        step("scan", scan)
        if "scan" in failed:
            state["passes"] = passes_before  # rescan the same ids next run
        save("state.json", state)
    if "snapshot" in steps:
        step("snapshot", lambda: snapshot(games, t, full))
        step("archive", lambda: archive(games, t))
    if "extras" in steps:
        step("enrich", lambda: enrich(games, t))
        step("badges", lambda: collect_badges(games, t))
        step("servers", lambda: collect_servers(games, t))
        step("groups", lambda: collect_groups(games, state, t))
        step("art", lambda: collect_art(games, t))

    state["runs"] = (state.get("runs", []) + [{
        "t": iso(t), "mode": "full" if full else "light", "seconds": round((now_utc() - t).total_seconds()),
        "calls": roblox.stats["calls"], "retries": roblox.stats["retries"],
        "tracking": sum(1 for g in games.values() if g["status"] == "tracking"),
        "failed": failed,
    }])[-1000:]
    save("state.json", state)
    log("run:", state["runs"][-1])
    if failed:
        sys.exit(1)  # data is saved; the workflow still publishes, but the run shows red


if __name__ == "__main__":
    main()
