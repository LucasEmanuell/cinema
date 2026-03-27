#!/usr/bin/env python3
"""
cinema.py — Entrypoint do CLI. Lógica em cli.py, dados em core.py.

Uso:
  python cinema.py filmes
  python cinema.py sessoes "super mario"
  python cinema.py sessoes "super mario" --data amanha
  python cinema.py sessoes "super mario" --teatro "via sul" --assentos
  python cinema.py assentos <session_id> <section_id>

API web:
  uvicorn app:app --reload
"""
from cli import main

if __name__ == "__main__":
    main()
