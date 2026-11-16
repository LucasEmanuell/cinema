"""test_events.py — title/tag classification and the weekly report grouping."""

import pytest

import bot
import events


def m(title, year=2026, tags=None, premiere="2026-11-19", i=None):
    return {"id": i or title, "title": title, "releaseYear": year, "tags": tags or [],
            "premiereDate": {"localDate": f"{premiere}T00:00:00+00:00"}}


@pytest.mark.parametrize("title,kind,label", [
    ("Sexta Feira 13 - UCI Day Terror 2026", "evento", "UCI Day Terror 2026"),
    ("Porão Do Belas: Angst", "evento", "Porão Do Belas"),
    ("Eu Sou O Rocky - Sessão Hideout", "evento", "Sessão Hideout"),
    ("Escola da Magia - Escape Route", "evento", "Escape Route"),
    ("Sessão Debate: Peixe Abissal", "evento", "Sessão Debate"),
    ("Kinopass POP - 14/12/2026", "passe", ""),
    ("Vale Sul - Clube do Aluno - POP - 31/12/2026", "passe", ""),
    ("Malês (Relançamento)", "relancamento", ""),
    ("Shrek (25º Aniversário)", "relancamento", ""),
    ("BTS World Tour 'Arirang' In Jakarta: Live Viewing", "show", ""),
    ("O Fantasma Da Ópera", "estreia", ""),
    ("Cine Encontro: Os Últimos 25 Anos Do Cinema Brasileiro", "evento", "Cine Encontro"),
    ("Os Farofeiros 3", "estreia", ""),
])
def test_classify_titles(title, kind, label):
    c = events.classify(m(title))
    assert (c["kind"], c["label"]) == (kind, label)


def test_passageira_is_not_a_pass():
    assert events.classify(m("A Estranha Passageira", year=1942))["kind"] == "classico"


def test_tag_makes_festival():
    c = events.classify(m("100 Dias", tags=["Festival do Cinema Brasileiro"]))
    assert c == {"kind": "festival", "label": "Festival do Cinema Brasileiro"}


def test_event_wins_over_relaunch():
    assert events.classify(m("Panico 30 Anos - UCI Day Terror 2026"))["kind"] == "evento"


def test_build_report_groups_and_filters_window():
    soon = [m("Fogaréu", i="1", premiere="2026-11-19"),
            m("Longe Demais", i="2", premiere="2026-12-30"),          # outside window
            m("Duna (Relançamento)", i="3", premiere="2026-11-20"),
            m("Kinopass POP - 14/12/2026", i="4", premiere="2026-11-20")]
    grid = {"9": {"movie": m("Sexta Feira 13 - UCI Day Terror 2026", year=1980, i="9"),
                  "dates": {"2026-11-20", "2026-11-21", "2026-11-30"}, "theaters": {"UCI"}}}
    rep = events.build_report(soon, grid, "2026-11-19", "2026-11-25")
    assert [t for t, _ in rep["estreias"]] == ["Fogaréu"]
    assert [t for t, _ in rep["relancamentos"]] == ["Duna (Relançamento)"]
    assert rep["eventos"]["UCI Day Terror 2026"] == [
        ("Sexta Feira 13 - UCI Day Terror 2026", ["2026-11-20", "2026-11-21"])]
    assert not any("Kinopass" in t for t, _ in rep["estreias"] + rep["classicos"])


def test_playing_film_is_not_a_premiere_this_week():
    grid = {"5": {"movie": m("Já Em Cartaz", i="5", premiere="2026-10-01"),
                  "dates": {"2026-11-20"}, "theaters": {"X"}}}
    rep = events.build_report([], grid, "2026-11-19", "2026-11-25")
    assert rep["estreias"] == []


def test_format_report_and_dates():
    rep = {"estreias": [("A & B", ["2026-11-19"])], "relancamentos": [], "shows": [],
           "classicos": [], "eventos": {"UCI Day": [("X", ["2026-10-30", "2026-10-31", "2026-11-01"])]},
           "festivais": {}}
    out = "\n".join(bot.format_report(rep))
    assert "A &amp; B — 19/11" in out and "30/10, 31/10 e 01/11" in out
    assert bot.format_report({"estreias": [], "relancamentos": [], "shows": [], "classicos": [],
                              "eventos": {}, "festivais": {}}) == []
