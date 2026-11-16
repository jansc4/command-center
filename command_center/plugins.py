"""Wtyczki: ~/.config/command-center/plugins/*.py z funkcją register(ctx).

Plik zaczynający się od "_" jest pomijany (np. _przyklad.py). Wtyczka może:
  ctx.add_collector(fn, every=5)            funkcja (sync/async) wołana w pętli co `every` sekund
  ctx.add_tab("Nazwa", build)               karta w GUI; build() tworzy elementy NiceGUI
  ctx.add_mcp_tool(fn, level="read", ...)   narzędzie MCP (opis z docstringu)
  ctx.state, ctx.app                        rdzeń (State) i aplikacja FastAPI/NiceGUI (własne trasy)
"""
from __future__ import annotations

import importlib.util
import traceback
from typing import Any, Callable

from .config import seed_dir


class PluginContext:
    def __init__(self, st, mcp, app, name: str) -> None:
        self.state, self.mcp, self.app, self.name = st, mcp, app, name

    def add_collector(self, fn: Callable[[], Any], every: float = 5) -> None:
        self.state.collectors.append((every, fn))

    def add_tab(self, title: str, build: Callable[[], None]) -> None:
        TABS.append((title, build))

    def add_mcp_tool(self, fn: Callable, level: str = "read", description: str = "",
                     read_only: bool = True, destructive: bool = False) -> None:
        if self.mcp:
            self.mcp.add_plugin_tool(fn, level, description, read_only, destructive)


TABS: list[tuple[str, Callable[[], None]]] = []
LOADED: list[dict] = []


def load_plugins(st, mcp, app) -> list[dict]:
    d = seed_dir("plugins")
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        entry = {"name": f.stem, "file": str(f), "ok": False, "error": ""}
        try:
            spec = importlib.util.spec_from_file_location(f"cc_plugin_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.register(PluginContext(st, mcp, app, f.stem))
            entry["ok"] = True
        except Exception:
            entry["error"] = traceback.format_exc(limit=3)
            print(f"[plugin {f.stem}] {entry['error']}")
        LOADED.append(entry)
    return LOADED
