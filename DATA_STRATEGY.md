# Data Strategy: What to Gather & Whether to Store It

## What the API exposes (complete picture)

### Tier 1 — Static-ish data (changes rarely)
| Data | Endpoint | Update freq | Size |
|---|---|---|---|
| Theater list + rooms | `/v0/theaters` | Weekly | ~1.2 MB for all Brazil |
| Movie metadata | `/v0/events/{id}` | When new movie added | ~3 KB/movie |
| Cities where movie plays | `/v0/events/url-key/{key}?includeCities=true` | Daily | ~30 KB/movie |
| Session types available per date | `/v0/sessions/city/{id}/event/{id}/dates/...` | Daily | ~3 KB/movie |

### Tier 2 — Daily data (changes once a day when new sessions open)
| Data | Endpoint | Update freq | Size (Fortaleza) |
|---|---|---|---|
| Session schedule (times, rooms, prices) | `/v0/sessions/city/{id}/event/{id}/...?date=D` | Daily | ~50 KB/movie/day |
| All sessions for a theater | `/v0/sessions/city/{id}/theater/{id}/...?date=D` | Daily | ~50 KB/theater/day |

### Tier 3 — Live data (changes in real-time)
| Data | Endpoint | Granularity | Size/request |
|---|---|---|---|
| Seat map + occupancy | `/v1/sessions/{id}/sections/{sectionId}/seats` | Real-time | ~61 KB |
| Ticket prices | `/v1/sessions/{id}/sections/{sectionId}/tickets` | Rarely changes | ~1.4 KB |
| Active reservations count | Embedded in seat map response | Real-time | (above) |

---

## Scale Numbers (Fortaleza, cityId=36)

From real sampling on 2026-04-01:

- **10 theaters**, ~62 total rooms
- **~20 movies playing** on any given day with Fortaleza sessions
- **~400 sessions/day** total (avg 20 sessions/movie × 20 movies)
- **~4,800 req/day** for hourly occupancy tracking = **3.4 req/min** ← totally fine
- **~24 MB** of seat map data per full daily snapshot

For comparison, **national scale** would be ~48,000 sessions/day → 400 req/min for hourly tracking. Feasible but you'd want a proper scheduler.

---

## Is it worth storing? An honest assessment

### What you CAN'T get without historical storage:
- "How fast does this session sell out?" (need time-series snapshots)
- "Is Saturday 8pm always packed at Cinépolis?" (need past data)
- "Do prices drop day-of for empty sessions?" (need price history)
- "When do sessions go on sale?" (need to detect first appearance)
- "Which theaters have the most promotions?" (need historical tickets data)

### What you CAN get on-demand (no storage needed):
- Current availability of any session right now
- Today's sessions and prices for any movie
- Which theater is cheapest right now
- How full is a specific session at this moment

### Verdict by data type:

| Data | Store it? | Reason |
|---|---|---|
| Theater/room metadata | ✅ Cache in SQLite | 1,000 rows, almost never changes, saves N requests |
| Movie metadata | ✅ Cache in SQLite | ~200 rows per city, refresh daily |
| Session schedule (times/prices) | ✅ Cache daily | ~400 rows/day for Fortaleza, saves repeated fetches |
| **Occupancy snapshots (hourly)** | ⚠️ Maybe, for queried sessions only | Full coverage is 170 MB/year for Fortaleza only. Useful for patterns but heavy |
| Ticket prices | ✅ Log changes only | Very lightweight, interesting if prices fluctuate |
| All seat maps for all sessions | ❌ Don't bother | 24 MB/day just for Fortaleza, limited utility unless doing ML |

---

## Recommended Storage Strategy

### Option A: Query-first cache (recommended for v1)

> Store nothing proactively. Cache API responses when the user actually queries something.

**How it works:**
1. User queries a movie → cache the session list for 15 min
2. User views a specific session → cache the seat map for 5 min
3. SQLite as the cache backend (simple key-value with TTL)

**Pros:** Zero storage waste, zero background requests, works offline for repeated queries
**Cons:** No historical data, no patterns over time

```
cache/
├── theaters.db       (SQLite, TTL: 7 days)
├── sessions.db       (SQLite, TTL: 15 min)
└── seats.db          (SQLite, TTL: 5 min)
```

---

### Option B: Track queried sessions (recommended for v1.5)

> Like Option A, but when a user views a session, start tracking its occupancy in the background.

**How it works:**
1. User queries session 84283462 → save first occupancy snapshot
2. Background job runs every 30 min → re-fetch and store new snapshot for all "watched" sessions
3. User can see occupancy timeline: "this session was 40% full at 9am, 80% at 6pm"

**Storage cost for Fortaleza:** If user "watches" 20 sessions/day × 12 snapshots × 365 days = 87,600 rows/year ≈ **9 MB/year** in SQLite. Trivial.

**Schema:**
```sql
CREATE TABLE occupancy_snapshots (
    session_id     TEXT NOT NULL,
    section_id     TEXT NOT NULL,
    fetched_at     DATETIME NOT NULL,
    total_seats    INTEGER,
    available      INTEGER,
    occupied       INTEGER,
    pct_full       REAL,
    PRIMARY KEY (session_id, section_id, fetched_at)
);

CREATE TABLE price_history (
    session_id     TEXT NOT NULL,
    section_id     TEXT NOT NULL,
    ticket_name    TEXT NOT NULL,
    price          REAL,
    service        REAL,
    total          REAL,
    observed_at    DATETIME NOT NULL,
    PRIMARY KEY (session_id, section_id, ticket_name, observed_at)
);
```

---

### Option C: Proactive daily scrape (for v2 / national scale)

> Every day at midnight, fetch all sessions for Fortaleza (or nationally) and store occupancy snapshots on a schedule.

**For Fortaleza only:**
- Midnight: fetch all ~400 sessions for tomorrow
- Every hour 10am–midnight: fetch seat maps for active sessions
- Total: ~400 (schedule) + 400×14 (hourly) = 6,000 req/day = 4 req/min
- Storage: ~170 MB/year in SQLite (very manageable)

**For national:**
- ~48,000 sessions/day nationally
- Hourly tracking = ~400 req/min → possible but you're hitting their API hard
- Storage: ~20 GB/year → need Postgres or TimescaleDB
- **Probably not worth it unless you have a specific analytical goal**

---

## Interesting Things You Can Do With Historical Data

If you go with Option B or C, here's what becomes possible:

### 1. Occupancy prediction
"Based on past 4 Saturdays, Interstellar 8pm at Cinépolis RioMar will be ~85% full by 6pm"

### 2. Best time to buy
"Sessions for blockbusters fill 60% in the first 2 hours of sale. Buy early."

### 3. Price change detection
"Centerplex Messejana dropped the Dolby Atmos session to R$ 35 on the day of showing (it was R$ 47)"

### 4. Theater patterns
"UCI Iguatemi sells out IMAX sessions 3 days before showtime on average"

### 5. Weekly occupancy heatmap
"Friday 9pm is always 90%+ full. Tuesday afternoon is almost never above 30%"

---

## Recommendation for Your Case

**Start with Option A** (query cache only):
- Zero maintenance overhead
- Fast responses for repeated queries
- No background tasks, no scheduler

**Move to Option B** (track queried sessions) after first version:
- Add a SQLite `occupancy_snapshots` table
- When user queries a session, spawn a background task to poll it every 30 min until showtime
- ~0 cost in storage and requests for personal use

**Fortaleza first, scalable later** — the architecture is the same either way:
- `cityId` is just a parameter
- Adding more cities = changing a config value
- If going national, swap SQLite for Postgres + add a proper job queue (e.g. `rq` or `celery`)

---

## Rate Limiting Notes

No rate limits detected on either API during testing. But:
- Be a good citizen: always cache, never poll faster than every 5 min
- The seat map endpoint (`api.ingresso.com/v1/`) is the heaviest at ~61 KB/request
- A `User-Agent` header identifying your app is good practice
- If they add rate limiting, the `partnership=ingresso.com` key is public — you may need a fallback or delay

---

## Summary Table

| Scenario | Req/day | Storage/year | Complexity |
|---|---|---|---|
| On-demand only (no storage) | ~50–200 | 0 | Low |
| Query cache (Option A) | ~50–200 | <10 MB | Low |
| Track queried sessions (Option B) | ~1,000–5,000 | <50 MB | Medium |
| Full Fortaleza daily scrape (Option C) | ~6,000 | ~170 MB | Medium |
| National daily scrape | ~580,000 | ~20 GB/year | High |

**For a personal project: Option A → B is the sweet spot.**
Fast, cheap, no server needed, and you get occupancy history for things you actually care about.
