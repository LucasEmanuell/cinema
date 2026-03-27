"""
core.py — Data layer: cache, ingresso.com API calls, pure helpers.

No I/O, no display, no sys.exit — raises APIError on failure so both
the CLI and the web API can handle errors their own way.
"""
import json
import time
import unicodedata
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

CITY_ID      = 36               # Fortaleza-CE
PARTNERSHIP  = "ingresso.com"
CONTENT_API  = "https://api-content.ingresso.com/v0"
CHECKOUT_API = "https://api.ingresso.com/v1"
CACHE_DIR    = Path.home() / ".cache" / "cinema-fortaleza"

TTL = {
    "movies":    3600,  # 1h  — catalog changes slowly
    "sessions":   900,  # 15m — schedule is stable once published
    "tickets":   3600,  # 1h  — prices rarely change intraday
    "seats":      300,  # 5m  — occupancy changes in real time
    "cities":   86400,  # 24h — cities almost never change
    "theaters":  3600,  # 1h  — theater list is stable
}


class APIError(Exception):
    pass


# ── Schema validation ─────────────────────────────────────────────────────────
#
# When ingresso.com changes their response format, fields we rely on may
# disappear silently — causing confusing KeyErrors or empty output instead of
# a clear error message.
#
# check_schema() is called after every live API response (cache hits are
# skipped — the format was already validated when the response was stored).
# If required fields are missing it writes a warning to:
#   ~/.cache/cinema-fortaleza/schema_warnings.log
#
# The app continues to work normally; the log is the notification.

# Required top-level fields per response type.
# For list responses the check is applied to the first item in the list.
_SCHEMAS: dict[str, set[str]] = {
    "movies":          {"id", "title", "urlKey", "isPlaying"},
    "sessions":        {"date", "theaters"},
    "tickets":         {"default"},
    "seats":           {"totalSeats", "lines", "labels", "stage"},
    "states":          {"name", "uf", "cities"},
    "theaters":        {"id", "name", "rooms"},
    "theater_sessions": {"date", "movies"},
}


def _warn_schema(name: str, missing: set[str], sample: dict) -> None:
    """Append a schema-change warning to the log file."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = CACHE_DIR / "schema_warnings.log"
    ts    = time.strftime("%Y-%m-%d %H:%M:%S")
    got   = sorted(sample.keys()) if isinstance(sample, dict) else type(sample).__name__
    entry = (
        f"[{ts}] SCHEMA WARNING — {name}\n"
        f"  Missing fields : {sorted(missing)}\n"
        f"  Fields present : {got}\n"
        f"  Action needed  : the ingresso.com API response format may have changed.\n"
        f"                   Review core.py and update _SCHEMAS if the field was\n"
        f"                   renamed, or update the code that reads it.\n\n"
    )
    with log_path.open("a") as f:
        f.write(entry)


def check_schema(data, name: str) -> None:
    """
    Validate that an API response still contains the expected fields.

    Called after every live fetch (not on cache hits). Silent on success.
    Writes to schema_warnings.log and returns normally on failure — the app
    keeps running so users are not blocked while the issue is investigated.
    """
    if not data or name not in _SCHEMAS:
        return
    sample = data[0] if isinstance(data, list) else data
    if not isinstance(sample, dict):
        return
    missing = _SCHEMAS[name] - sample.keys()
    if missing:
        _warn_schema(name, missing, sample)


# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"

def cache_get(key: str):
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text())
        if time.time() - obj["ts"] < obj["ttl"]:
            return obj["v"]
    except Exception:
        pass
    return None

def cache_set(key: str, value, ttl: int):
    _cache_path(key).write_text(
        json.dumps({"ts": time.time(), "ttl": ttl, "v": value})
    )

# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch(url: str, params: dict = None, cache_key: str = None, ttl: int = 300,
          schema: str = None):
    """
    Fetch a URL with optional caching.

    schema: if provided and this is a live (non-cached) response, the result
            is validated against _SCHEMAS[schema]. Warnings are logged to
            ~/.cache/cinema-fortaleza/schema_warnings.log.
    """
    if cache_key:
        hit = cache_get(cache_key)
        if hit is not None:
            return hit  # cache hit — format was validated when stored

    try:
        r = requests.get(url, params=params, timeout=10,
                         headers={"User-Agent": "cinema-fortaleza/1.0"})
    except requests.RequestException as e:
        raise APIError(str(e))

    if r.status_code == 204:
        return None
    if not r.ok:
        return None

    data = r.json()
    if schema:
        check_schema(data, schema)   # validate before caching
    if cache_key:
        cache_set(cache_key, data, ttl)
    return data

# ── API calls ─────────────────────────────────────────────────────────────────

def api_movies(city_id: int = CITY_ID):
    data = fetch(
        f"{CONTENT_API}/events",
        params={"partnership": PARTNERSHIP, "cityId": city_id, "isPlaying": "true"},
        cache_key=f"movies_{city_id}",
        ttl=TTL["movies"],
        schema="movies",
    )
    if not data:
        return []
    return sorted(
        [m for m in data if m.get("isPlaying")],
        # pre-sale movies (not yet in theaters) float to the bottom
        key=lambda m: (bool(m.get("inPreSale")), -m.get("countIsPlaying", 0)),
    )

def api_session_dates(movie_id: str, city_id: int = CITY_ID):
    return fetch(
        f"{CONTENT_API}/sessions/city/{city_id}/event/{movie_id}/dates/partnership/{PARTNERSHIP}",
        cache_key=f"dates_{movie_id}_{city_id}",
        ttl=TTL["sessions"],
    )

def api_sessions(movie_id: str, date_str: str, city_id: int = CITY_ID):
    data = fetch(
        f"{CONTENT_API}/sessions/city/{city_id}/event/{movie_id}/partnership/{PARTNERSHIP}/groupBy/sessionType",
        params={"date": date_str},
        cache_key=f"sessions_{movie_id}_{city_id}_{date_str}",
        ttl=TTL["sessions"],
        schema="sessions",
    )
    return data[0] if data else None

def api_tickets(session_id: str, section_id: str):
    return fetch(
        f"{CHECKOUT_API}/sessions/{session_id}/sections/{section_id}/tickets",
        cache_key=f"tickets_{session_id}_{section_id}",
        ttl=TTL["tickets"],
        schema="tickets",
    )

def api_seats(session_id: str, section_id: str):
    return fetch(
        f"{CHECKOUT_API}/sessions/{session_id}/sections/{section_id}/seats",
        cache_key=f"seats_{session_id}_{section_id}",
        ttl=TTL["seats"],
        schema="seats",
    )

def api_theaters(city_id: int = CITY_ID):
    """All theaters for a city, sorted by name."""
    data = fetch(
        f"{CONTENT_API}/theaters/city/{city_id}",
        params={"partnership": PARTNERSHIP},
        cache_key=f"theaters_{city_id}",
        ttl=TTL["theaters"],
        schema="theaters",
    ) or []
    return sorted(data, key=lambda t: t.get("name", ""))

def api_sessions_by_theater(theater_id: str, date_str: str, city_id: int = CITY_ID):
    """Sessions at a specific theater on a given date, grouped by movie."""
    data = fetch(
        f"{CONTENT_API}/sessions/city/{city_id}/theater/{theater_id}/partnership/{PARTNERSHIP}/groupBy/sessionType",
        params={"date": date_str},
        cache_key=f"theater_sessions_{theater_id}_{city_id}_{date_str}",
        ttl=TTL["sessions"],
        schema="theater_sessions",
    )
    return data[0] if data else None


def api_movie_ids_in_city(city_id: int) -> set[str]:
    """IDs of movies with sessions in this city today or tomorrow.

    Queries all theaters in the city (capped at 30, sorted by room count) for
    today and tomorrow in parallel. Result cached for 15 minutes.
    """
    today     = date.today().isoformat()
    tomorrow  = (date.today() + timedelta(days=1)).isoformat()
    cache_key = f"movie_ids_city_{city_id}_{today}"

    cached = cache_get(cache_key)
    if cached is not None:
        return set(cached)

    theaters = api_theaters(city_id)
    # Largest theaters first; cap at 30 so big cities (SP ~441) stay fast
    theaters = sorted(theaters, key=lambda t: -len(t.get("rooms", [])))[:30]

    def _fetch(theater_id, date_str):
        day = api_sessions_by_theater(theater_id, date_str, city_id)
        return {str(m["id"]) for m in (day or {}).get("movies", [])}

    movie_ids: set[str] = set()
    calls = [(t["id"], d) for t in theaters for d in (today, tomorrow)]
    with ThreadPoolExecutor(max_workers=10) as pool:
        for ids in pool.map(lambda args: _fetch(*args), calls):
            movie_ids |= ids

    cache_set(cache_key, list(movie_ids), TTL["sessions"])
    return movie_ids


def api_movies_in_city(city_id: int = CITY_ID) -> list:
    """Movies actually showing in this city now or in the near future.

    Filters the national isPlaying catalog to movies with real sessions here:
    - Regular movies: must appear in theater sessions (today or tomorrow).
    - Pre-sale movies: checked via /dates — included if they have upcoming sessions.
    """
    movies    = api_movies(city_id)
    local_ids = api_movie_ids_in_city(city_id)

    playing  = [m for m in movies if str(m["id"]) in local_ids]
    presale  = [m for m in movies if m.get("inPreSale") and str(m["id"]) not in local_ids]

    if presale:
        def _has_dates(m):
            return m if api_session_dates(m["id"], city_id) else None

        with ThreadPoolExecutor(max_workers=5) as pool:
            for result in pool.map(_has_dates, presale):
                if result:
                    playing.append(result)

    # Restore original sort order (inPreSale to bottom, then countIsPlaying)
    order = {str(m["id"]): i for i, m in enumerate(movies)}
    playing.sort(key=lambda m: order.get(str(m["id"]), 9999))
    return playing


def api_states():
    """All Brazilian states and their cities."""
    data = fetch(
        f"{CONTENT_API}/states",
        params={"partnership": PARTNERSHIP},
        cache_key="states",
        ttl=TTL["cities"],
        schema="states",
    ) or []
    return data

# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    """Lowercase, strip accents, collapse extra spaces."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower())
        if unicodedata.category(c) != "Mn"
    ).strip()

def find_city(query: str) -> dict | None:
    """Find a city by partial name or UF — accent/case-insensitive.
    Returns city dict with keys: id, name, uf, state, urlKey.
    """
    states = api_states()
    all_cities = [c for s in states for c in s.get("cities", [])]
    q = normalize(query)

    # Exact match first (urlKey or full name)
    for c in all_cities:
        if c.get("urlKey") == q or normalize(c["name"]) == q:
            return c

    # Partial name match
    matches = [c for c in all_cities if q in normalize(c["name"])]
    return matches[0] if matches else None

def resolve_city(arg: str | None) -> tuple[int, str]:
    """Resolve --cidade argument to (city_id, city_name). Defaults to Fortaleza."""
    if not arg:
        return CITY_ID, "Fortaleza"
    city = find_city(arg)
    if not city:
        return None, None
    return int(city["id"]), city["name"]

def find_movie(query: str, movies: list) -> dict | None:
    """Match movie by ID, urlKey, or partial title — accent/case-insensitive."""
    q     = normalize(query)
    raw_q = query.strip().lower()

    for m in movies:
        if str(m["id"]) == query or m.get("urlKey", "").lower() == raw_q:
            return m

    matches = [m for m in movies if q in normalize(m["title"])]
    return matches[0] if matches else None

def resolve_date(arg: str | None) -> tuple[str, bool]:
    """
    Resolve a date argument to an ISO date string.
    Returns (date_str, was_explicit).

    Accepted values:
      None / ""      → today
      amanha/amanhã  → tomorrow
      +1, +2, +N     → N days from today
      YYYY-MM-DD     → literal date
    """
    today = date.today()
    if not arg:
        return today.isoformat(), False
    s = normalize(arg)
    if s == "amanha":
        return (today + timedelta(days=1)).isoformat(), True
    if s.startswith("+") and s[1:].isdigit():
        return (today + timedelta(days=int(s[1:]))).isoformat(), True
    return arg, True

def parse_tickets(data: dict) -> dict:
    """
    Extract useful ticket types from a tickets response.
    Returns dict with keys: inteira, meia (may be None), all.
    """
    tickets = data.get("default", []) if data else []
    real    = [t for t in tickets if t.get("total", 0) > 0]

    def find(keywords, exclude=None):
        for t in real:
            name = t["name"].lower()
            if any(k in name for k in keywords):
                if exclude and any(e in name for e in exclude):
                    continue
                return t
        return None

    return {
        "inteira": find(["inteira"]),
        "meia":    find(["meia"], exclude=["unique"]),
        "all":     real,
    }
