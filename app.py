"""
app.py — FastAPI web API for cinema-fortaleza.

Run:
  uvicorn app:app --reload

Endpoints:
  GET /filmes
  GET /sessoes/{filme}?data=&teatro=&hora=
  GET /sessoes/{filme}/datas
  GET /tickets/{session_id}/{section_id}
  GET /assentos/{session_id}/{section_id}?atualizar=1
  GET /ocupacao?s={session_id}:{section_id}&s=…
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

_STATIC = Path(__file__).parent / "static"

from core import (
    APIError, CITY_ID,
    api_movies, api_movies_in_city, api_session_dates, api_sessions,
    api_tickets, api_seats, api_states, find_movie, normalize,
    resolve_date, resolve_city, parse_tickets,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the city-movie cache for the default city so the first web request is fast.
    threading.Thread(target=api_movies_in_city, args=(CITY_ID,), daemon=True).start()
    yield


app = FastAPI(
    title="Cinema Fortaleza API",
    description="Sessões, preços e mapas de assentos dos cinemas de Fortaleza via ingresso.com",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# Browser cache lifetimes per route prefix (seconds). Short on purpose: the
# server cache is the source of truth, this just absorbs repeat clicks.
_CACHE_CONTROL = {
    "/cidades":  "public, max-age=3600",
    "/filmes":   "public, max-age=60, stale-while-revalidate=300",
    "/sessoes":  "public, max-age=60, stale-while-revalidate=300",
    "/tickets":  "public, max-age=300",
    "/assentos": "public, max-age=20",
    "/ocupacao": "public, max-age=20",
    "/static":   "public, max-age=31536000, immutable",
}


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.method == "GET" and response.status_code == 200 \
            and "cache-control" not in response.headers:
        path = request.url.path
        for prefix, value in _CACHE_CONTROL.items():
            if path.startswith(prefix):
                response.headers["Cache-Control"] = value
                break
    return response


def _api_error(e: APIError):
    raise HTTPException(status_code=503, detail=f"Erro ao contactar ingresso.com: {e}")

def _resolve_city(cidade: str | None) -> tuple[int, str]:
    city_id, city_name = resolve_city(cidade)
    if city_id is None:
        raise HTTPException(status_code=404, detail=f"Cidade não encontrada: {cidade}")
    return city_id, city_name


# ── Routes ────────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/", include_in_schema=False)
def serve_frontend():
    """Serve the Vue SPA."""
    return FileResponse(_STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/cidades", summary="Lista todas as cidades disponíveis")
def get_cidades():
    """Retorna todos os estados e cidades com sessões disponíveis na ingresso.com."""
    try:
        return api_states()
    except APIError as e:
        _api_error(e)


@app.get("/filmes", summary="Filmes em cartaz")
def get_filmes(cidade: str | None = Query(None, description="Cidade (padrão: Fortaleza)")):
    """
    Lista os filmes em cartaz na cidade indicada, ordenados por número de salas.
    """
    try:
        city_id, _ = _resolve_city(cidade)
        movies = api_movies_in_city(city_id)
    except HTTPException:
        raise
    except APIError as e:
        _api_error(e)

    return [
        {
            "id":            m["id"],
            "title":         m["title"],
            "urlKey":        m.get("urlKey"),
            "contentRating": m.get("contentRating"),
            "duration":      m.get("duration"),
            "synopsis":      m.get("synopsis"),
            "posterUrl":     next((img.get("url") for img in m.get("images") or []), None),
            "premiereDate":  m.get("premiereDate"),
            "countPlaying":  m.get("countIsPlaying", 0),
            "inPreSale":     m.get("inPreSale", False),
        }
        for m in movies
    ]


@app.get("/sessoes/{filme}/datas", summary="Datas disponíveis para um filme")
def get_datas(
    filme:  str,
    cidade: str | None = Query(None, description="Cidade (padrão: Fortaleza)"),
):
    """
    Retorna as datas em que o filme tem sessões na cidade indicada.
    `filme` pode ser título parcial, sem acento, ou o ID numérico.
    """
    try:
        city_id, _ = _resolve_city(cidade)
        movies = api_movies(city_id)
        movie  = find_movie(filme, movies)
        if not movie:
            raise HTTPException(status_code=404, detail=f"Filme não encontrado: {filme}")

        dates = api_session_dates(movie["id"], city_id) or []
    except HTTPException:
        raise
    except APIError as e:
        _api_error(e)

    return {"movie": {"id": movie["id"], "title": movie["title"]}, "dates": dates}


@app.get("/sessoes/{filme}", summary="Sessões de um filme")
def get_sessoes(
    filme:  str,
    cidade: str | None = Query(None, description="Cidade (padrão: Fortaleza)"),
    data:   str | None = Query(None, description="YYYY-MM-DD, 'amanha', '+1', '+2'…"),
    teatro: str | None = Query(None, description="Filtro parcial por nome do cinema"),
    hora:   str | None = Query(None, description="Filtro por horário, ex: '20' ou '20:00'"),
):
    """
    Sessões de um filme para a data indicada (padrão: hoje).
    Se não houver sessões hoje e `data` não foi informado, avança para a próxima data disponível.
    """
    try:
        city_id, city_name = _resolve_city(cidade)
        movies = api_movies(city_id)
        movie  = find_movie(filme, movies)
        if not movie:
            raise HTTPException(status_code=404, detail=f"Filme não encontrado: {filme}")

        date_str, explicit = resolve_date(data)
        day = api_sessions(movie["id"], date_str, city_id)

        if not day or not day.get("theaters"):
            dates_raw = api_session_dates(movie["id"], city_id) or []
            if not explicit and dates_raw:
                date_str = dates_raw[0]["date"]
                day = api_sessions(movie["id"], date_str, city_id)

        if not day or not day.get("theaters"):
            raise HTTPException(
                status_code=404,
                detail=f"Sem sessões para '{movie['title']}' em {city_name} na data {date_str}",
            )

    except HTTPException:
        raise
    except APIError as e:
        _api_error(e)

    theaters = day["theaters"]

    # Apply filters
    if teatro:
        q = normalize(teatro)
        theaters = [t for t in theaters if q in normalize(t["name"])]

    hora_q = hora.replace("h", ":").strip() if hora else None
    if hora_q:
        # Build new dicts: the theaters come from a cache shared across requests.
        theaters = [
            {**t, "sessionTypes": [
                {**st, "sessions": [s for s in st["sessions"] if s.get("time", "").startswith(hora_q)]}
                for st in t.get("sessionTypes", [])
            ]}
            for t in theaters
        ]

    return {
        "movie":         {"id": movie["id"], "title": movie["title"], "urlKey": movie.get("urlKey")},
        "city":          city_name,
        "date":          date_str,
        "dateFormatted": day.get("dateFormatted"),
        "dayOfWeek":     day.get("dayOfWeek"),
        "theaters":      theaters,
    }


@app.get("/tickets/{session_id}/{section_id}", summary="Preços de uma sessão")
def get_tickets(session_id: str, section_id: str):
    """
    Retorna os tipos de ingresso com preço base, taxa de serviço e total.
    Inclui flag `meia_fee_warning` quando a taxa da Meia-Entrada é desproporcional (> 20%).
    """
    try:
        raw    = api_tickets(session_id, section_id)
        parsed = parse_tickets(raw)
    except APIError as e:
        _api_error(e)

    if not raw:
        raise HTTPException(status_code=404, detail="Sessão ou seção não encontrada")

    meia    = parsed["meia"]
    warning = False
    if meia and meia.get("price", 0) > 0:
        warning = (meia["service"] / meia["price"] * 100) > 20

    return {
        "session_id":        session_id,
        "section_id":        section_id,
        "inteira":           parsed["inteira"],
        "meia":              meia,
        "all":               parsed["all"],
        "meia_fee_warning":  warning,
    }


def _occupancy(data: dict) -> dict:
    total = data.get("totalSeats", 0)
    avail = sum(
        1 for ln in data.get("lines", [])
        for s in ln.get("seats", [])
        if s.get("status") == "Available"
    )
    return {
        "totalSeats":     total,
        "availableSeats": avail,
        "occupiedSeats":  total - avail,
        "occupancyPct":   round((total - avail) / total * 100, 1) if total else 0,
    }


@app.get("/assentos/{session_id}/{section_id}", summary="Mapa de assentos de uma sessão")
def get_assentos(
    session_id: str,
    section_id: str,
    atualizar:  bool = Query(False, description="Ignora o cache e busca a ocupação agora"),
):
    """
    Retorna o mapa de assentos completo: geometria da sala, status de cada assento
    (Available / Unavailable), tipo (Regular, SuperSeat, Couple…) e posição (linha/coluna).
    """
    try:
        data = api_seats(session_id, section_id, force=atualizar)
    except APIError as e:
        _api_error(e)

    if not data:
        raise HTTPException(status_code=404, detail="Sessão ou seção não encontrada")

    response = {
        "session_id":         session_id,
        "section_id":         section_id,
        "theaterName":        data.get("theaterName"),
        "roomName":           data.get("theaterLocationName"),
        **_occupancy(data),
        "activeReservations": data.get("activeReservations", 0),
        "bounds":             data.get("bounds"),
        "stage":              data.get("stage"),
        "labels":             data.get("labels"),
        "lines":              data.get("lines", []),
    }
    return JSONResponse(response, headers={"Cache-Control": "no-cache"} if atualizar else None)


@app.get("/ocupacao", summary="Ocupação de várias sessões de uma vez")
def get_ocupacao(s: list[str] = Query(..., description="session_id:section_id (repetível, máx. 80)")):
    """
    Versão leve de /assentos para listas: só os totais, sem o mapa, e buscando
    as sessões em paralelo. Sessões que falharem voltam como `null`.
    """
    pairs = []
    for item in s[:80]:
        sid, _, sec = item.partition(":")
        if not sid or not sec:
            raise HTTPException(status_code=422, detail=f"Formato inválido: {item!r} (use session:section)")
        pairs.append((sid, sec))

    def one(pair):
        try:
            data = api_seats(*pair)
        except APIError:
            return None
        return _occupancy(data) if data else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, pairs))
    return {f"{sid}:{sec}": r for (sid, sec), r in zip(pairs, results)}
