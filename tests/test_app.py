"""
test_app.py — Integration tests for app.py (FastAPI routes).

What is tested here:
  - All 6 endpoints: /cidades, /filmes, /sessoes/{filme}/datas,
    /sessoes/{filme}, /tickets/{sid}/{secid}, /assentos/{sid}/{secid}
  - HTTP status codes: 200 (happy path), 404 (not found), 503 (API down)
  - Query filters: ?cidade=, ?teatro=, ?hora=, ?data=
  - Response shape: required fields are present and typed correctly
  - Business logic: meia_fee_warning flag, occupancy calculation,
    auto-advance to next available date

What is NOT tested here:
  - The real ingresso.com API (all calls are mocked)
  - CLI rendering (separate concern)
  - Core helpers (covered in test_core.py)

Mocking strategy:
  Functions in app.py that make network calls are patched at the 'app' module
  level (e.g. patch('app.api_movies')). This is necessary because app.py imports
  them by name — patching 'core.api_movies' would not affect already-imported
  references inside app.py.

  Exception: resolve_city() is patched at 'app' level for most tests since
  it transitively calls api_states() → fetch() deep inside core.py. Tests that
  specifically exercise city-resolution edge cases patch 'app.resolve_city'
  directly to control the (city_id, city_name) return value.

  The two stable IDs used as URL parameters throughout this file come from the
  seats/tickets fixtures (Cine Araújo Campo Limpo, São Paulo, 2026-04-01):
    SESSION_ID = "84283462"
    SECTION_ID = "4583484"
"""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app import app

client = TestClient(app, raise_server_exceptions=False)

# Stable IDs from fixture data (Cine Araújo Campo Limpo, 2026-04-01)
SESSION_ID = "84283462"
SECTION_ID = "4583484"


# ── /cidades ──────────────────────────────────────────────────────────────────

class TestGetCidades:
    def test_returns_27_states(self, states):
        with patch("app.api_states", return_value=states):
            r = client.get("/cidades")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 27

    def test_each_state_has_cities(self, states):
        with patch("app.api_states", return_value=states):
            r = client.get("/cidades")
        for state in r.json():
            assert "name" in state
            assert "uf" in state
            assert "cities" in state
            assert len(state["cities"]) > 0

    def test_api_error_returns_503(self):
        from core import APIError
        with patch("app.api_states", side_effect=APIError("timeout")):
            r = client.get("/cidades")
        assert r.status_code == 503


# ── /filmes ───────────────────────────────────────────────────────────────────

class TestGetFilmes:
    def test_default_city_returns_movies(self, movies):
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies):
            r = client.get("/filmes")
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_custom_city_param(self, movies):
        with patch("app.resolve_city", return_value=(2, "Recife")), \
             patch("app.api_movies", return_value=movies):
            r = client.get("/filmes?cidade=recife")
        assert r.status_code == 200

    def test_unknown_city_returns_404(self):
        with patch("app.resolve_city", return_value=(None, None)):
            r = client.get("/filmes?cidade=xyznotacity")
        assert r.status_code == 404

    def test_response_shape(self, movies):
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies):
            r = client.get("/filmes")
        item = r.json()[0]
        for field in ("id", "title", "urlKey", "contentRating", "duration", "countPlaying"):
            assert field in item, f"Missing field: {field}"

    def test_api_error_returns_503(self):
        from core import APIError
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", side_effect=APIError("network")):
            r = client.get("/filmes")
        assert r.status_code == 503


# ── /sessoes/{filme}/datas ────────────────────────────────────────────────────

class TestGetDatas:
    def test_returns_available_dates(self, movies):
        dates = [
            {"date": "2026-04-01", "dayOfWeek": "Quarta-Feira"},
            {"date": "2026-04-02", "dayOfWeek": "Quinta-Feira"},
        ]
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies), \
             patch("app.api_session_dates", return_value=dates):
            r = client.get(f"/sessoes/{movies[0]['urlKey']}/datas")
        assert r.status_code == 200
        body = r.json()
        assert body["movie"]["id"] == movies[0]["id"]
        assert body["dates"] == dates

    def test_movie_not_found_returns_404(self, movies):
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies):
            r = client.get("/sessoes/xyznotafilme999/datas")
        assert r.status_code == 404

    def test_city_not_found_returns_404(self):
        with patch("app.resolve_city", return_value=(None, None)):
            r = client.get("/sessoes/mario/datas?cidade=xyznotacity")
        assert r.status_code == 404


# ── /sessoes/{filme} ──────────────────────────────────────────────────────────

class TestGetSessoes:
    def test_returns_sessions_for_movie(self, movies, sessions):
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies), \
             patch("app.api_sessions", return_value=sessions), \
             patch("app.api_session_dates", return_value=[]):
            r = client.get(f"/sessoes/{movies[0]['urlKey']}")
        assert r.status_code == 200
        body = r.json()
        assert body["movie"]["id"] == movies[0]["id"]
        assert body["city"] == "Fortaleza"
        assert "date" in body
        assert "theaters" in body

    def test_teatro_filter_matches(self, movies, sessions):
        """Partial teatro name should return only the matching theater."""
        theater_name = sessions["theaters"][0]["name"]
        partial = theater_name[:6]
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies), \
             patch("app.api_sessions", return_value=sessions), \
             patch("app.api_session_dates", return_value=[]):
            r = client.get(f"/sessoes/{movies[0]['urlKey']}?teatro={partial}")
        assert r.status_code == 200
        assert len(r.json()["theaters"]) == 1

    def test_teatro_filter_no_match_returns_empty_list(self, movies, sessions):
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies), \
             patch("app.api_sessions", return_value=sessions), \
             patch("app.api_session_dates", return_value=[]):
            r = client.get(f"/sessoes/{movies[0]['urlKey']}?teatro=xyznotacinema999")
        assert r.status_code == 200
        assert r.json()["theaters"] == []

    def test_hora_filter(self, movies, sessions):
        """Sessions outside the requested hour should be removed."""
        session_time = sessions["theaters"][0]["sessionTypes"][0]["sessions"][0]["time"]
        hora = session_time[:2]  # e.g. "16"
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies), \
             patch("app.api_sessions", return_value=sessions), \
             patch("app.api_session_dates", return_value=[]):
            r = client.get(f"/sessoes/{movies[0]['urlKey']}?hora={hora}")
        assert r.status_code == 200
        # All returned sessions should start with the requested hour
        for theater in r.json()["theaters"]:
            for st in theater["sessionTypes"]:
                for s in st["sessions"]:
                    assert s["time"].startswith(hora)

    def test_movie_not_found_returns_404(self, movies):
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies):
            r = client.get("/sessoes/xyznotafilme999")
        assert r.status_code == 404

    def test_city_not_found_returns_404(self):
        with patch("app.resolve_city", return_value=(None, None)):
            r = client.get("/sessoes/mario?cidade=xyznotacity")
        assert r.status_code == 404

    def test_no_sessions_returns_404(self, movies):
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies), \
             patch("app.api_sessions", return_value=None), \
             patch("app.api_session_dates", return_value=[]):
            r = client.get(f"/sessoes/{movies[0]['urlKey']}")
        assert r.status_code == 404

    def test_auto_advance_when_no_sessions_today(self, movies, sessions):
        """
        When no explicit date is given and today has no sessions,
        the API should re-query with the first available date.
        The response date should reflect the advanced date, not today.
        """
        next_date = "2026-04-01"
        dates = [{"date": next_date, "dayOfWeek": "Quarta-Feira"}]
        # First api_sessions call (today) returns None → triggers auto-advance
        # Second call (next_date) returns the real sessions
        with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
             patch("app.api_movies", return_value=movies), \
             patch("app.api_sessions", side_effect=[None, sessions]), \
             patch("app.api_session_dates", return_value=dates):
            r = client.get(f"/sessoes/{movies[0]['urlKey']}")
        assert r.status_code == 200
        assert r.json()["date"] == next_date


# ── /tickets/{session_id}/{section_id} ───────────────────────────────────────

class TestGetTickets:
    def test_returns_parsed_tickets(self, tickets):
        with patch("app.api_tickets", return_value=tickets):
            r = client.get(f"/tickets/{SESSION_ID}/{SECTION_ID}")
        assert r.status_code == 200
        body = r.json()
        assert "inteira" in body
        assert "meia" in body
        assert "all" in body
        assert "meia_fee_warning" in body
        assert "session_id" in body
        assert "section_id" in body

    def test_inteira_has_price_service_total(self, tickets):
        with patch("app.api_tickets", return_value=tickets):
            r = client.get(f"/tickets/{SESSION_ID}/{SECTION_ID}")
        t = r.json()["inteira"]
        assert t["price"] > 0
        assert t["service"] > 0
        assert t["total"] > 0

    def test_meia_fee_warning_false_when_proportional(self, tickets):
        """
        Araújo fixture has proportional service fees (~16%) — below 20% threshold.
        No warning should be emitted.
        """
        with patch("app.api_tickets", return_value=tickets):
            r = client.get(f"/tickets/{SESSION_ID}/{SECTION_ID}")
        assert r.json()["meia_fee_warning"] is False

    def test_meia_fee_warning_true_when_flat_fee(self):
        """
        Standard ingresso.com flat fee: 14% of Inteira applied to Meia too.
        With inteira=40 → fee=5.6. Meia=20 → fee_pct = 5.6/20 = 28% > 20%.
        Warning must be True.
        """
        data = {"default": [
            {"name": "Inteira", "price": 40.0, "service": 5.6, "total": 45.6},
            {"name": "Meia",    "price": 20.0, "service": 5.6, "total": 25.6},
        ]}
        with patch("app.api_tickets", return_value=data):
            r = client.get(f"/tickets/{SESSION_ID}/{SECTION_ID}")
        assert r.json()["meia_fee_warning"] is True

    def test_not_found_returns_404(self):
        with patch("app.api_tickets", return_value=None):
            r = client.get(f"/tickets/{SESSION_ID}/{SECTION_ID}")
        assert r.status_code == 404


# ── /assentos/{session_id}/{section_id} ──────────────────────────────────────

class TestGetAssentos:
    def test_returns_seat_map(self, seats):
        with patch("app.api_seats", return_value=seats):
            r = client.get(f"/assentos/{SESSION_ID}/{SECTION_ID}")
        assert r.status_code == 200
        body = r.json()
        assert body["totalSeats"] == seats["totalSeats"]
        assert body["theaterName"] == seats["theaterName"]
        assert "lines" in body
        assert "stage" in body
        assert "labels" in body

    def test_occupancy_calculation_is_consistent(self, seats):
        """availableSeats + occupiedSeats must equal totalSeats."""
        with patch("app.api_seats", return_value=seats):
            r = client.get(f"/assentos/{SESSION_ID}/{SECTION_ID}")
        body = r.json()
        assert body["availableSeats"] + body["occupiedSeats"] == body["totalSeats"]

    def test_occupancy_pct_is_in_range(self, seats):
        with patch("app.api_seats", return_value=seats):
            r = client.get(f"/assentos/{SESSION_ID}/{SECTION_ID}")
        pct = r.json()["occupancyPct"]
        assert 0.0 <= pct <= 100.0

    def test_occupancy_pct_matches_manual_calculation(self, seats):
        with patch("app.api_seats", return_value=seats):
            r = client.get(f"/assentos/{SESSION_ID}/{SECTION_ID}")
        body = r.json()
        total    = body["totalSeats"]
        occupied = body["occupiedSeats"]
        expected = round(occupied / total * 100, 1) if total else 0
        assert body["occupancyPct"] == expected

    def test_not_found_returns_404(self):
        with patch("app.api_seats", return_value=None):
            r = client.get(f"/assentos/{SESSION_ID}/{SECTION_ID}")
        assert r.status_code == 404
