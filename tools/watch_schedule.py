#!/usr/bin/env python3
"""
watch_schedule.py — descobre QUANDO o ingresso.com publica a programação da
semana seguinte.

Arquivo único, só biblioteca padrão (Python 3.8+): copie para a VPS e rode.
A cada rodada, para cada cinema da cidade, anota quantos filmes (e quais) já
têm sessões nos primeiros dias da PRÓXIMA semana de cinema (quinta, sexta e
sábado). Quando esse conjunto muda, registra o evento com a hora exata.

Uso:
  python3 watch_schedule.py poll                # uma rodada (para o cron)
  python3 watch_schedule.py loop --interval 1200  # rodadas contínuas
  python3 watch_schedule.py status              # saúde da coleta
  python3 watch_schedule.py report              # análise: quando a grade saiu
  python3 watch_schedule.py backup copia.db     # cópia consistente do banco

Cron (a cada 20 min):
  */20 * * * * /usr/bin/python3 /caminho/watch_schedule.py poll >> /caminho/watch.log 2>&1

Dados: schedule_watch.db ao lado do script (--db para mudar). É o único
arquivo que precisa voltar para análise.
"""
import argparse
import datetime as dt
import fcntl
import gzip
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

API = "https://api-content.ingresso.com/v0"
PARTNERSHIP = "ingresso.com"
DEFAULT_CITY = 36                      # Fortaleza
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "cinema-schedule-watch/1.0 (consulta pessoal; 1 rodada a cada ~20 min)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS poll (
    id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL, ts_local TEXT NOT NULL,
    city_id INTEGER NOT NULL, errors INTEGER NOT NULL DEFAULT 0
);
-- uma linha por (rodada, cinema, data): o que existe publicado naquele dia
CREATE TABLE IF NOT EXISTS obs (
    poll_id INTEGER NOT NULL REFERENCES poll(id),
    theater_id TEXT NOT NULL, theater TEXT NOT NULL, date TEXT NOT NULL,
    status TEXT NOT NULL,              -- ok | empty | error:<motivo>
    n_movies INTEGER NOT NULL DEFAULT 0, n_sessions INTEGER NOT NULL DEFAULT 0,
    movie_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS obs_key ON obs(theater_id, date, poll_id);
CREATE TABLE IF NOT EXISTS titles (movie_id TEXT PRIMARY KEY, title TEXT NOT NULL);
-- mudanças entre duas rodadas consecutivas válidas do mesmo cinema/data
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, poll_id INTEGER NOT NULL, ts_utc TEXT NOT NULL, ts_local TEXT NOT NULL,
    theater_id TEXT NOT NULL, theater TEXT NOT NULL, date TEXT NOT NULL,
    kind TEXT NOT NULL,                -- movie_added | movie_removed
    movie_id TEXT NOT NULL, title TEXT
);
"""


# ── tempo ─────────────────────────────────────────────────────────────────────

def tz(args):
    return dt.timezone(dt.timedelta(hours=args.tz_offset))

def now_pair(args):
    utc = dt.datetime.now(dt.timezone.utc)
    return utc.isoformat(timespec="seconds"), utc.astimezone(tz(args)).isoformat(timespec="seconds")

def next_thursday(today):
    """Quinta-feira estritamente depois de hoje: começo da próxima semana de cinema."""
    return today + dt.timedelta(days=(3 - today.weekday()) % 7 or 7)

def target_dates(args):
    today = dt.datetime.now(tz(args)).date()
    start = next_thursday(today)
    dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(3)]
    dates += [d for d in (args.extra_dates or "").split(",") if d]
    return dates


# ── HTTP ──────────────────────────────────────────────────────────────────────

def get_json(path, params, timeout=20):
    """Devolve (dados, erro). 204/404 => ([], None): 'sem programação'."""
    url = API + path + "?" + "&".join("%s=%s" % kv for kv in params.items())
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                if r.status == 204 or not raw:
                    return [], None
                return json.loads(raw.decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code in (204, 404):
                return [], None
            last = "http%d" % e.code
        except Exception as e:                      # rede, timeout, JSON ruim
            last = type(e).__name__
        time.sleep(2)
    return None, last


# ── banco ─────────────────────────────────────────────────────────────────────

def connect(args):
    con = sqlite3.connect(args.db, timeout=30)
    con.executescript(SCHEMA)
    return con

def lock(args):
    f = open(args.db + ".lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("outra rodada em andamento; saindo")
        sys.exit(0)
    return f                                         # manter referência viva


def previous_obs(con, theater_id, date):
    """Última observação VÁLIDA (ok/empty) anterior para o cinema/data, ou None."""
    row = con.execute(
        "SELECT movie_ids FROM obs WHERE theater_id=? AND date=? AND status IN ('ok','empty') "
        "ORDER BY poll_id DESC LIMIT 1", (theater_id, date)).fetchone()
    return set(json.loads(row[0])) if row else None


# ── coleta ────────────────────────────────────────────────────────────────────

def do_poll(args):
    ts_utc, ts_local = now_pair(args)
    con = connect(args)
    theaters, err = get_json("/theaters/city/%d" % args.city_id, {"partnership": PARTNERSHIP})
    if err or not theaters:
        print("[%s] ERRO ao listar cinemas: %s" % (ts_local, err or "lista vazia"))
        con.close()
        return 1
    dates = target_dates(args)
    cur = con.execute("INSERT INTO poll(ts_utc, ts_local, city_id) VALUES (?,?,?)",
                      (ts_utc, ts_local, args.city_id))
    poll_id, errors, changes = cur.lastrowid, 0, 0

    for t in sorted(theaters, key=lambda x: x.get("name", "")):
        tid, name = str(t["id"]), t.get("name", "?")
        for d in dates:
            data, err = get_json(
                "/sessions/city/%d/theater/%s/partnership/%s/groupBy/sessionType"
                % (args.city_id, tid, PARTNERSHIP), {"date": d})
            time.sleep(args.pause)
            if err:
                errors += 1
                con.execute("INSERT INTO obs(poll_id,theater_id,theater,date,status) VALUES (?,?,?,?,?)",
                            (poll_id, tid, name, d, "error:" + err))
                continue
            movies = (data[0].get("movies") if data and isinstance(data[0], dict) else None) or []
            ids, n_sessions = [], 0
            for m in movies:
                mid = str(m["id"])
                ids.append(mid)
                con.execute("INSERT OR REPLACE INTO titles(movie_id,title) VALUES (?,?)", (mid, m.get("title", "")))
                n_sessions += sum(len(st.get("sessions", [])) for st in m.get("sessionTypes", []))
            prev = previous_obs(con, tid, d)
            if prev is not None:
                now_set = set(ids)
                for kind, group in (("movie_added", now_set - prev), ("movie_removed", prev - now_set)):
                    for mid in sorted(group):
                        title = (con.execute("SELECT title FROM titles WHERE movie_id=?", (mid,)).fetchone() or [None])[0]
                        con.execute("INSERT INTO events(poll_id,ts_utc,ts_local,theater_id,theater,date,kind,movie_id,title) "
                                    "VALUES (?,?,?,?,?,?,?,?,?)", (poll_id, ts_utc, ts_local, tid, name, d, kind, mid, title))
                        changes += 1
            con.execute("INSERT INTO obs(poll_id,theater_id,theater,date,status,n_movies,n_sessions,movie_ids) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (poll_id, tid, name, d, "ok" if ids else "empty", len(ids), n_sessions, json.dumps(sorted(ids))))
    con.execute("UPDATE poll SET errors=? WHERE id=?", (errors, poll_id))
    con.commit()
    con.close()
    print("[%s] rodada %d: %d cinemas x %d datas (%s..%s) | mudanças: %d | erros: %d"
          % (ts_local, poll_id, len(theaters), len(dates), dates[0], dates[-1], changes, errors))
    return 0

def cmd_poll(args):
    _lock = lock(args)
    return do_poll(args)

def cmd_loop(args):
    _lock = lock(args)
    while True:
        try:
            do_poll(args)
        except Exception as e:                       # nunca morrer por um erro isolado
            print("rodada falhou: %r" % (e,))
        time.sleep(args.interval)


# ── consulta ──────────────────────────────────────────────────────────────────

def cmd_status(args):
    con = connect(args)
    n, first, last, errs = con.execute("SELECT COUNT(*), MIN(ts_local), MAX(ts_local), SUM(errors) FROM poll").fetchone()
    print("rodadas: %d | primeira: %s | última: %s | erros acumulados: %s" % (n, first, last, errs or 0))
    print("eventos registrados:", con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    row = con.execute("SELECT MAX(id) FROM poll").fetchone()[0]
    if row:
        print("\núltima rodada:")
        for r in con.execute("SELECT theater, date, status, n_movies, n_sessions FROM obs WHERE poll_id=? ORDER BY theater, date", (row,)):
            print("  %-34s %s  %-6s filmes=%-3d sessões=%d" % r)
    return 0

def cmd_report(args):
    con = connect(args)
    dias = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    fmt = lambda s: (lambda d: "%s %s" % (dias[d.weekday()], d.strftime("%d/%m %H:%M")))(dt.datetime.fromisoformat(s))

    print("== Evolução do nº de filmes publicados, por cinema/data (só quando muda) ==")
    rows = con.execute("SELECT o.theater, o.date, p.ts_local, o.n_movies, o.status FROM obs o JOIN poll p ON p.id=o.poll_id "
                       "WHERE o.status IN ('ok','empty') ORDER BY o.theater, o.date, p.id").fetchall()
    series = {}
    for theater, date, ts, n, _ in rows:
        s = series.setdefault((theater, date), [])
        if not s or s[-1][1] != n:
            s.append((ts, n))
    for (theater, date), s in sorted(series.items()):
        steps = " -> ".join("%d (%s)" % (n, fmt(ts)) for ts, n in s)
        print("  %-34s %s  %s" % (theater, date, steps))

    print("\n== Estimativa de quando a grade 'saiu' (1º momento com >= 60% do total final e >= 5 filmes) ==")
    for (theater, date), s in sorted(series.items()):
        final = s[-1][1]
        hit = next((ts for ts, n in s if final >= 5 and n >= 0.6 * final), None)
        if hit:
            print("  %-34s %s  %s  (final: %d filmes)" % (theater, date, fmt(hit), final))

    print("\n== Filmes adicionados por dia da semana / hora (hora local) ==")
    hist = {}
    for (ts,) in con.execute("SELECT ts_local FROM events WHERE kind='movie_added'"):
        d = dt.datetime.fromisoformat(ts)
        hist[(d.weekday(), d.hour)] = hist.get((d.weekday(), d.hour), 0) + 1
    for (wd, h), n in sorted(hist.items()):
        print("  %s %02dh: %d" % (dias[wd], h, n))
    if not hist:
        print("  (nenhuma adição registrada ainda)")
    return 0

def cmd_backup(args):
    src = connect(args)
    dst = sqlite3.connect(args.out)
    src.backup(dst)                                  # cópia consistente, mesmo com o cron rodando
    dst.close()
    src.close()
    print("backup salvo em", args.out, "(%d bytes)" % os.path.getsize(args.out))
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=os.path.join(HERE, "schedule_watch.db"))
    p.add_argument("--city-id", type=int, default=DEFAULT_CITY, help="padrão: 36 (Fortaleza)")
    p.add_argument("--tz-offset", type=int, default=-3, help="fuso do horário local em horas (padrão -3)")
    p.add_argument("--pause", type=float, default=0.3, help="pausa entre requisições, em segundos")
    p.add_argument("--extra-dates", default="", help="datas extras AAAA-MM-DD, separadas por vírgula")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("poll")
    pl = sub.add_parser("loop"); pl.add_argument("--interval", type=int, default=1200)
    sub.add_parser("status")
    sub.add_parser("report")
    pb = sub.add_parser("backup"); pb.add_argument("out")
    args = p.parse_args()
    fn = {"poll": cmd_poll, "loop": cmd_loop, "status": cmd_status, "report": cmd_report, "backup": cmd_backup}.get(args.cmd)
    if not fn:
        p.print_help()
        return 2
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
