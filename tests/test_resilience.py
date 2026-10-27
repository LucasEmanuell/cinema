"""
test_resilience.py — cache layers, HTTP error handling, and the batch
occupancy endpoint. Everything is offline: requests and the cache dir are patched.
"""
import copy
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import core
from core import APIError
from app import app

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path):
    with patch("core.CACHE_DIR", tmp_path):
        yield tmp_path


def _resp(status=200, data=None):
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.json.return_value = data
    return r


# ── fetch(): errors vs. "nothing here" ────────────────────────────────────────

class TestFetchErrors:
    def test_5xx_raises_instead_of_looking_empty(self):
        with patch.object(core._session, "get", return_value=_resp(503)):
            with pytest.raises(APIError):
                core.fetch("http://x/a")

    def test_404_and_204_are_none(self):
        for status in (204, 404):
            with patch.object(core._session, "get", return_value=_resp(status)):
                assert core.fetch("http://x/a") is None

    def test_network_error_raises(self):
        import requests
        with patch.object(core._session, "get", side_effect=requests.ConnectionError("down")):
            with pytest.raises(APIError):
                core.fetch("http://x/a")

    def test_error_is_not_cached(self):
        get = MagicMock(side_effect=[_resp(503), _resp(200, {"ok": 1})])
        with patch.object(core._session, "get", get):
            with pytest.raises(APIError):
                core.fetch("http://x/a", cache_key="k", ttl=60)
            assert core.fetch("http://x/a", cache_key="k", ttl=60) == {"ok": 1}


# ── cache behaviour ───────────────────────────────────────────────────────────

class TestCacheLayers:
    def test_hit_does_not_refetch(self):
        get = MagicMock(return_value=_resp(200, [1]))
        with patch.object(core._session, "get", get):
            core.fetch("http://x/a", cache_key="k", ttl=60)
            core.fetch("http://x/a", cache_key="k", ttl=60)
        assert get.call_count == 1

    def test_empty_answer_is_remembered_briefly(self):
        get = MagicMock(return_value=_resp(204))
        with patch.object(core._session, "get", get):
            assert core.fetch("http://x/a", cache_key="k", ttl=60) is None
            assert core.fetch("http://x/a", cache_key="k", ttl=60) is None
        assert get.call_count == 1

    def test_disk_write_is_atomic_no_tmp_left(self, isolated_cache):
        core.cache_set("k", {"a": 1}, 60)
        assert [p.name for p in isolated_cache.iterdir()] == ["k.json"]

    def test_cold_process_reads_disk(self, isolated_cache):
        core.cache_set("k", {"a": 1}, 60)
        core._MEM.clear()                       # simulate a fresh process / the CLI
        assert core.cache_get("k") == {"a": 1}

    def test_stale_is_served_and_refreshed_in_background(self):
        core.cache_set("k", "old", ttl=1)
        refreshed = threading.Event()

        def loader():
            refreshed.set()
            return "new"

        with patch("core.time.time", return_value=time.time() + 10):   # past TTL, inside stale window
            assert core.cached("k", 1, loader, stale=3600) == "old"
        assert refreshed.wait(2)
        for _ in range(50):                     # let the background write land
            if core.cache_get("k") == "new":
                break
            time.sleep(0.02)
        assert core.cache_get("k") == "new"

    def test_stale_value_is_fallback_when_api_down(self):
        core.cache_set("k", "old", ttl=1)

        def boom():
            raise APIError("down")

        with patch("core.time.time", return_value=time.time() + 10):
            assert core.cached("k", 1, boom, stale=3600, force=True) == "old"

    def test_no_stale_window_means_error_propagates(self):
        core.cache_set("k", "old", ttl=1)

        def boom():
            raise APIError("down")

        with patch("core.time.time", return_value=time.time() + 10):
            with pytest.raises(APIError):
                core.cached("k", 1, boom, force=True)

    def test_concurrent_cold_requests_share_one_fetch(self):
        calls = []

        def loader():
            calls.append(1)
            time.sleep(0.1)
            return "v"

        threads = [threading.Thread(target=core.cached, args=("k", 60, loader)) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(calls) == 1
        assert core._locks == {}                # locks are cleaned up


# ── big cities: scan truncated -> check remaining movies via /dates ───────────

class TestMoviesInCity:
    def test_truncated_scan_falls_back_to_dates(self):
        movies = [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}]
        scan   = {"ids": ["1"], "truncated": True}
        with patch("core.api_movies", return_value=movies), \
             patch("core.api_city_scan", return_value=scan), \
             patch("core.api_session_dates", return_value=[{"date": "2026-01-01"}]) as dates:
            got = core.api_movies_in_city(1)
        assert [m["id"] for m in got] == ["1", "2"]
        dates.assert_called_once_with("2", 1)

    def test_complete_scan_trusts_it(self):
        movies = [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}]
        scan   = {"ids": ["1"], "truncated": False}
        with patch("core.api_movies", return_value=movies), \
             patch("core.api_city_scan", return_value=scan), \
             patch("core.api_session_dates") as dates:
            got = core.api_movies_in_city(1)
        assert [m["id"] for m in got] == ["1"]
        dates.assert_not_called()


# ── /ocupacao ─────────────────────────────────────────────────────────────────

class TestOcupacao:
    def test_batch_returns_counts_without_seat_map(self, seats):
        with patch("app.api_seats", return_value=seats):
            r = client.get("/ocupacao?s=1:10&s=2:20")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"1:10", "2:20"}
        assert set(body["1:10"]) == {"totalSeats", "availableSeats", "occupiedSeats", "occupancyPct"}

    def test_failures_become_null_not_a_500(self, seats):
        def fake(sid, sec, force=False):
            if sid == "bad":
                raise APIError("x")
            return seats
        with patch("app.api_seats", side_effect=fake):
            body = client.get("/ocupacao?s=ok:1&s=bad:1").json()
        assert body["ok:1"] and body["bad:1"] is None

    def test_malformed_item_is_422(self):
        assert client.get("/ocupacao?s=nocolon").status_code == 422

    def test_atualizar_bypasses_cache(self, seats):
        with patch("app.api_seats", return_value=seats) as m:
            r = client.get("/assentos/1/2?atualizar=1")
        m.assert_called_once_with("1", "2", force=True)
        assert r.headers["cache-control"] == "no-cache"


# ── shared cache objects must not be mutated by requests ──────────────────────

def test_hora_filter_does_not_mutate_cached_sessions(movies, sessions):
    original = copy.deepcopy(sessions)
    shared = copy.deepcopy(sessions)
    hour = shared["theaters"][0]["sessionTypes"][0]["sessions"][0]["time"][:2]
    with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
         patch("app.api_movies", return_value=movies), \
         patch("app.api_sessions", return_value=shared):
        client.get(f"/sessoes/{movies[0]['urlKey']}?hora=99")   # matches nothing
    assert shared == original


def test_responses_are_cacheable_and_gzipped(movies):
    with patch("app.resolve_city", return_value=(36, "Fortaleza")), \
         patch("app.api_movies_in_city", return_value=movies * 20):
        r = client.get("/filmes", headers={"Accept-Encoding": "gzip"})
    assert "max-age" in r.headers["cache-control"]
    assert r.headers.get("content-encoding") == "gzip"
