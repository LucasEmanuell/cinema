"""
events.py — classify catalog entries as premiere / re-release / named event /
festival / live show / ticket-product.

ingresso.com has no reliable "this is an event" flag: `isReexhibition` is always
False and `tags` only covers some festivals. What does work is the title (the
organizer writes the event name into it, e.g. "Sexta Feira 13 - UCI Day Terror
2026", "Porão Do Belas: Angst", "X - Sessão Hideout") plus `tags`.

classify(movie) -> {"kind": ..., "label": ...}
    kind: "passe"      ticket products (Kinopass, Clube do Aluno...) — hide from users
          "evento"     named event / series / debate / cineclube
          "show"       live viewing, concert, opera, ballet
          "relancamento"  re-release or anniversary
          "classico"   old film (release year 15+ years ago) with no other marker
          "festival"   tagged festival or season
          "estreia"    anything else
"""

import re
import unicodedata
from datetime import date


def _n(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


# Ticket products / passes that live in the catalog as fake "movies".
_PASSE = re.compile(
    r"\b(kinopass|cinesystem pass|cinepass|clube do aluno|clube de vantagem|cartao|"
    r"roxystar|passaporte|cineshow|orient club|pass|passe)\b|^vale sul -|\bingressos\b")

# (regex over normalized title, label builder). First match wins.
# The label is the matched slice of the ORIGINAL title (normalization keeps offsets).
_EVENTO = [
    re.compile(r"[-–]\s*(uci day [^-–]+)$"),
    re.compile(r"[-–]\s*(sessao hideout)$"),
    re.compile(r"[-–]\s*(escape route)$"),
    re.compile(r"^(porao do belas):"),
    re.compile(r"^(belas sonoriza):"),
    re.compile(r"^(cine ?clube\b[^:–-]*?)\s*[:–-]"),
    re.compile(r"^(sessao debate|pre-estreia com debate)"),
    re.compile(r"^(ciencia no cinema)"),
    re.compile(r"^(cinema de pijama)"),
    re.compile(r"^(sessao especial|sessao secreta)"),
    re.compile(r"^(halloween no roxy|book night|cine encontro)"),
    re.compile(r"^(pre estreia)\s*-"),
]
_SHOW = re.compile(r"live viewing|world tour|live on stage|\bballet\b|ao vivo|"
                   r"conmebol|libertadores|cheering party|back to the beginning")
_RELAN = re.compile(r"relancamento|\d+o aniversario|\(\d+ anos\)|- \d+ anos$")
_FEST_TAG = re.compile(r"festival|fest\b|semana|mostra|oscar|halloween|carnaval|ferias|ghibli")


def classify(m: dict) -> dict:
    title = _n(m.get("title"))
    if _PASSE.search(title):
        return {"kind": "passe", "label": ""}
    for rx in _EVENTO:
        mt = rx.search(title)
        if mt:
            return {"kind": "evento", "label": (m.get("title") or "")[mt.start(1):mt.end(1)].strip()}
    if _SHOW.search(title):
        return {"kind": "show", "label": ""}
    if _RELAN.search(title):
        return {"kind": "relancamento", "label": ""}
    for tag in m.get("tags") or []:
        if _FEST_TAG.search(_n(tag)):
            return {"kind": "festival", "label": tag}
    year = m.get("releaseYear") or 0
    if 1850 < year <= date.today().year - 15:
        return {"kind": "classico", "label": ""}
    return {"kind": "estreia", "label": ""}


# ── Weekly report ─────────────────────────────────────────────────────────────

def premiere_day(m: dict) -> str:
    p = m.get("premiereDate")
    return ((p.get("localDate") if isinstance(p, dict) else p) or "")[:10]


def scan_grid(days: int = 7, start: date | None = None) -> dict:
    """City-wide grid: movie_id -> {"movie", "dates", "theaters"} for `days` days."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    import core

    start = start or date.today()
    day_list = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    theaters = core.api_theaters()

    def one(job):
        t, d = job
        try:
            return t, d, core.api_sessions_by_theater(t["id"], d)
        except core.APIError:
            return t, d, None

    grid: dict = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        for t, d, day in pool.map(one, [(t, d) for t in theaters for d in day_list]):
            for m in (day or {}).get("movies", []):
                g = grid.setdefault(str(m["id"]), {"movie": m, "dates": set(), "theaters": set()})
                g["dates"].add(d)
                g["theaters"].add(t["name"])
    return grid


def build_report(soon: list, grid: dict, start: str, end: str) -> dict:
    """Group what happens between `start` and `end` (ISO dates, inclusive).

    soon: coming-soon catalog entries; grid: scan_grid() output.
    Returns {"estreias", "relancamentos", "shows", "classicos": [(title, dates)],
             "eventos": {label: [(title, dates)]}, "festivais": {tag: [titles]}}.
    """
    items: dict[str, dict] = {}
    for m in soon:
        d = premiere_day(m)
        if start <= d <= end:
            items[str(m["id"])] = {"movie": m, "dates": {d}, "theaters": set()}
    for mid, g in grid.items():
        it = items.setdefault(mid, {"movie": g["movie"], "dates": set(), "theaters": set()})
        it["dates"] |= {d for d in g["dates"] if start <= d <= end}
        it["theaters"] |= g["theaters"]

    rep = {"estreias": [], "relancamentos": [], "shows": [], "classicos": [],
           "eventos": {}, "festivais": {}}
    for mid, it in items.items():
        m, dates = it["movie"], sorted(it["dates"])
        if not dates:
            continue
        c = classify(m)
        row = (m["title"], dates)
        k = c["kind"]
        if k == "evento":
            rep["eventos"].setdefault(c["label"], []).append(row)
        elif k == "festival":
            rep["festivais"].setdefault(c["label"], []).append(m["title"])
        elif k == "estreia":
            # a premiere is only news if it actually opens in the window
            if start <= premiere_day(m) <= end:
                rep["estreias"].append((m["title"], [premiere_day(m)]))
        elif k == "relancamento":
            rep["relancamentos"].append(row)
        elif k == "show":
            rep["shows"].append(row)
        elif k == "classico":
            rep["classicos"].append(row)
    for key in ("estreias", "relancamentos", "shows", "classicos"):
        rep[key].sort(key=lambda r: (r[1][0], r[0]))
    return rep
