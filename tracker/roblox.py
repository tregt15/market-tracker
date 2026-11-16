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
GAPS = {"/v1/games?": 1.0}
MIN_GAPS = {"/v1/games?": 0.5}
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
