"""Punkt wejścia: uv run command-center [--port 8770] [--show] [--native]"""
from __future__ import annotations

import argparse

from nicegui import app, ui

from . import api
from . import ui as gui
from .state import State


def main() -> None:
    ap = argparse.ArgumentParser(description="Command Center")
    ap.add_argument("--host", default="127.0.0.1", help="adres nasłuchu (domyślnie tylko lokalnie)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--show", action="store_true", help="otwórz w przeglądarce po starcie")
    ap.add_argument("--native", action="store_true", help="okno natywne (wymaga pywebview)")
    args = ap.parse_args()

    st = State()
    api.register(st)
    gui.register(st)
    app.on_startup(st.start)
    ui.run(host=args.host, port=args.port, title="Command Center", dark=True,
           reload=False, show=args.show, native=args.native)


if __name__ in {"__main__", "__mp_main__"}:
    main()
