# Cinema App Design

## Problem

ingresso.com is slow. When you want to quickly check:
- What sessions are available for a movie?
- How full is the room going to be?
- Which theater has the cheapest tickets?
- Is there a less crowded session?

...the site/app makes you click through 5 pages and load a bunch of JS just to get that info.

## Solution: A fast, focused CLI or lightweight web app

---

## Proposed Features (v1)

### 1. `sessions` — Browse showtimes for a movie

```bash
cinema sessions "super mario" --city sao-paulo --date 2026-04-01
```

Output:
```
Super Mario Galaxy (2026-04-01 - quarta-feira)

Cine Araújo Campo Limpo · Santo Amaro
  16:10  Laser + Dublado     R$ 46,60 (inteira) / R$ 23,30 (meia)    ████████░░ 78% ocupado
  19:30  Laser + Dublado     R$ 46,60                                 ██░░░░░░░░ 22% ocupado
  21:45  IMAX + Dublado      R$ 52,00                                 ████░░░░░░ 45% ocupado

Cinemark Paulista
  14:00  Normal + Dublado    R$ 38,00                                 ░░░░░░░░░░  8% ocupado
  17:30  Dolby Atmos + Dub   R$ 55,00                                 ██████░░░░ 60% ocupado
```

### 2. `least-crowded` — Find the emptiest session

```bash
cinema least-crowded "super mario" --city sao-paulo --date 2026-04-01 --format IMAX
```

Ranks all sessions by occupancy %, shows the top 5 emptiest.

### 3. `prices` — Compare ticket prices across theaters

```bash
cinema prices "super mario" --city sao-paulo --date 2026-04-01
```

Shows a price table across theaters, including all ticket types (inteira, meia, promos).

### 4. `theater` — Show what's playing at a specific theater today

```bash
cinema theater "cine-araujo-campo-limpo"
```

### 5. `seats` — Show the seat map for a specific session

```bash
cinema seats 84283462 --section 4583484
```

ASCII seat map with available/occupied visualization.

---

## Architecture

### Tech Stack Options

**Option A: Python CLI** (fastest to build, most useful for personal use)
- `httpx` or `requests` for API calls
- `rich` for terminal output (tables, progress bars, colors)
- `typer` or `click` for CLI interface
- Optional: `diskcache` to cache responses (theaters don't change, sessions rarely change)

**Option B: Next.js / React webapp** (more shareable)
- Direct calls to `api-content.ingresso.com` (CORS is open)
- No backend needed for reads
- Can be deployed as static site

**Option C: FastAPI backend + thin frontend**
- Cache-heavy backend (Redis/diskcache)
- Useful if adding features like session monitoring/alerts

### Recommended: Start with Python CLI (Option A)

```
cinema/
├── cinema/
│   ├── __init__.py
│   ├── api/
│   │   ├── content.py      # api-content.ingresso.com client
│   │   └── checkout.py     # api.ingresso.com/v1 client
│   ├── commands/
│   │   ├── sessions.py
│   │   ├── prices.py
│   │   ├── seats.py
│   │   └── theater.py
│   ├── models.py           # Pydantic models for API responses
│   ├── cache.py            # Simple file/diskcache layer
│   └── display.py          # Rich formatters
├── main.py
├── pyproject.toml
└── README.md
```

---

## Key Design Decisions

### Occupancy calculation
The API gives us:
- `totalSeats` — room capacity
- Per-seat `status` field (`Available` / `Occupied` / `Reserved` etc.)

We can calculate `occupied / total * 100` = occupancy %.

We can also use `activeReservations` as a proxy for "currently being purchased".

### Caching strategy
- **Theater list**: Cache for 24h (rarely changes)
- **Movie list**: Cache for 1h
- **Session dates**: Cache for 30min
- **Sessions/showtimes**: Cache for 15min
- **Seat maps**: Cache for 2-5min (changes as people buy)
- **Ticket prices**: Cache for 1h

### City resolution
No `/cities` endpoint — build city list from `/theaters` response.
Store a `cities.json` with `{ urlKey, cityId, name, uf }` mapping.

Known cityIds:
- `1` = São Paulo (SP)
- `2` = Rio de Janeiro (RJ)
- `21` = Belo Horizonte (MG)
- `3` = Curitiba (PR)
- `58` = Londrina (PR)

---

## Interesting Data Points We Can Surface

| Insight | How |
|---|---|
| Occupancy % per session | `seats` endpoint, count Available vs total |
| Cheapest session for a movie today | Compare `price` across all sessions |
| Promotions available | Ticket names with "Meia" / "Promoção" |
| **Box office vs online price** | `price` = bilheteria, `service` = online fee, `total` = online price |
| **Whether to buy online or in person** | Show `service` fee and flag when it's disproportionate for Meia |
| Service fee always 14% of Inteira | Fee is flat per session — Meia buyers pay 28% effectively |
| Whether a room has seat selection | `hasSeatSelection` on session |
| Room has premium/recliner seats | `SuperSeat` type in seat map |
| Session accessibility | Seat types: `Disability` seats available |
| Upcoming premiere dates | `premiereDate` + `isComingSoon` flags |
| Pre-sale sessions | `inPreSale` flag |
| Theater chain promos (Santander Esfera, Claro Clube) | Appear as separate ticket types with discounted base price |

---

## Potential Issues

1. **The `sessions/city/{id}/...` endpoint returns 204 (No Content) for same-day or past dates** — only future dates work reliably. Confirmed: dates are returned starting from tomorrow for movies not showing today.

2. **The `events` endpoint without `cityId` returns 17,000+ items** — always filter.

3. **Seat map is only useful for sessions in the near future** — too far out and all seats show Available (expected).

4. **No rate limiting detected** but be reasonable — cache aggressively.

5. **`partnership=ingresso.com` param is required** on all content API calls. Other partnership keys exist but we only have `ingresso.com`.
