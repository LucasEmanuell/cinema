"""
conftest.py — Shared pytest fixtures for the entire test suite.

Fixture files in tests/fixtures/ are real, trimmed responses from the
ingresso.com API saved at a point in time. They are committed to the
repository so the test suite runs completely offline.

How fixtures were collected:
  - movies.json   → GET /v0/events?cityId=36&isPlaying=true  (first 3 results)
  - sessions.json → GET /v0/sessions/city/1/event/30773/.../groupBy/sessionType
                    (trimmed to 1 theater, 1 sessionType, 2 sessions)
  - tickets.json  → GET /v1/sessions/84283462/sections/4583484/tickets
  - seats.json    → GET /v1/sessions/84283462/sections/4583484/seats
  - states.json   → GET /v0/states?partnership=ingresso.com  (full, 27 states)

The session/section IDs (84283462 / 4583484) belong to Cine Araújo Campo Limpo,
São Paulo, on 2026-04-01. They are used as stable IDs throughout the test suite.
"""

import json
import pytest
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.fixture(scope="session")
def movies():
    """3 movies from GET /v0/events?cityId=36&isPlaying=true."""
    return _load("movies.json")


@pytest.fixture(scope="session")
def sessions_raw():
    """
    Raw fetch() response for the sessions endpoint (a list).
    api_sessions() returns sessions_raw[0] after calling fetch().
    """
    return _load("sessions.json")


@pytest.fixture(scope="session")
def sessions(sessions_raw):
    """
    Processed day object — what api_sessions() returns.
    Has keys: date, dateFormatted, dayOfWeek, isToday, theaters.
    """
    return sessions_raw[0]


@pytest.fixture(scope="session")
def tickets():
    """Response from GET /v1/sessions/84283462/sections/4583484/tickets."""
    return _load("tickets.json")


@pytest.fixture(scope="session")
def seats():
    """Response from GET /v1/sessions/84283462/sections/4583484/seats."""
    return _load("seats.json")


@pytest.fixture(scope="session")
def states():
    """Full response from GET /v0/states?partnership=ingresso.com (27 states)."""
    return _load("states.json")
