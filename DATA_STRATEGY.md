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

### Option A: Query-first cache ✅ **Implemented**

> Store nothing proactively. Cache API responses when the user actually queries something.

**How it works:**
1. User queries a movie → cache the session list for 15 min
2. User views a specific session → cache the seat map for 5 min
3. JSON files as the cache backend (one file per cache key, stored in `~/.cache/cinema-fortaleza/`)

**Pros:** Zero storage waste, zero background requests, works offline for repeated queries, shared between CLI and web API
**Cons:** No historical data, no patterns over time

```
~/.cache/cinema-fortaleza/
├── movies_<city_id>.json                       (TTL: 1h)
├── theaters_<city_id>.json                     (TTL: 1h)
├── theater_sessions_<tid>_<city>_<date>.json   (TTL: 15min)
├── movie_ids_city_<city_id>_<date>.json        (TTL: 15min)
├── sessions_<mid>_<city>_<date>.json           (TTL: 15min)
├── tickets_<sid>_<secid>.json                  (TTL: 1h)
├── seats_<sid>_<secid>.json                    (TTL: 5min)
├── states.json                                 (TTL: 24h)
└── schema_warnings.log                         (append-only, written when API format changes)
```

---

### Option B: Track queried sessions

> Like Option A, but when a user views a session, start tracking its occupancy in the background.

When a user queries session `84283462` → save first snapshot → background job polls every 30 min → user sees occupancy timeline.

**Storage cost (Fortaleza):** 20 sessions/day × 12 snapshots × 365 days ≈ **9 MB/year** in SQLite. Trivial.

---

### Option C: Proactive daily scrape

> Every day at midnight, fetch all sessions for Fortaleza and track occupancy on a schedule.

~6,000 req/day = 4 req/min. Storage: ~170 MB/year. Feasible for a single city; for national scale (~48,000 sessions/day) you'd need a proper job queue and Postgres.

---

## With historical data (Options B/C) you could add

- Occupancy prediction based on past sessions at the same time/day
- "Best time to buy" patterns (blockbusters fill fast in the first hours)
- Price change detection
- Weekly occupancy heatmap per theater

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
