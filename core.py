"""
core.py — Data layer: cache, ingresso.com API calls, pure helpers.

No I/O, no display, no sys.exit — raises APIError on failure so both
the CLI and the web API can handle errors their own way.
"""
import json
import os
import threading
import time
import unicodedata
from contextlib import contextmanager

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor
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
    "seats":       45,  # 45s — occupancy changes in real time
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
#
# Two layers: an in-process dict (no disk or JSON parsing on a hit) backed by
# JSON files so the CLI and server restarts share results.
#
# Entries past their TTL can still be served for `stale` more seconds while a
# background thread refreshes them (stale-while-revalidate), and are also the
# fallback when ingresso.com is down. Cached objects are shared — callers must
# not mutate them.

NEG_TTL = 120  # how long an empty (204/404) answer is remembered

_MEM: dict[tuple[str, str], dict] = {}
_locks: dict[str, list] = {}
_locks_guard = threading.Lock()
_refreshing: set[str] = set()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"

def _cache_entry(key: str) -> dict | None:
    mem_key = (str(CACHE_DIR), key)
    obj = _MEM.get(mem_key)
    if obj is None:
        try:
            obj = json.loads(_cache_path(key).read_text())
            obj["ts"], obj["ttl"], obj["v"]
        except Exception:
            return None
        _MEM[mem_key] = obj
    return obj

def cache_get(key: str):
    obj = _cache_entry(key)
    if obj and time.time() - obj["ts"] < obj["ttl"]:
        return obj["v"]
    return None

def cache_set(key: str, value, ttl: int):
    obj = {"ts": time.time(), "ttl": ttl, "v": value}
    _MEM[(str(CACHE_DIR), key)] = obj
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path(f"{key}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(obj))
        os.replace(tmp, _cache_path(key))   # atomic: readers never see a half-written file
    except OSError:
        pass  # disk cache is best-effort; the in-memory copy still works

@contextmanager
def _key_lock(key: str):
    """Per-key lock so concurrent requests for a cold key share one fetch."""
    with _locks_guard:
        ent = _locks.setdefault(key, [threading.Lock(), 0])
        ent[1] += 1
    try:
        with ent[0]:
            yield
    finally:
        with _locks_guard:
            ent[1] -= 1
            if ent[1] == 0:
                _locks.pop(key, None)

def _refresh_async(key: str, ttl: int, loader) -> None:
    with _locks_guard:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run():
        try:
            cached(key, ttl, loader, force=True)
        except Exception:
            pass  # stale value keeps being served; next request retries
        finally:
            with _locks_guard:
                _refreshing.discard(key)

    threading.Thread(target=run, daemon=True).start()

def cached(key: str, ttl: int, loader, stale: int = 0, force: bool = False):
    """
    Return the cached value for `key`, calling `loader()` on a miss.

    stale: seconds past TTL during which the old value is returned instantly
           while a background refresh runs (and used if the loader fails).
    force: skip the cache read (explicit refresh).
    """
    def lookup():
        obj = _cache_entry(key)
        if not obj:
            return None, False
        age = time.time() - obj["ts"]
        if age < obj["ttl"]:
            return obj, True
        return (obj, False) if age < obj["ttl"] + stale else (None, False)

    if not force:
        obj, fresh = lookup()
        if obj and fresh:
            return obj["v"]
        if obj:
            _refresh_async(key, ttl, loader)
            return obj["v"]

    with _key_lock(key):
        if not force:
            obj, fresh = lookup()
            if obj and fresh:
                return obj["v"]
        try:
            value = loader()
        except APIError:
            obj, _ = lookup()
            if obj:
                return obj["v"]
            raise
        cache_set(key, value, ttl if value is not None else NEG_TTL)
        return value

# ── HTTP ──────────────────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers["User-Agent"] = "cinema-fortaleza/1.0"
_adapter = HTTPAdapter(
    pool_connections=4, pool_maxsize=32,
    max_retries=Retry(total=2, backoff_factor=0.3, status_forcelist=(429, 502, 503, 504),
                      allowed_methods=("GET",), raise_on_status=False),
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


def _http_get(url: str, params: dict | None, schema: str | None):
    try:
        r = _session.get(url, params=params, timeout=10)
    except requests.RequestException as e:
        raise APIError(str(e))

    if r.status_code in (204, 404):
        return None                    # legitimately "nothing here"
    if not r.ok:
        raise APIError(f"HTTP {r.status_code} em {url}")   # transient: don't pass as "empty"
    try:
        data = r.json()
    except ValueError as e:
        raise APIError(f"Resposta inválida de {url}: {e}")
    if schema:
        check_schema(data, schema)
    return data

def fetch(url: str, params: dict = None, cache_key: str = None, ttl: int = 300,
          schema: str = None, stale: int = 0, force: bool = False):
    """
    Fetch a URL with optional caching (see `cached`).

    Returns None for 204/404; raises APIError for network and 5xx/429 failures.
    schema: live responses are validated against _SCHEMAS[schema]; warnings are
            logged to ~/.cache/cinema-fortaleza/schema_warnings.log.
    """
    def load():
        return _http_get(url, params, schema)

    if not cache_key:
        return load()
    return cached(cache_key, ttl, load, stale=stale, force=force)

# ── API calls ─────────────────────────────────────────────────────────────────

def api_movies(city_id: int = CITY_ID):
    data = fetch(
        f"{CONTENT_API}/events",
        params={"partnership": PARTNERSHIP, "cityId": city_id, "isPlaying": "true"},
        cache_key=f"movies_{city_id}",
        ttl=TTL["movies"],
        stale=86400,
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
        stale=3600,
    )

def api_sessions(movie_id: str, date_str: str, city_id: int = CITY_ID):
    data = fetch(
        f"{CONTENT_API}/sessions/city/{city_id}/event/{movie_id}/partnership/{PARTNERSHIP}/groupBy/sessionType",
        params={"date": date_str},
        cache_key=f"sessions_{movie_id}_{city_id}_{date_str}",
        ttl=TTL["sessions"],
        stale=3600,
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

def api_seats(session_id: str, section_id: str, force: bool = False):
    return fetch(
        f"{CHECKOUT_API}/sessions/{session_id}/sections/{section_id}/seats",
        cache_key=f"seats_{session_id}_{section_id}",
        ttl=TTL["seats"],
        schema="seats",
        force=force,
    )

def api_theaters(city_id: int = CITY_ID):
    """All theaters for a city, sorted by name."""
    data = fetch(
        f"{CONTENT_API}/theaters/city/{city_id}",
        params={"partnership": PARTNERSHIP},
        cache_key=f"theaters_{city_id}",
        ttl=TTL["theaters"],
        stale=86400,
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
        stale=3600,
        schema="theater_sessions",
    )
    return data[0] if data else None


SCAN_THEATERS = 40   # theaters sampled per city (largest first)


def _scan_city(city_id: int) -> dict:
    today    = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    theaters = api_theaters(city_id)
    # Largest theaters first; cap so big cities (SP ~441) stay fast
    sample = sorted(theaters, key=lambda t: -len(t.get("rooms") or []))[:SCAN_THEATERS]

    def _fetch(args):
        day = api_sessions_by_theater(args[0], args[1], city_id)
        return {str(m["id"]) for m in (day or {}).get("movies", [])}

    movie_ids: set[str] = set()
    calls = [(t["id"], d) for t in sample for d in (today, tomorrow)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        for ids in pool.map(_fetch, calls):
            movie_ids |= ids

    return {"ids": sorted(movie_ids), "truncated": len(theaters) > SCAN_THEATERS}


def api_city_scan(city_id: int) -> dict:
    """{"ids": movie ids with sessions today/tomorrow, "truncated": bool}.

    `truncated` means the city has more theaters than we sampled, so absence
    from `ids` doesn't prove a movie isn't showing there.
    """
    return cached(
        f"city_scan_{city_id}", TTL["sessions"],
        lambda: _scan_city(city_id), stale=6 * 3600,
    )


def api_movie_ids_in_city(city_id: int) -> set[str]:
    """IDs of movies with sessions in this city today or tomorrow."""
    return set(api_city_scan(city_id)["ids"])


def api_movies_in_city(city_id: int = CITY_ID) -> list:
    """Movies actually showing in this city now or in the near future.

    Filters the national isPlaying catalog to movies with real sessions here:
    - Movies found in the theater scan (today or tomorrow) are included.
    - Pre-sale movies, and — when the scan only sampled part of a big city —
      every movie not found by it, are checked via /dates and included if they
      have upcoming sessions.
    """
    movies = api_movies(city_id)
    scan   = api_city_scan(city_id)
    local_ids = set(scan["ids"])

    playing = [m for m in movies if str(m["id"]) in local_ids]
    check   = [m for m in movies if str(m["id"]) not in local_ids
               and (scan["truncated"] or m.get("inPreSale"))]

    if check:
        def _has_dates(m):
            return m if api_session_dates(m["id"], city_id) else None

        with ThreadPoolExecutor(max_workers=8) as pool:
            playing.extend(m for m in pool.map(_has_dates, check) if m)

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
        stale=7 * 86400,
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
