"""
bot.py — Telegram bot for cinema-br (long polling, no extra dependencies).

Setup:
    1. Talk to @BotFather, /newbot, copy the token.
    2. export TELEGRAM_BOT_TOKEN=...            (required)
       export BOT_ALLOWED_IDS=123456,789        (optional: restrict to these chat ids)
       export BOT_DB=/path/bot_state.db         (optional)
    3. python bot.py

Commands:
    /cinemas            choose favorite theaters (inline buttons)
    /hoje, /amanha      sessions at your favorites
    /sessoes [data]     same, for +N, YYYY-MM-DD ...
    /filme <titulo> [data]   sessions of one movie (favorites first)
    /embreve            upcoming premieres
    /seguir /seguindo /parar   movies you are waiting for
    /avisos             toggle notifications on/off

Background jobs (same process):
    - new film in the grid of a favorite theater      (checked hourly)
    - new "em breve" title                            (checked hourly)
    - changes in followed movies (date, pre-sale, in theaters)
    - weekly digest on Thursday morning (DIGEST_WEEKDAY / DIGEST_HOUR)
"""

import html
import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import core
import events

TZ = timezone(timedelta(hours=-3))
DIGEST_WEEKDAY = int(os.environ.get("DIGEST_WEEKDAY", 3))   # Monday=0 → Thursday
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", 9))
CHECK_EVERY = 3600
GRID_DAYS = 7
MAX_MSG = 3800


# ── State ─────────────────────────────────────────────────────────────────────

class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS chats(
                    chat_id INTEGER PRIMARY KEY,
                    favs TEXT NOT NULL DEFAULT '[]',
                    notify INTEGER NOT NULL DEFAULT 1,
                    digest_week TEXT);
                CREATE TABLE IF NOT EXISTS seen(
                    chat_id INTEGER, theater_id TEXT, movie_id TEXT,
                    PRIMARY KEY(chat_id, theater_id, movie_id));
                CREATE TABLE IF NOT EXISTS seen_soon(movie_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS watch(
                    chat_id INTEGER, movie_id TEXT, title TEXT, last_state TEXT,
                    PRIMARY KEY(chat_id, movie_id));
            """)

    def _q(self, sql, args=(), write=False):
        with self.lock:
            cur = self.db.execute(sql, args)
            rows = cur.fetchall()
            if write:
                self.db.commit()
            return rows

    def ensure(self, chat_id):
        self._q("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", (chat_id,), True)

    def chats(self):
        return [r[0] for r in self._q("SELECT chat_id FROM chats")]

    def favs(self, chat_id) -> list[str]:
        r = self._q("SELECT favs FROM chats WHERE chat_id=?", (chat_id,))
        return json.loads(r[0][0]) if r else []

    def toggle_fav(self, chat_id, theater_id) -> list[str]:
        self.ensure(chat_id)
        favs = self.favs(chat_id)
        favs = [f for f in favs if f != theater_id] if theater_id in favs else favs + [theater_id]
        self._q("UPDATE chats SET favs=? WHERE chat_id=?", (json.dumps(favs), chat_id), True)
        return favs

    def notify(self, chat_id) -> bool:
        r = self._q("SELECT notify FROM chats WHERE chat_id=?", (chat_id,))
        return bool(r[0][0]) if r else True

    def toggle_notify(self, chat_id) -> bool:
        self.ensure(chat_id)
        new = not self.notify(chat_id)
        self._q("UPDATE chats SET notify=? WHERE chat_id=?", (int(new), chat_id), True)
        return new

    def digest_week(self, chat_id):
        r = self._q("SELECT digest_week FROM chats WHERE chat_id=?", (chat_id,))
        return r[0][0] if r else None

    def set_digest_week(self, chat_id, week):
        self._q("UPDATE chats SET digest_week=? WHERE chat_id=?", (week, chat_id), True)

    def seen(self, chat_id, theater_id) -> set[str]:
        return {r[0] for r in self._q(
            "SELECT movie_id FROM seen WHERE chat_id=? AND theater_id=?", (chat_id, theater_id))}

    def add_seen(self, chat_id, theater_id, movie_ids):
        with self.lock, self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO seen VALUES(?,?,?)",
                [(chat_id, theater_id, m) for m in movie_ids])

    def watch_add(self, chat_id, movie_id, title, state: dict):
        self.ensure(chat_id)
        self._q("INSERT OR REPLACE INTO watch VALUES(?,?,?,?)",
                (chat_id, movie_id, title, json.dumps(state)), True)

    def watch_remove(self, chat_id, movie_id):
        self._q("DELETE FROM watch WHERE chat_id=? AND movie_id=?", (chat_id, movie_id), True)

    def watch_list(self, chat_id) -> list[dict]:
        return [{"id": r[0], "title": r[1], "urlKey": "", "state": json.loads(r[2] or "{}")}
                for r in self._q(
                    "SELECT movie_id, title, last_state FROM watch WHERE chat_id=? ORDER BY title",
                    (chat_id,))]

    def watch_set_state(self, chat_id, movie_id, state: dict):
        self._q("UPDATE watch SET last_state=? WHERE chat_id=? AND movie_id=?",
                (json.dumps(state), chat_id, movie_id), True)

    def seen_soon(self) -> set[str]:
        return {r[0] for r in self._q("SELECT movie_id FROM seen_soon")}

    def add_seen_soon(self, ids):
        with self.lock, self.db:
            self.db.executemany("INSERT OR IGNORE INTO seen_soon VALUES(?)", [(i,) for i in ids])


# ── Formatting (pure) ─────────────────────────────────────────────────────────

def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _type_label(types) -> str:
    names = [t for t in (types or []) if t and t not in ("Nacional", "Normal")]
    short = {"Dublado": "DUB", "Legendado": "LEG"}
    return "/".join(short.get(n, n) for n in names)


def _session_dt(s) -> datetime | None:
    raw = (s.get("date") or {}).get("localDate")
    try:
        return datetime.fromisoformat(raw).astimezone(TZ) if raw else None
    except ValueError:
        return None


def format_movie_times(movie: dict, now: datetime | None = None) -> str:
    """'DUB 14:00 16:20 · LEG 19:00' (sessions already started are dropped)."""
    parts = []
    for st in movie.get("sessionTypes", []):
        times = []
        for s in st.get("sessions", []):
            dt = _session_dt(s)
            if now and dt and dt < now:
                continue
            times.append(s.get("time") or (dt.strftime("%H:%M") if dt else "?"))
        if times:
            label = _type_label(st.get("type"))
            parts.append((label + " " if label else "") + " ".join(times))
    return " · ".join(parts)


def format_theater_day(name: str, day: dict | None, now: datetime | None = None) -> str:
    lines = []
    for m in (day or {}).get("movies", []):
        times = format_movie_times(m, now)
        if times:
            rating = m.get("contentRating") or ""
            rating = f" ({esc(rating.replace(' anos', '').replace('Livre', 'L'))})" if rating else ""
            lines.append(f"• {esc(m['title'])}{rating}: {times}")
    if not lines:
        lines = ["<i>sem sessões</i>"]
    return f"<b>{esc(name)}</b>\n" + "\n".join(lines)


def format_titles(day: dict | None) -> str:
    titles = sorted({m["title"] for m in (day or {}).get("movies", [])})
    return ", ".join(esc(t) for t in titles) or "<i>nada ainda</i>"


def chunk(blocks: list[str], limit: int = MAX_MSG) -> list[str]:
    out, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > limit:
            out.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        out.append(cur)
    return out


def format_premiere(m: dict) -> str:
    p = m.get("premiereDate")
    raw = p.get("localDate") if isinstance(p, dict) else p
    try:
        d = datetime.fromisoformat(raw).strftime("%d/%m")
    except (TypeError, ValueError):
        d = "?"
    return f"• {d}  {esc(m['title'])}"


def movie_state(m: dict) -> dict:
    p = m.get("premiereDate")
    raw = p.get("localDate") if isinstance(p, dict) else p
    return {"premiere": (raw or "")[:10], "presale": bool(m.get("inPreSale")),
            "playing": bool(m.get("isPlaying"))}


def watch_changes(title: str, old: dict, new: dict) -> list[str]:
    """Human messages for what changed in a followed movie (empty if nothing)."""
    out = []
    if new["playing"] and not old.get("playing"):
        out.append(f"🎬 <b>{esc(title)}</b> entrou em cartaz")
    if new["presale"] and not old.get("presale"):
        out.append(f"🎟 <b>{esc(title)}</b>: pré-venda aberta")
    if new["premiere"] and old.get("premiere") and new["premiere"] != old["premiere"]:
        d = new["premiere"]
        out.append(f"📅 <b>{esc(title)}</b>: estreia agora em {d[8:]}/{d[5:7]}")
    return out


def _fmt_dates(dates: list[str]) -> str:
    ds = [f"{d[8:]}/{d[5:7]}" for d in dates]
    return ds[0] if len(ds) == 1 else ", ".join(ds[:-1]) + " e " + ds[-1]


def format_report(rep: dict) -> list[str]:
    """Weekly report blocks (HTML). Empty sections are omitted."""
    blocks = []
    def lines(rows):
        return "\n".join(f"• {esc(t)} — {_fmt_dates(d)}" for t, d in rows)
    if rep["estreias"]:
        blocks.append("🎬 <b>Estreias</b>\n" + lines(rep["estreias"]))
    if rep["relancamentos"]:
        blocks.append("♻️ <b>Relançamentos</b>\n" + lines(rep["relancamentos"]))
    for label, rows in sorted(rep["eventos"].items()):
        blocks.append(f"🎟 <b>{esc(label)}</b>\n" + lines(sorted(rows, key=lambda r: r[1][0])))
    if rep["shows"]:
        blocks.append("🎤 <b>Shows e transmissões</b>\n" + lines(rep["shows"]))
    for tag, titles in sorted(rep["festivais"].items()):
        blocks.append(f"🏷 <b>{esc(tag)}</b>: " + ", ".join(esc(t) for t in sorted(titles)))
    if rep["classicos"]:
        blocks.append("🎞 <b>Clássicos em tela</b>\n" + lines(rep["classicos"]))
    return blocks


def week_report_blocks(start: date | None = None, days: int = 7) -> list[str]:
    start = start or date.today()
    end = (start + timedelta(days=days - 1)).isoformat()
    rep = events.build_report(core.api_coming_soon(), events.scan_grid(days, start),
                              start.isoformat(), end)
    return format_report(rep) or ["<i>Nada de especial mapeado para esta semana.</i>"]


def digest_key(today: date) -> str:
    iso = today.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def digest_due(now: datetime, last_week: str | None) -> bool:
    return (now.weekday() == DIGEST_WEEKDAY and now.hour >= DIGEST_HOUR
            and last_week != digest_key(now.date()))


# ── Data gathering ────────────────────────────────────────────────────────────

def theater_names() -> dict[str, str]:
    return {str(t["id"]): t["name"] for t in core.api_theaters()}


def day_blocks(favs: list[str], date_str: str, now: datetime | None) -> list[str]:
    names = theater_names()
    return [format_theater_day(names.get(f, f), core.api_sessions_by_theater(f, date_str), now)
            for f in favs]


def grid_additions(theater_id: str, days: int = GRID_DAYS) -> dict[str, tuple[str, str]]:
    """movie_id -> (title, first date it shows up) across the next `days` days."""
    found: dict[str, tuple[str, str]] = {}
    for i in range(days):
        d = (date.today() + timedelta(days=i)).isoformat()
        day = core.api_sessions_by_theater(theater_id, d)
        for m in (day or {}).get("movies", []):
            found.setdefault(str(m["id"]), (m["title"], d))
    return found


# ── Telegram I/O ──────────────────────────────────────────────────────────────

class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = requests.Session()

    def call(self, method, _timeout=15, **params):
        r = self.http.post(f"{self.base}/{method}", json=params, timeout=_timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description')}")
        return data["result"]

    def send(self, chat_id, text, **kw):
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         disable_web_page_preview=True, **kw)

    def send_all(self, chat_id, blocks, **kw):
        for part in chunk(blocks):
            self.send(chat_id, part, **kw)

    def updates(self, offset):
        return self.call("getUpdates", _timeout=40, offset=offset, timeout=30)


# ── Handlers ──────────────────────────────────────────────────────────────────

HELP = (
    "<b>cinema-br</b> — sessões rápidas\n\n"
    "/cinemas — escolher seus cinemas favoritos\n"
    "/hoje · /amanha · /sessoes [data]\n"
    "/filme &lt;título&gt; [data]\n"
    "/embreve — próximas estreias\n"
    "/semana — estreias, eventos e relançamentos dos próximos 7 dias\n"
    "/seguir &lt;título&gt; · /seguindo · /parar &lt;título&gt; — filmes que você espera\n"
    "/avisos — ligar/desligar notificações"
)


def cinemas_keyboard(favs: list[str]) -> dict:
    rows = [[{"text": ("✅ " if str(t["id"]) in favs else "⬜ ") + t["name"],
              "callback_data": f"t:{t['id']}"}] for t in core.api_theaters()]
    return {"inline_keyboard": rows}


class Bot:
    def __init__(self, tg, state, allowed=None):
        self.tg, self.state, self.allowed = tg, state, allowed

    def permitted(self, chat_id) -> bool:
        return not self.allowed or chat_id in self.allowed

    def handle(self, update: dict):
        if "callback_query" in update:
            return self.on_callback(update["callback_query"])
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        if not text or chat_id is None or not self.permitted(chat_id):
            return
        self.state.ensure(chat_id)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        try:
            fn = {
                "/start": self.c_help, "/ajuda": self.c_help, "/help": self.c_help,
                "/cinemas": self.c_cinemas,
                "/hoje": lambda c, a: self.c_sessoes(c, ""),
                "/amanha": lambda c, a: self.c_sessoes(c, "amanha"),
                "/sessoes": self.c_sessoes,
                "/filme": self.c_filme,
                "/embreve": self.c_embreve,
                "/semana": self.c_semana,
                "/avisos": self.c_avisos,
                "/seguir": self.c_seguir,
                "/seguindo": self.c_seguindo,
                "/parar": self.c_parar,
            }.get(cmd)
            if fn:
                fn(chat_id, arg)
        except core.APIError:
            self.tg.send(chat_id, "ingresso.com não respondeu agora. Tente de novo em instantes.")

    def c_help(self, chat_id, arg):
        self.tg.send(chat_id, HELP)

    def c_cinemas(self, chat_id, arg):
        self.tg.send(chat_id, "Toque para marcar/desmarcar seus cinemas:",
                     reply_markup=cinemas_keyboard(self.state.favs(chat_id)))

    def on_callback(self, cb):
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        data = cb.get("data") or ""
        if chat_id is None or not self.permitted(chat_id) or not data.startswith("t:"):
            return
        favs = self.state.toggle_fav(chat_id, data[2:])
        self.tg.call("answerCallbackQuery", callback_query_id=cb["id"])
        self.tg.call("editMessageReplyMarkup", chat_id=chat_id,
                     message_id=cb["message"]["message_id"],
                     reply_markup=cinemas_keyboard(favs))

    def _need_favs(self, chat_id):
        favs = self.state.favs(chat_id)
        if not favs:
            self.tg.send(chat_id, "Você ainda não escolheu cinemas. Use /cinemas.")
        return favs

    def c_sessoes(self, chat_id, arg):
        favs = self._need_favs(chat_id)
        if not favs:
            return
        date_str, _ = core.resolve_date(arg or None)
        now = datetime.now(TZ) if date_str == date.today().isoformat() else None
        self.tg.send_all(chat_id, [f"🎬 <b>{esc(date_str)}</b>"] + day_blocks(favs, date_str, now))

    def c_filme(self, chat_id, arg):
        if not arg:
            return self.tg.send(chat_id, "Uso: /filme &lt;título&gt; [amanha|+N|AAAA-MM-DD]")
        words = arg.split()
        date_arg = None
        if len(words) > 1 and (words[-1].lower() == "amanha" or words[-1].startswith("+")
                               or (len(words[-1]) == 10 and words[-1][4] == "-")):
            date_arg = words.pop()
        movie = core.find_movie(" ".join(words), core.api_movies_in_city())
        if not movie:
            return self.tg.send(chat_id, f"Não achei “{esc(' '.join(words))}” em cartaz.")
        date_str, explicit = core.resolve_date(date_arg)
        day = core.api_sessions(movie["id"], date_str)
        if not (day and day.get("theaters")) and not explicit:
            dates = core.api_session_dates(movie["id"]) or []
            if dates:
                date_str = dates[0]["date"]
                day = core.api_sessions(movie["id"], date_str)
        if not (day and day.get("theaters")):
            return self.tg.send(chat_id, f"Sem sessões de {esc(movie['title'])} em {esc(date_str)}.")
        favs = self.state.favs(chat_id)
        theaters = day["theaters"]
        mine = [t for t in theaters if str(t.get("id")) in favs]
        shown = mine or theaters
        now = datetime.now(TZ) if date_str == date.today().isoformat() else None
        blocks = [f"🎬 <b>{esc(movie['title'])}</b> · {esc(date_str)}"
                  + ("" if mine or not favs else "\n<i>nenhum favorito exibe; mostrando todos</i>")]
        for t in shown:
            times = format_movie_times(t, now)
            blocks.append(f"<b>{esc(t['name'])}</b>\n{times or '<i>sessões encerradas</i>'}")
        self.tg.send_all(chat_id, blocks)

    def c_embreve(self, chat_id, arg):
        items = core.api_coming_soon()[:25]
        if not items:
            return self.tg.send(chat_id, "Nada em breve no momento.")
        self.tg.send(chat_id, "<b>Próximas estreias</b>\n" + "\n".join(format_premiere(m) for m in items))

    def _catalog(self) -> dict[str, dict]:
        cat = {str(m["id"]): m for m in core.api_movies()}
        cat.update({str(m["id"]): m for m in core.api_coming_soon()})
        return cat

    def c_seguir(self, chat_id, arg):
        if not arg:
            return self.tg.send(chat_id, "Uso: /seguir &lt;título&gt;")
        movie = core.find_movie(arg, list(self._catalog().values()))
        if not movie:
            return self.tg.send(chat_id, f"Não achei “{esc(arg)}”.")
        self.state.watch_add(chat_id, str(movie["id"]), movie["title"], movie_state(movie))
        self.tg.send(chat_id, f"⭐ Seguindo <b>{esc(movie['title'])}</b>. Aviso quando mudar a "
                              "estreia, abrir a pré-venda ou chegar aos seus cinemas.")

    def c_seguindo(self, chat_id, arg):
        items = self.state.watch_list(chat_id)
        if not items:
            return self.tg.send(chat_id, "Você não segue nenhum filme. Use /seguir &lt;título&gt;.")
        self.tg.send(chat_id, "<b>Seguindo</b>\n" + "\n".join(f"⭐ {esc(m['title'])}" for m in items))

    def c_parar(self, chat_id, arg):
        movie = core.find_movie(arg, self.state.watch_list(chat_id)) if arg else None
        if not movie:
            return self.tg.send(chat_id, "Uso: /parar &lt;título&gt; (veja /seguindo).")
        self.state.watch_remove(chat_id, movie["id"])
        self.tg.send(chat_id, f"Parei de seguir <b>{esc(movie['title'])}</b>.")

    def c_semana(self, chat_id, arg):
        self.tg.send_all(chat_id, ["📅 <b>Próximos 7 dias</b>"] + week_report_blocks())

    def c_avisos(self, chat_id, arg):
        on = self.state.toggle_notify(chat_id)
        self.tg.send(chat_id, "🔔 Avisos ligados." if on else "🔕 Avisos desligados.")


# ── Background jobs ───────────────────────────────────────────────────────────

def job_new_in_grid(tg, state):
    """Tell each chat about films that newly appeared at its favorite theaters."""
    cache: dict[str, dict] = {}
    for chat_id in state.chats():
        favs = state.favs(chat_id)
        names = theater_names() if favs else {}
        for tid in favs:
            if tid not in cache:
                cache[tid] = grid_additions(tid)
            found = cache[tid]
            known = state.seen(chat_id, tid)
            new = {m: v for m, v in found.items() if m not in known}
            state.add_seen(chat_id, tid, new)
            if known and new and state.notify(chat_id):    # first run seeds silently
                followed = {m["id"] for m in state.watch_list(chat_id)}
                lines = [f"{'⭐' if m in followed else '•'} {esc(t)} (a partir de {d[8:]}/{d[5:7]})"
                         for m, (t, d) in new.items()]
                tg.send(chat_id, f"🆕 <b>Novo em {esc(names.get(tid, tid))}</b>\n" + "\n".join(lines))


def job_new_soon(tg, state):
    soon = core.api_coming_soon()
    known = state.seen_soon()
    new = [m for m in soon if str(m["id"]) not in known]
    state.add_seen_soon(str(m["id"]) for m in new)
    if known and new:                                      # first run seeds silently
        text = "🔜 <b>Novo em breve</b>\n" + "\n".join(format_premiere(m) for m in new[:20])
        for chat_id in state.chats():
            if state.notify(chat_id):
                tg.send(chat_id, text)


def job_watch(tg, state):
    """Notify about changes (premiere date, pre-sale, in theaters) of followed movies."""
    catalog = None
    for chat_id in state.chats():
        items = state.watch_list(chat_id)
        if not items:
            continue
        if catalog is None:
            catalog = {str(m["id"]): m for m in core.api_movies()}
            catalog.update({str(m["id"]): m for m in core.api_coming_soon()})
        for it in items:
            m = catalog.get(it["id"])
            if not m:
                continue
            new = movie_state(m)
            msgs = watch_changes(it["title"], it["state"], new)
            if new != it["state"]:
                state.watch_set_state(chat_id, it["id"], new)
            if msgs and state.notify(chat_id):
                tg.send(chat_id, "\n".join(msgs))


def job_digest(tg, state, now=None):
    now = now or datetime.now(TZ)
    for chat_id in state.chats():
        favs = state.favs(chat_id)
        if not favs or not state.notify(chat_id) or not digest_due(now, state.digest_week(chat_id)):
            continue
        names = theater_names()
        d = now.date().isoformat()
        blocks = ["🗓 <b>Nova semana em cartaz</b>"] + [
            f"<b>{esc(names.get(f, f))}</b>\n{format_titles(core.api_sessions_by_theater(f, d))}"
            for f in favs]
        blocks += ["📅 <b>Esta semana na cidade</b>"] + week_report_blocks(now.date())
        tg.send_all(chat_id, blocks)
        state.set_digest_week(chat_id, digest_key(now.date()))


def background(tg, state, stop: threading.Event):
    while not stop.is_set():
        for job in (job_new_in_grid, job_new_soon, job_watch, job_digest):
            try:
                job(tg, state)
            except Exception as e:                         # a failing job must not kill the loop
                print(f"[job {job.__name__}] {e!r}", flush=True)
        stop.wait(CHECK_EVERY)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Defina TELEGRAM_BOT_TOKEN (crie o bot com @BotFather).")
    allowed = {int(x) for x in os.environ.get("BOT_ALLOWED_IDS", "").split(",") if x.strip()}
    db = os.environ.get("BOT_DB") or str(Path(__file__).with_name("bot_state.db"))
    tg, state = Telegram(token), State(db)
    bot = Bot(tg, state, allowed or None)

    stop = threading.Event()
    threading.Thread(target=background, args=(tg, state, stop), daemon=True).start()

    offset = 0
    print("bot no ar", flush=True)
    while True:
        try:
            for u in tg.updates(offset):
                offset = u["update_id"] + 1
                try:
                    bot.handle(u)
                except Exception as e:
                    print(f"[handler] {e!r}", flush=True)
        except KeyboardInterrupt:
            stop.set()
            break
        except Exception as e:
            print(f"[poll] {e!r}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
