"""test_bot.py — state, formatting, handlers and jobs of bot.py (Telegram faked, API patched)."""

from datetime import datetime
from unittest.mock import patch

import pytest

import bot


class FakeTG:
    def __init__(self):
        self.sent = []
        self.calls = []

    def send(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    def send_all(self, chat_id, blocks, **kw):
        for p in bot.chunk(blocks):
            self.sent.append((chat_id, p))

    def call(self, method, **kw):
        self.calls.append((method, kw))


@pytest.fixture
def st(tmp_path):
    return bot.State(str(tmp_path / "s.db"))


@pytest.fixture
def tg():
    return FakeTG()


def say(b, text, chat=1):
    b.handle({"message": {"text": text, "chat": {"id": chat}}})


def movie(i, title, **kw):
    return {"id": i, "title": title, "urlKey": title.lower().replace(" ", "-"), **kw}


# ── State ─────────────────────────────────────────────────────────────────────

def test_favs_persist_across_instances(tmp_path):
    p = str(tmp_path / "s.db")
    bot.State(p).toggle_fav(7, "1416")
    assert bot.State(p).favs(7) == ["1416"]


def test_toggle_fav_adds_and_removes(st):
    assert st.toggle_fav(1, "a") == ["a"]
    assert st.toggle_fav(1, "b") == ["a", "b"]
    assert st.toggle_fav(1, "a") == ["b"]


def test_chats_are_isolated(st):
    st.toggle_fav(1, "a")
    st.toggle_fav(2, "b")
    assert st.favs(1) == ["a"] and st.favs(2) == ["b"]


def test_watchlist_roundtrip(st):
    st.watch_add(1, "9", "Fogaréu", {"presale": False})
    assert st.watch_list(1)[0]["title"] == "Fogaréu"
    st.watch_remove(1, "9")
    assert st.watch_list(1) == []


# ── Formatting ────────────────────────────────────────────────────────────────

def test_type_label_hides_normal_and_shortens():
    assert bot._type_label(["Normal", "Vip", "Dublado"]) == "Vip/DUB"
    assert bot._type_label(["Nacional"]) == ""


def test_format_drops_started_sessions():
    m = {"sessionTypes": [{"type": ["Nacional"], "sessions": [
        {"time": "10:00", "date": {"localDate": "2026-11-16T10:00:00-03:00"}},
        {"time": "20:00", "date": {"localDate": "2026-11-16T20:00:00-03:00"}}]}]}
    now = datetime.fromisoformat("2026-11-16T15:00:00-03:00")
    assert bot.format_movie_times(m, now) == "20:00"
    assert bot.format_movie_times(m) == "10:00 20:00"


def test_title_is_html_escaped():
    day = {"movies": [{"title": "A & B", "sessionTypes": [
        {"type": ["Nacional"], "sessions": [{"time": "10:00"}]}]}]}
    assert "A &amp; B" in bot.format_theater_day("X", day)


def test_chunk_respects_limit():
    parts = bot.chunk(["x" * 60] * 10, limit=150)
    assert all(len(p) <= 150 for p in parts) and len(parts) > 1


def test_digest_due():
    thu_10 = datetime.fromisoformat("2026-11-19T10:00:00-03:00")
    thu_08 = datetime.fromisoformat("2026-11-19T08:00:00-03:00")
    wed = datetime.fromisoformat("2026-11-18T10:00:00-03:00")
    assert bot.digest_due(thu_10, None)
    assert not bot.digest_due(thu_10, bot.digest_key(thu_10.date()))
    assert not bot.digest_due(thu_08, None) and not bot.digest_due(wed, None)


def test_watch_changes():
    old = {"playing": False, "presale": False, "premiere": "2026-11-26"}
    assert bot.watch_changes("F", old, dict(old)) == []
    msgs = bot.watch_changes("F", old, {"playing": True, "presale": True, "premiere": "2026-11-20"})
    assert len(msgs) == 3


# ── Handlers ──────────────────────────────────────────────────────────────────

def test_hoje_without_favs_points_to_cinemas(st, tg):
    say(bot.Bot(tg, st), "/hoje")
    assert "/cinemas" in tg.sent[0][1]


def test_callback_toggles_favorite(st, tg):
    b = bot.Bot(tg, st)
    with patch("core.api_theaters", return_value=[{"id": "1", "name": "A"}]):
        b.handle({"callback_query": {"id": "c", "data": "t:1",
                                     "message": {"chat": {"id": 1}, "message_id": 5}}})
    assert st.favs(1) == ["1"]
    assert [c[0] for c in tg.calls] == ["answerCallbackQuery", "editMessageReplyMarkup"]


def test_unauthorized_chat_ignored(st, tg):
    b = bot.Bot(tg, st, allowed={42})
    say(b, "/start", chat=1)
    assert tg.sent == [] and st.chats() == []


def test_seguir_and_parar(st, tg):
    b = bot.Bot(tg, st)
    with patch("core.api_movies", return_value=[movie("1", "Cara De Barro", isPlaying=True)]), \
         patch("core.api_coming_soon", return_value=[movie("2", "Fogaréu", inPreSale=False)]):
        say(b, "/seguir fogareu")
    assert [m["title"] for m in st.watch_list(1)] == ["Fogaréu"]
    say(b, "/parar fogareu")
    assert st.watch_list(1) == []


def test_api_error_reported_to_user(st, tg):
    b = bot.Bot(tg, st)
    with patch("core.api_coming_soon", side_effect=bot.core.APIError("boom")):
        say(b, "/embreve")
    assert "não respondeu" in tg.sent[0][1]


# ── Jobs ──────────────────────────────────────────────────────────────────────

def test_new_soon_seeds_silently_then_notifies(st, tg):
    st.ensure(1)
    with patch("core.api_coming_soon", return_value=[movie("1", "A")]):
        bot.job_new_soon(tg, st)
    assert tg.sent == []
    with patch("core.api_coming_soon", return_value=[movie("1", "A"), movie("2", "B")]):
        bot.job_new_soon(tg, st)
    assert len(tg.sent) == 1 and "B" in tg.sent[0][1]


def test_new_soon_respects_mute(st, tg):
    st.ensure(1)
    st.toggle_notify(1)
    with patch("core.api_coming_soon", return_value=[movie("1", "A")]):
        bot.job_new_soon(tg, st)
    with patch("core.api_coming_soon", return_value=[movie("1", "A"), movie("2", "B")]):
        bot.job_new_soon(tg, st)
    assert tg.sent == []


def test_grid_job_flags_followed_movie(st, tg):
    st.toggle_fav(1, "T")
    st.watch_add(1, "2", "B", {})
    with patch("core.api_theaters", return_value=[{"id": "T", "name": "Cine"}]), \
         patch.object(bot, "grid_additions", return_value={"1": ("A", "2026-11-16")}):
        bot.job_new_in_grid(tg, st)
    with patch("core.api_theaters", return_value=[{"id": "T", "name": "Cine"}]), \
         patch.object(bot, "grid_additions",
                      return_value={"1": ("A", "2026-11-16"), "2": ("B", "2026-11-19")}):
        bot.job_new_in_grid(tg, st)
    assert len(tg.sent) == 1 and "⭐ B" in tg.sent[0][1]


def test_watch_job_notifies_once(st, tg):
    st.watch_add(1, "2", "B", {"playing": False, "presale": False, "premiere": "2026-11-26"})
    cat = [movie("2", "B", inPreSale=True, premiereDate={"localDate": "2026-11-26T00:00:00+00:00"})]
    with patch("core.api_movies", return_value=[]), patch("core.api_coming_soon", return_value=cat):
        bot.job_watch(tg, st)
        bot.job_watch(tg, st)
    assert len(tg.sent) == 1 and "pré-venda" in tg.sent[0][1]


def test_digest_sends_once_per_week(st, tg):
    st.toggle_fav(1, "T")
    thu = datetime.fromisoformat("2026-11-19T10:00:00-03:00")
    day = {"movies": [{"id": "1", "title": "A"}]}
    with patch("core.api_theaters", return_value=[{"id": "T", "name": "Cine"}]), \
         patch("core.api_sessions_by_theater", return_value=day):
        bot.job_digest(tg, st, thu)
        bot.job_digest(tg, st, thu)
    assert len(tg.sent) == 1 and "Nova semana" in tg.sent[0][1]
