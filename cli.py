#!/usr/bin/env python3
"""
cli.py — Command-line interface for cinema-fortaleza.

Usage:
  python cinema.py filmes
  python cinema.py sessoes "super mario"
  python cinema.py sessoes "super mario" --data amanha
  python cinema.py sessoes "super mario" --teatro "via sul" --assentos
  python cinema.py assentos <session_id> <section_id>
"""
import argparse
import sys

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich import box

from core import (
    APIError,
    api_movies, api_session_dates, api_sessions, api_tickets, api_seats,
    api_states, find_movie, normalize, resolve_date, resolve_city, parse_tickets,
)

console = Console()

# ── Display helpers ───────────────────────────────────────────────────────────

def occ_bar(pct: float, width: int = 10) -> Text:
    """Colored occupancy progress bar."""
    filled = round(pct / 100 * width)
    bar    = "█" * filled + "░" * (width - filled)
    color  = "red" if pct >= 80 else ("yellow" if pct >= 50 else "green")
    t = Text()
    t.append(bar, style=color)
    t.append(f" {pct:.0f}%", style="dim")
    return t

def fmt_price(value: float) -> str:
    return f"R$ {value:.2f}".replace(".", ",")

def seat_char(stype: str, available: bool) -> tuple[str, str]:
    """Return (character, style) for a seat based on type and availability."""
    if not available:
        if stype == "SuperSeat":
            return "◆", "red"
        if stype in ("CoupleLeft", "CoupleRight"):
            return "♥", "red"
        return "●", "red"
    match stype:
        case "SuperSeat":                        return "◇", "cyan"
        case "CoupleLeft":                       return "(", "magenta"
        case "CoupleRight":                      return ")", "magenta"
        case "Disability" | "ReducedMobility":   return "W", "blue"
        case "Obese":                            return "O", "blue"
        case "Companion":                        return "c", "blue"
        case _:                                  return "○", "green"

def render_seat_map(data: dict, show_numbers: bool = False):
    """Render a seat-map to the console from a /v1/sessions/.../seats response.

    show_numbers: replace seat symbol with sequential row number (3 chars wide).
    Default mode adds a space after every seat for readable spacing (2 chars wide).
    """
    total      = data.get("totalSeats", 0)
    theater    = data.get("theaterName", "")
    room       = data.get("theaterLocationName", "")
    lines      = data.get("lines", [])
    active_res = data.get("activeReservations", 0)
    stage      = data.get("stage", {})
    labels_raw = data.get("labels", [])

    avail    = sum(1 for ln in lines for s in ln.get("seats", []) if s.get("status") == "Available")
    occupied = total - avail
    pct      = (occupied / total * 100) if total else 0

    # Row letter labels: line_num → letter
    row_labels: dict[int, str] = {}
    for lbl in labels_raw:
        ln = lbl.get("line")
        if ln and ln not in row_labels:
            row_labels[ln] = lbl.get("label", "")

    all_cols = [s["column"] for ln in lines for s in ln.get("seats", [])]
    if not all_cols:
        console.print("[red]Sem dados de assento para esta sessão.[/red]")
        return
    min_col = min(all_cols)
    max_col = max(all_cols)

    stage_left  = stage.get("upperLeft",  {}).get("column", min_col)
    stage_right = stage.get("lowerRight", {}).get("column", max_col)
    stage_line  = stage.get("upperLeft",  {}).get("line",   999)

    # Extend render range so screen banner and seat rows share the same grid
    render_min = min(min_col, stage_left)
    render_max = max(max_col, stage_right)

    # 2 chars per slot (default) or 3 chars (numbers mode)
    slot_w = 3 if show_numbers else 2

    # ── Header ────────────────────────────────────────────────────────────────
    console.print()
    if theater:
        console.print(f"  [bold]{theater}[/bold]  [dim]—[/dim]  {room}")
    console.print(
        f"  Total: [bold]{total}[/bold]  "
        f"Disponíveis: [green]{avail}[/green]  "
        f"Ocupados: [red]{occupied}[/red]"
        + (f"  [yellow]({active_res} reservas ativas)[/yellow]" if active_res else "")
    )
    t = Text("  Lotação: ")
    t.append_text(occ_bar(pct, width=20))
    console.print(t)
    console.print()

    # ── Screen banner ──────────────────────────────────────────────────────────
    first_line_num = min(ln["line"] for ln in lines)
    screen_before  = stage_line <= first_line_num

    def render_screen():
        pad   = (stage_left - render_min) * slot_w
        width = max((stage_right - stage_left + 1) * slot_w - 2, 4)
        label = "TELA".center(width)
        row   = Text()
        row.append("     ")
        row.append(" " * pad)
        row.append("╔" + "═" * width + "╗", style="dim")
        console.print(row)
        row2 = Text()
        row2.append("     ")
        row2.append(" " * pad)
        row2.append("║" + label + "║", style="dim")
        console.print(row2)

    if screen_before:
        render_screen()

    # ── Seat rows ─────────────────────────────────────────────────────────────
    for ln in sorted(lines, key=lambda l: l["line"]):
        seats    = ln.get("seats", [])
        line_num = ln["line"]
        label    = row_labels.get(line_num, str(line_num))

        col_map  = {s["column"]: s for s in seats}
        seat_num = 0

        row = Text()
        row.append(f"  {label:>2} ")

        for col in range(render_min, render_max + 1):
            seat = col_map.get(col)
            if seat is None:
                row.append(" " * slot_w)
            else:
                seat_num += 1
                available = seat.get("status") == "Available"
                ch, style = seat_char(seat.get("type", "Regular"), available)
                if show_numbers:
                    row.append(f"{seat_num:>2} ", style=style)
                else:
                    row.append(ch + " ", style=style)

        console.print(row)

    if not screen_before:
        render_screen()

    # ── Legend ────────────────────────────────────────────────────────────────
    console.print()
    legend = Text("  ")
    if show_numbers:
        for txt, style, lbl in [
            (" 1", "green",   "livre"),
            (" 1", "red",     "ocupado"),
            (" 1", "cyan",    "SuperSeat"),
            (" 1", "magenta", "namorados"),
            (" 1", "blue",    "acessível"),
        ]:
            legend.append(txt, style=style)
            legend.append(f" {lbl}  ", style="dim")
    else:
        for ch, style, lbl in [
            ("○", "green",   "livre"),
            ("●", "red",     "ocupado"),
            ("◇", "cyan",    "SuperSeat"),
            ("()", "magenta","namorados"),
            ("W",  "blue",   "acessível"),
            ("O",  "blue",   "obeso"),
        ]:
            legend.append(ch, style=style)
            legend.append(f" {lbl}  ", style="dim")
    console.print(legend)
    console.print()

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_filmes(args):
    city_id, city_name = resolve_city(args.cidade)
    if city_id is None:
        console.print(f"[red]Cidade não encontrada:[/red] {args.cidade}")
        console.print("[dim]Tente parte do nome, ex: 'recife', 'sao paulo', 'belo horizonte'[/dim]")
        return

    with console.status(f"[dim]Buscando filmes em cartaz em {city_name}...[/dim]"):
        movies = api_movies(city_id)

    if not movies:
        console.print("[red]Nenhum filme encontrado.[/red]")
        return

    table = Table(
        title=f"🎬  Filmes em Cartaz — {city_name}",
        box=box.ROUNDED,
        border_style="bright_black",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("#",        style="dim",  width=3,  justify="right")
    table.add_column("Título",   min_width=32)
    table.add_column("Classif.", justify="center", width=9)
    table.add_column("Duração",  justify="right",  width=8)
    table.add_column("Estreia",  justify="center", width=7)

    for i, m in enumerate(movies[:30], 1):
        premiere     = m.get("premiereDate") or {}
        premiere_str = premiere.get("dayAndMonth", "—") if isinstance(premiere, dict) else "—"
        duration     = m.get("duration", "")
        table.add_row(
            str(i),
            m["title"],
            m.get("contentRating") or "—",
            f"{duration}min" if duration else "—",
            premiere_str,
        )

    console.print()
    console.print(table)
    console.print()
    console.print("[dim]  python cinema.py sessoes \"<título>\"[/dim]")
    console.print()


def cmd_sessoes(args):
    city_id, city_name = resolve_city(args.cidade)
    if city_id is None:
        console.print(f"[red]Cidade não encontrada:[/red] {args.cidade}")
        console.print("[dim]Tente parte do nome, ex: 'recife', 'sao paulo', 'belo horizonte'[/dim]")
        return

    date_str, explicit = resolve_date(args.data)

    with console.status(f"[dim]Buscando filmes em {city_name}...[/dim]"):
        movies = api_movies(city_id)

    movie = find_movie(args.filme, movies)
    if not movie:
        console.print(f"[red]Filme não encontrado:[/red] {args.filme}")
        console.print(f"[dim]Use 'python cinema.py filmes --cidade \"{args.cidade or city_name}\"' para ver o que está em cartaz.[/dim]")
        return

    with console.status(f"[dim]Buscando sessões de {movie['title']}...[/dim]"):
        day = api_sessions(movie["id"], date_str, city_id)

    if not day or not day.get("theaters"):
        dates_raw = api_session_dates(movie["id"], city_id) or []
        if not explicit and dates_raw:
            next_date = dates_raw[0]["date"]
            console.print(
                f"[dim]Sem sessões hoje ({date_str}) — "
                f"mostrando próxima data disponível: {next_date}[/dim]"
            )
            date_str = next_date
            day = api_sessions(movie["id"], date_str, city_id)

        if not day or not day.get("theaters"):
            if dates_raw:
                dates_str = "  ".join(
                    f"{d['date']} ({d['dayOfWeek'][:3]})" for d in dates_raw
                )
                console.print(
                    f"\n[yellow]Sem sessões em {city_name} para {date_str}.[/yellow]\n"
                    f"Datas disponíveis:\n  {dates_str}\n"
                )
            else:
                console.print(f"\n[red]{movie['title']} não está em cartaz em {city_name}.[/red]\n")
            return

    dow      = day.get("dayOfWeek", "")
    date_fmt = day.get("dateFormatted", date_str)
    theaters = day["theaters"]

    # ── Filters ───────────────────────────────────────────────────────────────
    teatro_q = normalize(args.teatro) if args.teatro else None
    hora_q   = args.hora.replace("h", ":").strip() if args.hora else None

    if teatro_q:
        theaters = [t for t in theaters if teatro_q in normalize(t["name"])]
        if not theaters:
            console.print(f"\n[yellow]Nenhum cinema encontrado com:[/yellow] {args.teatro}")
            console.print("[dim]Tente parte do nome, ex: 'via sul', 'iguatemi', 'benfica'[/dim]\n")
            return

    console.print()
    console.rule(
        f"[bold]{movie['title']}[/bold]  [dim]·  {city_name}  ·  {dow}, {date_fmt}[/dim]",
        align="left",
    )
    console.print()

    for theater in theaters:
        if not theater.get("sessionTypes"):
            continue

        enabled = theater.get("enabled", True)
        console.print(Text(f"  {theater['name']}", style="bold" if enabled else "dim"))

        if not enabled and theater.get("blockMessage"):
            console.print(f"  [red]  {theater['blockMessage']}[/red]")
            console.print()
            continue

        for st in theater["sessionTypes"]:
            type_label = " · ".join(st["type"])
            sessions   = st["sessions"]

            if hora_q:
                sessions = [s for s in sessions if s.get("time", "").startswith(hora_q)]
            if not sessions:
                continue

            meia_tip_shown = False
            for s in sessions:
                time_str   = s.get("time", "—")
                room       = s.get("room", "")
                session_id = s["id"]
                section_id = s.get("defaultSector", "")
                s_enabled  = s.get("enabled", True)
                block_msg  = s.get("blockMessage", "")

                line = Text()
                line.append(f"    {time_str:6}", style="bold" if s_enabled else "dim")
                line.append(f"  {type_label:<26}", style="" if s_enabled else "dim")
                if room:
                    line.append(f"  {room}", style="dim")

                # ── Price ─────────────────────────────────────────────────
                if args.precos and section_id:
                    tickets_raw = api_tickets(session_id, section_id)
                    parsed      = parse_tickets(tickets_raw)
                    inteira     = parsed["inteira"]
                    if inteira:
                        line.append(f"  {fmt_price(inteira['price'])}")
                        line.append(f" + {fmt_price(inteira['service'])} taxa", style="dim")
                        line.append(f" = {fmt_price(inteira['total'])}", style="bold")
                    else:
                        line.append(f"  {fmt_price(s.get('price', 0))}", style="dim")
                else:
                    if s.get("price"):
                        line.append(f"  {fmt_price(s['price'])}", style="dim")

                # ── Occupancy ─────────────────────────────────────────────
                seats_raw = None
                if (args.ocupacao or args.assentos) and section_id:
                    seats_raw = api_seats(session_id, section_id)

                if args.ocupacao and seats_raw:
                    total_s = seats_raw.get("totalSeats", 0)
                    avail   = sum(
                        1 for ln in seats_raw.get("lines", [])
                        for seat in ln.get("seats", [])
                        if seat.get("status") == "Available"
                    )
                    pct = ((total_s - avail) / total_s * 100) if total_s else 0
                    line.append("  ")
                    line.append_text(occ_bar(pct))
                    line.append(f" ({avail} livres)", style="dim")

                # ── IDs (developer mode) ──────────────────────────────────
                if args.ids and section_id:
                    line.append(f"  session={session_id}  section={section_id}", style="dim")

                if not s_enabled and block_msg:
                    line.append(f"  {block_msg}", style="red")

                console.print(line)

                # ── Meia tip (once per session type) ──────────────────────
                if args.precos and section_id and not meia_tip_shown:
                    parsed = parse_tickets(api_tickets(session_id, section_id))
                    meia   = parsed["meia"]
                    if meia and meia.get("price", 0) > 0:
                        fee_pct = meia["service"] / meia["price"] * 100
                        if fee_pct > 20:
                            console.print(
                                f"           ⚠ Meia-Entrada: {fmt_price(meia['price'])} "
                                f"+ {fmt_price(meia['service'])} taxa ({fee_pct:.0f}%) "
                                f"— considere comprar na bilheteria",
                                style="dim yellow",
                            )
                            meia_tip_shown = True

                # ── Seat map (inline) ─────────────────────────────────────
                if args.assentos and seats_raw:
                    render_seat_map(seats_raw, show_numbers=args.numeros)

        console.print()

    # Footer hints
    hints = []
    if not args.teatro:
        hints.append("--teatro \"nome\"  para filtrar por cinema")
    if not args.precos:
        hints.append("--precos  taxa de serviço e dica de bilheteria")
    if not args.ocupacao and not args.assentos:
        hints.append("--ocupacao  lotação em tempo real")
    if not args.assentos:
        hints.append("--assentos  mapa da sala (use com --teatro)")
    if not args.ids:
        hints.append("--ids  mostra session/section IDs")
    if hints:
        console.print(Text("  Dicas: " + "  |  ".join(hints), style="dim"))
    console.print()


def cmd_assentos(args):
    with console.status("[dim]Buscando mapa de assentos...[/dim]"):
        data = api_seats(args.session_id, args.section_id)

    if not data:
        console.print("[red]Sessão não encontrada ou sem seleção de assentos.[/red]")
        return

    render_seat_map(data, show_numbers=args.numeros)

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="cinema",
        description="Sessões de cinema no Brasil — rápido e sem enrolação.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
exemplos:
  python cinema.py filmes
  python cinema.py filmes --cidade "recife"
  python cinema.py sessoes "super mario"
  python cinema.py sessoes "super mario" --cidade "sao paulo"
  python cinema.py sessoes "super mario" --data amanha
  python cinema.py sessoes "super mario" --data 2026-04-01
  python cinema.py sessoes "velhos bandidos" --precos
  python cinema.py sessoes "super mario" --teatro "via sul" --assentos
  python cinema.py assentos 84262624 5472265
        """,
    )
    sub = parser.add_subparsers(dest="cmd", metavar="comando")

    pf = sub.add_parser("filmes", help="Lista filmes em cartaz")
    pf.add_argument("--cidade", metavar="CIDADE",
                    help="Cidade (padrão: Fortaleza). Parcial, sem acento, ex: 'recife', 'sao paulo'")

    p = sub.add_parser("sessoes", help="Sessões de um filme")
    p.add_argument("filme",      help="Título (ou parte) do filme")
    p.add_argument("--cidade",   metavar="CIDADE",
                   help="Cidade (padrão: Fortaleza). Parcial, sem acento, ex: 'recife', 'sao paulo'")
    p.add_argument("--data",     metavar="DATA",
                   help="Data: YYYY-MM-DD, 'amanha', '+1', '+2'… (padrão: hoje, auto-avança se vazio)")
    p.add_argument("--precos",   action="store_true",
                   help="Mostra taxa de serviço e alerta quando vale a pena comprar na bilheteria")
    p.add_argument("--ocupacao", action="store_true",
                   help="Mostra lotação em tempo real (faz requisições adicionais)")
    p.add_argument("--teatro",   metavar="NOME",
                   help="Filtra por nome do cinema (parcial, sem acento)")
    p.add_argument("--hora",     metavar="HH:MM",
                   help="Filtra por horário, ex: '20:00' ou '20'")
    p.add_argument("--assentos", action="store_true",
                   help="Mostra mapa de assentos inline (recomendado usar com --teatro)")
    p.add_argument("--numeros",  action="store_true",
                   help="No mapa de assentos, mostra o número do assento ao invés do símbolo")
    p.add_argument("--ids",      action="store_true",
                   help="Mostra session_id e section_id (modo desenvolvedor)")

    p2 = sub.add_parser("assentos", help="Mapa de assentos de uma sessão")
    p2.add_argument("session_id", help="ID da sessão (obter com sessoes --ids)")
    p2.add_argument("section_id", help="ID da seção (obter com sessoes --ids)")
    p2.add_argument("--numeros",  action="store_true",
                    help="Mostra o número do assento ao invés do símbolo")

    args = parser.parse_args()

    try:
        if args.cmd == "filmes":
            cmd_filmes(args)
        elif args.cmd == "sessoes":
            cmd_sessoes(args)
        elif args.cmd == "assentos":
            cmd_assentos(args)
        else:
            parser.print_help()
    except APIError as e:
        console.print(f"[red]Erro de conexão:[/red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
