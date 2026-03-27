"""
app.py — FastAPI web API for cinema-fortaleza.

Run:
  uvicorn app:app --reload

Endpoints:
  GET /filmes
  GET /sessoes/{filme}?data=&teatro=&hora=
  GET /sessoes/{filme}/datas
  GET /tickets/{session_id}/{section_id}
  GET /assentos/{session_id}/{section_id}
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from core import (
    APIError,
    api_movies, api_session_dates, api_sessions, api_tickets, api_seats,
    api_states, find_movie, normalize, resolve_date, resolve_city, parse_tickets,
)

app = FastAPI(
    title="Cinema Fortaleza API",
    description="Sessões, preços e mapas de assentos dos cinemas de Fortaleza via ingresso.com",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _api_error(e: APIError):
    raise HTTPException(status_code=503, detail=f"Erro ao contactar ingresso.com: {e}")

def _resolve_city(cidade: str | None) -> tuple[int, str]:
    city_id, city_name = resolve_city(cidade)
    if city_id is None:
        raise HTTPException(status_code=404, detail=f"Cidade não encontrada: {cidade}")
    return city_id, city_name


# ── Routes ────────────────────────────────────────────────────────────────────

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
        movies = api_movies(city_id)
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
            "posterUrl":     m.get("posterUrl") or m.get("images", [None])[0],
            "premiereDate":  m.get("premiereDate"),
            "countPlaying":  m.get("countIsPlaying", 0),
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
        for t in theaters:
            for st in t.get("sessionTypes", []):
                st["sessions"] = [
                    s for s in st["sessions"]
                    if s.get("time", "").startswith(hora_q)
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


@app.get("/assentos/{session_id}/{section_id}", summary="Mapa de assentos de uma sessão")
def get_assentos(session_id: str, section_id: str):
    """
    Retorna o mapa de assentos completo: geometria da sala, status de cada assento
    (Available / Unavailable), tipo (Regular, SuperSeat, Couple…) e posição (linha/coluna).
    """
    try:
        data = api_seats(session_id, section_id)
    except APIError as e:
        _api_error(e)

    if not data:
        raise HTTPException(status_code=404, detail="Sessão ou seção não encontrada")

    lines = data.get("lines", [])
    total = data.get("totalSeats", 0)
    avail = sum(
        1 for ln in lines
        for s in ln.get("seats", [])
        if s.get("status") == "Available"
    )

    return {
        "session_id":        session_id,
        "section_id":        section_id,
        "theaterName":       data.get("theaterName"),
        "roomName":          data.get("theaterLocationName"),
        "totalSeats":        total,
        "availableSeats":    avail,
        "occupiedSeats":     total - avail,
        "occupancyPct":      round((total - avail) / total * 100, 1) if total else 0,
        "activeReservations": data.get("activeReservations", 0),
        "stage":             data.get("stage"),
        "labels":            data.get("labels"),
        "lines":             lines,
    }
