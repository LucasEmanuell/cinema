# Ingresso.com API Research

## Overview

Ingresso.com exposes two separate API bases — **content** (public, no auth) and **checkout** (auth required for purchases):

| Base URL | Purpose | Auth |
|---|---|---|
| `https://api-content.ingresso.com` | Movies, theaters, sessions listing | None (public) |
| `https://api.ingresso.com/v1/` | Seat maps, ticket prices | None for reads |
| `https://api-auth.ingresso.com/v2/` | Login / user account | JWT |

All content requests require `?partnership=ingresso.com` (or another valid partner key).

---

## Confirmed Endpoints

### 1. Theaters

```
GET https://api-content.ingresso.com/v0/theaters?partnership=ingresso.com
GET https://api-content.ingresso.com/v0/theaters?partnership=ingresso.com&cityId={cityId}
GET https://api-content.ingresso.com/v0/theaters/url-key/{urlKey}/partnership/ingresso.com
```

**Response fields per theater:**
- `id`, `name`, `urlKey`, `boxOfficeId`
- `address`, `neighborhood`, `cityId`, `cityName`, `uf`, `state`
- `geolocation` → `{ lat, lng }`
- `corporation`, `corporationId`
- `rooms[]` → `{ id, name, fullName, capacity }`
- `totalRooms`
- `properties` → `{ hasBomboniere, hasContactlessWithdrawal, hasSession, hasSeatDistancePolicy }`
- `deliveryType[]` → e.g. `["Bilheteria/ATM", "Aplicativo/Scannerless"]`
- `enabled`, `blockMessage`

**Note:** There are ~441 theaters in SP alone. No separate `/cities` endpoint — extract cities from `/theaters`.

---

### 2. Movies (Events)

```
GET https://api-content.ingresso.com/v0/events?partnership=ingresso.com&cityId={cityId}&isPlaying=true
GET https://api-content.ingresso.com/v0/events/{eventId}?partnership=ingresso.com
GET https://api-content.ingresso.com/v0/events/url-key/{urlKey}/partnership/ingresso.com
GET https://api-content.ingresso.com/v0/events/url-key/{urlKey}/partnership/ingresso.com?includeCities=true
```

**Response fields per movie:**
- `id`, `title`, `originalTitle`, `urlKey`
- `type` (e.g. "Filme")
- `contentRating` (e.g. "14 anos")
- `duration` (minutes as string)
- `synopsis`, `cast`, `director`
- `genres[]`
- `rating`, `ratingDetails` → `{ id, name, label, color }`
- `isPlaying`, `countIsPlaying` (number of theaters playing it)
- `inPreSale`, `isComingSoon`, `isReexhibition`
- `premiereDate`
- `images[]` → `{ url, type }` (types: `PosterPortrait`, `PosterHorizontal`)
- `trailers[]`
- `cities[]` (only with `includeCities=true`) → `{ id, name, uf, state, urlKey, timeZone }`

**Warning:** `/events` without filters returns ~17,000 items. Always filter by `cityId` and `isPlaying=true`.

---

### 3. Session Dates

```
GET https://api-content.ingresso.com/v0/sessions/city/{cityId}/event/{eventId}/dates/partnership/ingresso.com
GET https://api-content.ingresso.com/v0/sessions/city/{cityId}/theater/{theaterId}/dates/partnership/ingresso.com
```

**Response:** Array of available dates:
```json
[
  {
    "date": "2026-04-01",
    "dateFormatted": "01/04",
    "dayOfWeek": "quarta-feira",
    "isToday": false,
    "sessionTypes": ["Normal", "Dublado", "Laser", "IMAX", "4DX", "Dolby Atmos", ...]
  }
]
```

---

### 4. Sessions (Showtimes)

**By movie** (returns theaters grouped under the movie):
```
GET https://api-content.ingresso.com/v0/sessions/city/{cityId}/event/{eventId}/partnership/ingresso.com/groupBy/sessionType?date=YYYY-MM-DD
```

**By theater** (returns movies grouped under the theater):
```
GET https://api-content.ingresso.com/v0/sessions/city/{cityId}/theater/{theaterId}/partnership/ingresso.com/groupBy/sessionType?date=YYYY-MM-DD
```

**Response structure (by movie):**
```json
[{
  "date": "2026-04-01",
  "theaters": [{
    "id": "413",
    "name": "Cine Araújo Campo Limpo",
    "address": "...",
    "neighborhood": "Santo Amaro",
    "enabled": true,
    "sessionTypes": [{
      "type": ["Laser", "Dublado"],
      "sessions": [{
        "id": "84283462",
        "price": 46.6,
        "room": "Sala 3 - LASER",
        "time": "16:10",
        "date": { "localDate": "2026-04-01T16:10:00-03:00", "hour": "16:10", ... },
        "defaultSector": "4583484",   ← sectionId for seat map
        "hasSeatSelection": true,
        "enabled": true,
        "blockMessage": "",
        "driveIn": false,
        "streaming": false,
        "siteURL": "https://checkout.ingresso.com/?sessionId=84283462&..."
      }]
    }]
  }]
}]
```

**Response structure (by theater):** Same but with `movies[]` instead of `theaters[]`.

---

### 5. Ticket Prices

```
GET https://api.ingresso.com/v1/sessions/{sessionId}/sections/{sectionId}/tickets
```

`sectionId` = `defaultSector` from the session object.

**Response:**
```json
{
  "default": [
    {
      "id": "1",
      "name": "Inteira",
      "price": 44.0,
      "service": 6.16,
      "tax": 0.0,
      "total": 50.16,
      "maxQuantity": 8,
      "help": "",
      "highlight": false
    },
    {
      "id": "2",
      "name": "Meia",
      "price": 22.0,
      "service": 6.16,
      "tax": 0.0,
      "total": 28.16,
      "help": "Válido para crianças menores de 12 anos, estudantes..."
    }
  ]
}
```

**Key fields:**
- `price` — box office price (what you'd pay in person, **no fee**)
- `service` — online service fee (ingresso.com surcharge)
- `tax` — always 0 in practice
- `total` — what you pay online (`price + service`)
- `help` — eligibility rules (shown for Meia-Entrada, promos)
- `maxQuantity` — max tickets per purchase

### ⚠️ Critical pricing quirk: Meia-Entrada fee

The service fee is a **flat amount per transaction** tied to the Inteira price, NOT a percentage of the actual ticket:

| Ticket | Box office | Online fee | Online total | Fee as % of ticket |
|---|---|---|---|---|
| Inteira | R$ 44.00 | R$ 6.16 | R$ 50.16 | **14%** |
| Meia-Entrada | R$ 22.00 | R$ 6.16 | R$ 28.16 | **28%** |
| 50% Esfera Santander | R$ 22.00 | R$ 6.16 | R$ 28.16 | **28%** |
| 25% Esfera Santander | R$ 33.00 | R$ 6.16 | R$ 39.16 | **19%** |

The fee for Meia is the **same absolute value** as Inteira (14% of Inteira base), so Meia buyers pay proportionally double the fee. Buying at the box office saves the full R$ 6+ for them.

**Exception:** UCI's own "UNIQUE MEIA ENTRADA" tickets have a proportional 14% fee because they go through UCI's own pricing tier — they're not standard Meia.

### session.price vs tickets endpoint

The `price` field on a session object (from `/v0/sessions/...`) shows the **highest priced seat in the section** — which may be SuperSeat/recliner price when the room has premium seats. Always use the tickets endpoint for accurate per-type pricing.

Example from UCI Iguatemi (has SuperSeat recliners):
- `session.price = 54.72` → SuperSeat Inteira (R$ 48 + R$ 6.72)
- Tickets endpoint Inteira: R$ 44.00 + R$ 6.16 = R$ 50.16 (regular seats)
```

---

### 6. Seat Map (Occupancy)

```
GET https://api.ingresso.com/v1/sessions/{sessionId}/sections/{sectionId}/seats
```

**Response fields:**
- `totalSeats` — total capacity of the room
- `activeReservations` — seats currently in cart/pending
- `theaterName`, `theaterLocationName` (room name)
- `lines[]` → `{ line, seats[] }`
  - Each `seat`: `{ id, line, column, label, type, status, typeDescription }`
  - `type`: `"Regular"`, `"Obese"`, `"Disability"`
  - `status`: `"Available"`, `"Occupied"` (etc.)
- `boxOfficeSeatTypes[]` — seat category definitions
- `socialDistance`, `diagonalBlock`, etc. — COVID-era flags (mostly false now)

**Occupancy calculation:**
```python
total = data["totalSeats"]
available = sum(1 for line in data["lines"] for seat in line["seats"] if seat["status"] == "Available")
occupied = total - available
occupancy_pct = (occupied / total) * 100
```

---

## Data Flow for App

```
1. List cities → GET /v0/theaters → extract unique cityId/cityName/uf
2. Pick city → GET /v0/events?cityId=X&isPlaying=true → movie list
3. Pick movie → GET /v0/sessions/city/X/event/Y/dates/partnership/ingresso.com → available dates
4. Pick date → GET /v0/sessions/city/X/event/Y/partnership/ingresso.com/groupBy/sessionType?date=D → sessions by theater
5. Pick session → GET /v1/sessions/{id}/sections/{sectionId}/seats → seat map + occupancy
6. Get prices → GET /v1/sessions/{id}/sections/{sectionId}/tickets → ticket types + prices
```

---

## Session Types Available

From the API (real data from São Paulo):
`Normal`, `Dublado`, `Legendado`, `3D`, `IMAX`, `4DX`, `Laser`, `D-Box`, `XD`, `CINEPIC`, `Macro XE`, `XPLUS`, `Dolby Atmos`, `Vip`

---

## Rate Limits / Auth

- Content API (`api-content.ingresso.com`): Public, no API key needed beyond `?partnership=ingresso.com`
- Checkout API (`api.ingresso.com/v1/`): Seat reads appear public; purchases require JWT auth via `Authorization: Bearer <token>`
- Auth API uses: `X-User` header (userId) and `Authorization` header (JWT Bearer)
- Swagger UI at `https://api-content.ingresso.com/swagger/index.html` exists but has **empty paths** — not useful

---

## App Ideas & What's Possible

### Core use cases (100% possible with this API):

1. **Session browser** — browse all sessions for a movie in your city, filtered by format (IMAX, Laser, etc.) and time
2. **Price comparison** — compare ticket prices across theaters for the same movie (full + promo prices)
3. **Seat availability heatmap** — show how full each session is before deciding which to attend
4. **"Find least crowded session"** — rank sessions by occupancy % to avoid packed rooms
5. **Format finder** — find which theaters/sessions have a specific format (IMAX, 4DX, Dolby Atmos)
6. **Theater browser** — browse all theaters in a city with their rooms and active sessions
7. **Session alerts** — monitor a specific session and alert when seats start filling up

### What the API **does NOT expose** directly:
- Historical pricing data
- Reviews or ratings (only an aggregate `rating` float)
- Parking/accessibility info beyond what's in theater properties
- Concessions / food menu
- Loyalty points / membership benefits

---

## Key Identifiers

| Thing | ID type | Example |
|---|---|---|
| City | `cityId` (int) | `1` = São Paulo, `36` = Fortaleza |
| Theater | `id` (string) | `"413"` |
| Theater URL | `urlKey` | `"cine-araujo-campo-limpo"` |
| Movie | `id` (string) | `"30773"` |
| Movie URL | `urlKey` | `"super-mario-galaxy-o-filme"` |
| Session | `id` (string) | `"84283462"` |
| Section/Room | `defaultSector` (string) | `"4583484"` |

## Known City IDs

- `1` = São Paulo (SP)
- `2` = Rio de Janeiro (RJ)
- `3` = Curitiba (PR)
- `21` = Belo Horizonte (MG)
- `36` = Fortaleza (CE) ← primary target
- `58` = Londrina (PR)

## Fortaleza (cityId=36) — Primary Target

- **10 theaters**, ~62 rooms total
- **~20 movies/day** with active sessions
- **~400 sessions/day** total
- **Theaters:**
  - `1416` — Centerplex Messejana (5 rooms)
  - `1191` — Centerplex Via Sul (6 rooms)
  - `1117` — Cinema do Dragão (2 rooms)
  - `1329` — Cinemas Benfica (4 rooms)
  - `1175` — Cinépolis Jóquei Fortaleza (5 rooms)
  - `1256` — Cinépolis Rio Mar (10 rooms)
  - `1446` — Cinépolis RioMar Kennedy (6 rooms)
  - `388` — Kinoplex North Shopping (6 rooms)
  - `308` — UCI Kinoplex Iguatemi Fortaleza (12 rooms)
  - `1206` — UCI Parangaba (6 rooms)
