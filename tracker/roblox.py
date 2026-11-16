"""Small client for the public Roblox web APIs the tracker uses.

Only the standard library is used, so the GitHub Action needs no installs.
Every call goes through `get_json`, which paces requests and backs off on 429s.
"""

import json
import time
import urllib.error
import urllib.request

USER_AGENT = "roblox-market-tracker/1.0"
BATCH = 50  # games.roblox.com returns nothing for more than 50 ids

# Pause between two requests to the same endpoint. The game details endpoint
# (/v1/games?universeIds=) is the strict one; its pause adapts: it doubles after a
# 429 and slowly shrinks back while calls succeed.
GAPS = {"/v1/games?": 1.0, "games.roblox.com/v2/": 1.5,  # v2 = creator game lists
        "/servers/Public": 4.0, "groups.roblox.com": 2.0}  # both allow only a few calls a minute
MIN_GAPS = {"/v1/games?": 0.5, "games.roblox.com/v2/": 1.0, "/servers/Public": 2.0, "groups.roblox.com": 1.0}
DEFAULT_GAP = 0.3

_last_call = {}
stats = {"calls": 0, "retries": 0}


def _gap_key(url):
    for key in GAPS:
        if key in url:
            return key
    return url.split("/")[2]


def get_json(url, tries=8, body=None):
    key = _gap_key(url)
    for attempt in range(tries):
        gap = GAPS.get(key, DEFAULT_GAP)
        wait = gap - (time.time() - _last_call.get(key, 0))
        if wait > 0:
            time.sleep(wait)
        _last_call[key] = time.time()
        stats["calls"] += 1
        try:
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            if key in MIN_GAPS:
                GAPS[key] = max(MIN_GAPS[key], GAPS[key] * 0.97)
            return out
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                stats["retries"] += 1
                if e.code == 429 and key in MIN_GAPS:
                    GAPS[key] = min(15.0, GAPS[key] * 2)
                time.sleep(min(60, 4 * (attempt + 1)))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            stats["retries"] += 1
            time.sleep(min(60, 4 * (attempt + 1)))
    raise RuntimeError(f"gave up after {tries} tries: {url[:120]}")


def chunks(items, size=BATCH):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def games(universe_ids):
    """Details (name, created, visits, playing, favorites, genre, creator) for up to 50 ids."""
    ids = ",".join(str(i) for i in universe_ids)
    return get_json(f"https://games.roblox.com/v1/games?universeIds={ids}").get("data", [])


def universes(universe_ids):
    """Light details for up to 100 ids: name, created, privacyType. Fast, loose rate limit."""
    q = "&".join(f"ids={i}" for i in universe_ids)
    return get_json(f"https://develop.roblox.com/v1/universes/multiget?{q}").get("data", [])


def age_rating(universe_id):
    """(minimumAge, contentMaturity) such as (0, 'minimal') or (13, 'moderate')."""
    url = "https://apis.roblox.com/experience-guidelines-api/experience-guidelines/get-age-recommendation"
    try:
        d = get_json(url, body={"universeId": str(universe_id)}, tries=3)
        rec = d["ageRecommendationDetails"]["summary"]["ageRecommendation"]
        return rec.get("minimumAge"), rec.get("contentMaturity")
    except Exception:
        return None, None


def game_passes(universe_id):
    """[{name, price}] for a game's passes that are for sale (first 100)."""
    url = f"https://apis.roblox.com/game-passes/v1/universes/{universe_id}/game-passes?passView=Full&pageSize=100"
    try:
        d = get_json(url, tries=3)
    except Exception:
        return []
    return [{"name": (p.get("displayName") or p.get("name") or "")[:60], "price": p.get("price")}
            for p in d.get("gamePasses") or [] if p.get("isForSale")]


def creator_games(creator_type, creator_id):
    """Public games by the same user or group (up to 50): [{id, name, placeVisits, created}]."""
    if creator_type == "Group":
        url = f"https://games.roblox.com/v2/groups/{creator_id}/gamesV2?accessFilter=Public&limit=50"
    else:
        url = f"https://games.roblox.com/v2/users/{creator_id}/games?accessFilter=Public&limit=50"
    try:
        return get_json(url, tries=3).get("data") or []
    except Exception:
        return []


def developer_products(universe_id, pages=2):
    """[{name, price}] for a game's developer products (consumables) that are for sale."""
    out, cursor = [], ""
    for _ in range(pages):
        url = f"https://apis.roblox.com/developer-products/v2/universes/{universe_id}/developerproducts?limit=50"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            d = get_json(url, tries=3)
        except Exception:
            return out if out else None
        out += [{"name": (p.get("displayName") or p.get("Name") or "")[:60], "price": p.get("PriceInRobux")}
                for p in d.get("developerProducts") or [] if p.get("IsForSale")]
        cursor = d.get("nextPageCursor")
        if not cursor:
            break
    return out


def badges(universe_id, pages=3):
    """[{id, name, description, created, awarded, pastDay, winRate}] for a game's enabled badges."""
    out, cursor = [], ""
    for _ in range(pages):
        url = f"https://badges.roblox.com/v1/universes/{universe_id}/badges?limit=100&sortOrder=Asc"
        if cursor:
            url += f"&cursor={cursor}"
        d = get_json(url, tries=3)
        for b in d.get("data") or []:
            st = b.get("statistics") or {}
            if b.get("enabled"):
                out.append({"id": b["id"], "name": (b.get("displayName") or b.get("name") or "")[:80],
                            "description": (b.get("displayDescription") or b.get("description") or "")[:160],
                            "created": (b.get("created") or "")[:19], "awarded": st.get("awardedCount"),
                            "pastDay": st.get("pastDayAwardedCount"), "winRate": st.get("winRatePercentage")})
        cursor = d.get("nextPageCursor")
        if not cursor:
            break
    return out


def servers(place_id):
    """First page (up to 100) of a place's public servers: [{playing, maxPlayers, fps, ping}], more_pages."""
    d = get_json(f"https://games.roblox.com/v1/games/{place_id}/servers/Public?limit=100&sortOrder=Desc&excludeFullGames=false",
                 tries=3)
    return d.get("data") or [], bool(d.get("nextPageCursor"))


def group_members(group_id):
    return get_json(f"https://groups.roblox.com/v1/groups/{group_id}", tries=3).get("memberCount")


def thumbnails(universe_ids):
    """First thumbnail (screenshot) per game: [{universeId, url}]."""
    ids = ",".join(str(i) for i in universe_ids)
    url = f"https://thumbnails.roblox.com/v1/games/multiget/thumbnails?universeIds={ids}&countPerUniverse=1&size=768x432&format=Png&defaults=false"
    out = []
    for d in get_json(url).get("data", []):
        th = [x for x in d.get("thumbnails") or [] if x.get("imageUrl")]
        if th:
            out.append({"universeId": d.get("universeId"), "url": th[0]["imageUrl"]})
    return out


def votes(universe_ids):
    ids = ",".join(str(i) for i in universe_ids)
    return get_json(f"https://games.roblox.com/v1/games/votes?universeIds={ids}").get("data", [])


def icons(universe_ids):
    ids = ",".join(str(i) for i in universe_ids)
    url = f"https://thumbnails.roblox.com/v1/games/icons?universeIds={ids}&size=150x150&format=Png&isCircular=false"
    return get_json(url).get("data", [])


def discovery_sorts(device="all", country="all"):
    """Yield every chart on the Charts page: (sort_id, display_name, [game dicts])."""
    token = ""
    for _ in range(20):
        url = f"https://apis.roblox.com/explore-api/v1/get-sorts?sessionId=market-tracker&device={device}&country={country}"
        if token:
            url += f"&sortsPageToken={token}"
        page = get_json(url)
        for sort in page.get("sorts") or []:
            if sort.get("contentType") == "Games" and sort.get("games"):
                yield sort.get("sortId"), sort.get("sortDisplayName"), sort["games"]
        token = page.get("nextSortsPageToken")
        if not token:
            return
