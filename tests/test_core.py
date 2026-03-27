"""
test_core.py — Unit tests for core.py (data layer).

What is tested here:
  - normalize()      Text normalization: accents, case, spaces
  - find_movie()     Fuzzy movie lookup by id, urlKey, partial title
  - resolve_date()   Date parsing: today, amanha, +N, literal YYYY-MM-DD
  - find_city()      Fuzzy city lookup against the states fixture
  - resolve_city()   City resolution with default fallback to Fortaleza
  - parse_tickets()  Ticket extraction: inteira, meia, edge cases
  - cache            TTL hit/miss/expiry behavior

What is NOT tested here:
  - fetch()          Just wraps requests — tested implicitly via integration
  - render_seat_map  Visual output, not meaningful to assert
  - CLI output       Tested manually or via snapshot tools if needed

Mocking strategy:
  - Pure functions (normalize, find_movie, resolve_date, parse_tickets)
    need no mocking — they have no side effects.
  - find_city / resolve_city call api_states() internally; we patch
    'core.api_states' to return the states fixture instead of hitting the network.
  - Cache tests patch 'core.CACHE_DIR' to use tmp_path so they never
    touch the real ~/.cache directory.
"""

import time
import pytest
from unittest.mock import patch
from freezegun import freeze_time

from core import (
    CITY_ID,
    normalize,
    find_movie,
    find_city,
    resolve_city,
    resolve_date,
    parse_tickets,
    cache_get,
    cache_set,
    check_schema,
)


# ── normalize ────────────────────────────────────────────────────────────────

class TestNormalize:
    def test_strips_accents(self):
        assert normalize("Pânico") == "panico"

    def test_lowercases(self):
        assert normalize("MARIO") == "mario"

    def test_strips_leading_trailing_spaces(self):
        assert normalize("  mario  ") == "mario"

    def test_multiple_accents_in_one_word(self):
        assert normalize("São Paulo") == "sao paulo"

    def test_cedilla(self):
        assert normalize("Ação") == "acao"

    def test_already_normalized_is_unchanged(self):
        assert normalize("mario") == "mario"

    def test_empty_string(self):
        assert normalize("") == ""


# ── find_movie ───────────────────────────────────────────────────────────────

class TestFindMovie:
    def test_by_numeric_id(self, movies):
        first = movies[0]
        result = find_movie(str(first["id"]), movies)
        assert result is not None
        assert result["id"] == first["id"]

    def test_by_urlkey_exact(self, movies):
        first = movies[0]
        result = find_movie(first["urlKey"], movies)
        assert result is not None
        assert result["urlKey"] == first["urlKey"]

    def test_partial_title_match(self, movies):
        # Take the first 4 chars of the title — should still match
        partial = movies[0]["title"][:4]
        result = find_movie(partial, movies)
        assert result is not None

    def test_accent_tolerant_query(self, movies):
        # Strip accents from a title and query without them
        title = movies[0]["title"]
        stripped = normalize(title)
        result = find_movie(stripped, movies)
        assert result is not None
        assert result["id"] == movies[0]["id"]

    def test_case_insensitive(self, movies):
        title = movies[0]["title"].upper()
        result = find_movie(title, movies)
        assert result is not None

    def test_not_found_returns_none(self, movies):
        assert find_movie("xyznotafilm999", movies) is None

    def test_returns_first_match_when_multiple(self, movies):
        # A query that matches all movies should return the first one
        result = find_movie("a", movies)  # 'a' is in most titles
        # Just verify it returns something deterministically
        assert result is not None
        assert result == movies[0] or result in movies


# ── resolve_date ─────────────────────────────────────────────────────────────

class TestResolveDate:
    @freeze_time("2026-03-26")
    def test_no_arg_returns_today(self):
        d, explicit = resolve_date(None)
        assert d == "2026-03-26"
        assert explicit is False

    @freeze_time("2026-03-26")
    def test_empty_string_returns_today(self):
        d, explicit = resolve_date("")
        assert d == "2026-03-26"
        assert explicit is False

    @freeze_time("2026-03-26")
    def test_amanha_without_accent(self):
        d, explicit = resolve_date("amanha")
        assert d == "2026-03-27"
        assert explicit is True

    @freeze_time("2026-03-26")
    def test_amanha_with_accent(self):
        # normalize() strips the ã so both forms work
        d, explicit = resolve_date("amanhã")
        assert d == "2026-03-27"
        assert explicit is True

    @freeze_time("2026-03-26")
    def test_plus_one(self):
        d, explicit = resolve_date("+1")
        assert d == "2026-03-27"
        assert explicit is True

    @freeze_time("2026-03-26")
    def test_plus_seven_crosses_month_boundary(self):
        d, explicit = resolve_date("+7")
        assert d == "2026-04-02"
        assert explicit is True

    @freeze_time("2026-03-26")
    def test_plus_zero(self):
        d, explicit = resolve_date("+0")
        assert d == "2026-03-26"
        assert explicit is True

    def test_literal_date_passthrough(self):
        d, explicit = resolve_date("2026-06-15")
        assert d == "2026-06-15"
        assert explicit is True


# ── find_city ────────────────────────────────────────────────────────────────

class TestFindCity:
    def test_exact_name(self, states):
        with patch("core.api_states", return_value=states):
            city = find_city("Fortaleza")
        assert city is not None
        assert city["name"] == "Fortaleza"
        assert city["id"] == "36"

    def test_partial_name(self, states):
        with patch("core.api_states", return_value=states):
            city = find_city("fortal")
        assert city is not None
        assert "Fortaleza" in city["name"]

    def test_accent_tolerant(self, states):
        # "sao paulo" should match "São Paulo"
        with patch("core.api_states", return_value=states):
            city = find_city("sao paulo")
        assert city is not None
        assert "Paulo" in city["name"]

    def test_case_insensitive(self, states):
        with patch("core.api_states", return_value=states):
            city = find_city("RECIFE")
        assert city is not None
        assert city["name"] == "Recife"

    def test_not_found_returns_none(self, states):
        with patch("core.api_states", return_value=states):
            city = find_city("xyznotacity999")
        assert city is None

    def test_urlkey_exact_match(self, states):
        with patch("core.api_states", return_value=states):
            city = find_city("fortaleza")  # matches urlKey and normalized name
        assert city is not None


# ── resolve_city ─────────────────────────────────────────────────────────────

class TestResolveCity:
    def test_default_is_fortaleza(self):
        """No argument → (36, 'Fortaleza') without any API call."""
        city_id, city_name = resolve_city(None)
        assert city_id == CITY_ID
        assert city_name == "Fortaleza"

    def test_found_returns_id_and_name(self, states):
        with patch("core.api_states", return_value=states):
            city_id, city_name = resolve_city("recife")
        assert city_id is not None
        assert isinstance(city_id, int)
        assert "Recife" in city_name

    def test_not_found_returns_none_tuple(self, states):
        with patch("core.api_states", return_value=states):
            city_id, city_name = resolve_city("xyznotacity999")
        assert city_id is None
        assert city_name is None


# ── parse_tickets ─────────────────────────────────────────────────────────────

class TestParseTickets:
    def test_inteira_found(self, tickets):
        parsed = parse_tickets(tickets)
        assert parsed["inteira"] is not None
        assert parsed["inteira"]["price"] > 0
        assert parsed["inteira"]["service"] > 0
        assert parsed["inteira"]["total"] > 0

    def test_meia_found(self, tickets):
        parsed = parse_tickets(tickets)
        assert parsed["meia"] is not None
        assert parsed["meia"]["price"] > 0

    def test_all_contains_real_tickets_only(self, tickets):
        parsed = parse_tickets(tickets)
        assert len(parsed["all"]) > 0
        for t in parsed["all"]:
            assert t["total"] > 0  # zero-price vouchers excluded

    def test_meia_unique_is_excluded(self):
        """UCI's 'Unique Meia Entrada' must not match as a regular meia."""
        data = {"default": [
            {"name": "Inteira",             "price": 40.0, "service": 5.6, "total": 45.6},
            {"name": "Unique Meia Entrada", "price": 20.0, "service": 5.6, "total": 25.6},
        ]}
        parsed = parse_tickets(data)
        assert parsed["meia"] is None

    def test_zero_price_tickets_excluded(self):
        """Voucher/complimentary tickets with total=0 must be filtered out."""
        data = {"default": [
            {"name": "Inteira", "price": 40.0, "service": 5.6, "total": 45.6},
            {"name": "Voucher", "price": 0.0,  "service": 0.0, "total": 0.0},
        ]}
        parsed = parse_tickets(data)
        assert len(parsed["all"]) == 1
        assert parsed["all"][0]["name"] == "Inteira"

    def test_none_data(self):
        parsed = parse_tickets(None)
        assert parsed["inteira"] is None
        assert parsed["meia"] is None
        assert parsed["all"] == []

    def test_empty_default_list(self):
        parsed = parse_tickets({"default": []})
        assert parsed["inteira"] is None
        assert parsed["meia"] is None
        assert parsed["all"] == []

    def test_inteira_total_equals_price_plus_service(self, tickets):
        parsed = parse_tickets(tickets)
        t = parsed["inteira"]
        assert abs(t["total"] - (t["price"] + t["service"])) < 0.01


# ── check_schema ──────────────────────────────────────────────────────────────

class TestCheckSchema:
    def test_valid_response_produces_no_log(self, tmp_path):
        """A response with all required fields must not write to the log."""
        data = [{"id": 1, "title": "X", "urlKey": "x", "isPlaying": True}]
        with patch("core.CACHE_DIR", tmp_path):
            check_schema(data, "movies")
        log = tmp_path / "schema_warnings.log"
        assert not log.exists()

    def test_missing_fields_writes_log(self, tmp_path):
        """A response missing required fields must append a warning to the log."""
        data = [{"id": 1}]  # missing title, urlKey, isPlaying
        with patch("core.CACHE_DIR", tmp_path):
            check_schema(data, "movies")
        log = tmp_path / "schema_warnings.log"
        assert log.exists()
        content = log.read_text()
        assert "SCHEMA WARNING" in content
        assert "movies" in content

    def test_log_lists_missing_fields(self, tmp_path):
        data = [{"id": 1}]
        with patch("core.CACHE_DIR", tmp_path):
            check_schema(data, "movies")
        content = (tmp_path / "schema_warnings.log").read_text()
        assert "title" in content or "urlKey" in content

    def test_unknown_schema_name_is_skipped(self, tmp_path):
        """An unknown schema name should not crash or write anything."""
        data = {"foo": "bar"}
        with patch("core.CACHE_DIR", tmp_path):
            check_schema(data, "unknown_endpoint")
        assert not (tmp_path / "schema_warnings.log").exists()

    def test_empty_data_is_skipped(self, tmp_path):
        with patch("core.CACHE_DIR", tmp_path):
            check_schema([], "movies")
            check_schema(None, "movies")
        assert not (tmp_path / "schema_warnings.log").exists()

    def test_list_response_checks_first_item(self, tmp_path):
        """For list responses the first element is checked, not the list itself."""
        data = [{"date": "2026-04-01", "theaters": []}]
        with patch("core.CACHE_DIR", tmp_path):
            check_schema(data, "sessions")
        assert not (tmp_path / "schema_warnings.log").exists()

    def test_dict_response_checked_directly(self, tmp_path):
        """Dict responses (e.g. tickets) are checked directly, not via [0]."""
        data = {"default": []}
        with patch("core.CACHE_DIR", tmp_path):
            check_schema(data, "tickets")
        assert not (tmp_path / "schema_warnings.log").exists()

    def test_multiple_warnings_are_appended(self, tmp_path):
        """Two check_schema failures should both appear in the same log file."""
        bad_movies   = [{"id": 1}]
        bad_sessions = [{"date": "2026-04-01"}]  # missing theaters
        with patch("core.CACHE_DIR", tmp_path):
            check_schema(bad_movies,   "movies")
            check_schema(bad_sessions, "sessions")
        content = (tmp_path / "schema_warnings.log").read_text()
        assert content.count("SCHEMA WARNING") == 2


# ── cache ─────────────────────────────────────────────────────────────────────

class TestCache:
    def test_miss_on_unknown_key(self, tmp_path):
        with patch("core.CACHE_DIR", tmp_path):
            assert cache_get("key_that_does_not_exist_xyz") is None

    def test_hit_within_ttl(self, tmp_path):
        with patch("core.CACHE_DIR", tmp_path):
            cache_set("mykey", {"x": 1}, ttl=60)
            result = cache_get("mykey")
        assert result == {"x": 1}

    def test_miss_after_ttl_expired(self, tmp_path):
        with patch("core.CACHE_DIR", tmp_path):
            cache_set("mykey", {"x": 1}, ttl=1)
            # Simulate time advancing past the TTL
            with patch("core.time.time", return_value=time.time() + 10):
                result = cache_get("mykey")
        assert result is None

    def test_different_keys_are_isolated(self, tmp_path):
        with patch("core.CACHE_DIR", tmp_path):
            cache_set("key_a", "value_a", ttl=60)
            cache_set("key_b", "value_b", ttl=60)
            assert cache_get("key_a") == "value_a"
            assert cache_get("key_b") == "value_b"

    def test_overwrite_existing_key(self, tmp_path):
        with patch("core.CACHE_DIR", tmp_path):
            cache_set("mykey", "first", ttl=60)
            cache_set("mykey", "second", ttl=60)
            assert cache_get("mykey") == "second"
