"""Serwer MCP (oficjalny SDK, streamable HTTP) pod http://127.0.0.1:8770/mcp.

Poziom z config.yaml (mcp_level): read < control < full. Zestaw narzędzi i ich opisy są budowane
z definicji kafelków i odświeżane po każdej zmianie plików konfiguracji.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import editor
from .tiles import session_env

LEVELS = {"read": 0, "control": 1, "full": 2}

INSTRUCTIONS = """Command Center Jana: zasoby komputera (CPU, RAM, VRAM RTX 3060 12 GB), procesy, Docker
i uruchamianie lokalnych usług AI (llama-server, Strata, RAG, kokoro-tts) oraz aplikacji z kafelków.
Zanim coś uruchomisz, sprawdź stan (get_status) i plan (plan_start). Start, który odsuwa inne usługi,
wymaga confirm=true: najpierw pokaż plan użytkownikowi i zapytaj o zgodę."""


class CCMcp:
    def __init__(self, st) -> None:
        self.st = st
        self.server = MCPServer(name="command-center", title="Command Center", version="0.2.0",
                                instructions=INSTRUCTIONS)
        self.registered: list[str] = []
        self.extra: list[tuple] = []     # narzędzia z wtyczek: (fn, level, description, read_only, destructive)
        self._stop = asyncio.Event()
        self.sync()
        st.on_reload.append(self.sync)

    # ---------- montowanie w aplikacji NiceGUI ----------
    def mount(self, app) -> None:
        starlette_app = self.server.streamable_http_app(streamable_http_path="/mcp")
        for route in reversed(starlette_app.routes):
            app.router.routes.insert(0, route)
        app.on_startup(self._run)
        app.on_shutdown(self._stop.set)

    async def _run(self) -> None:
        async def keep() -> None:
            async with self.server.session_manager.run():
                await self._stop.wait()
        asyncio.create_task(keep())

    # ---------- narzędzia ----------
    def _add(self, fn: Callable, level: str, description: str, read_only: bool = False,
             destructive: bool = False) -> None:
        if LEVELS[self.st.config.mcp_level] < LEVELS[level]:
            return
        name = fn.__name__
        st = self.st

        @functools.wraps(fn)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            bound = inspect.signature(fn).bind(*args, **kwargs)
            bound.apply_defaults()
            call_args = {k: v for k, v in bound.arguments.items()}
            try:
                res = fn(*args, **kwargs)
                if inspect.isawaitable(res):
                    res = await res
            except ToolError as e:
                st.actions.add("mcp", name, call_args, str(e), False)
                raise
            except Exception as e:
                st.actions.add("mcp", name, call_args, f"{type(e).__name__}: {e}", False)
                raise ToolError(f"{type(e).__name__}: {e}") from e
            if not read_only:
                st.actions.add("mcp", name, call_args, res, True)
            else:
                st.actions.add("mcp", name, call_args, "odczyt", True)
            return res

        self.server.add_tool(wrapped, name=name, description=description, structured_output=False,
                             annotations=ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive))
        self.registered.append(name)

    def catalog(self) -> str:
        lines = []
        for t in self.st.registry.tiles.values():
            first = (t.help.strip().splitlines() or [""])[0]
            opts = ", ".join(f"{o.id}={o.default!r}" for o in t.options if not o.advanced)
            presets = ", ".join(repr(p) for p in t.presets)
            line = f"- {t.id} ({t.type}): {t.title}. {first}"
            if opts:
                line += f" Opcje: {opts}."
            if presets:
                line += f" Presety: {presets}."
            lines.append(line)
        return "\n".join(lines)

    def profiles_catalog(self) -> str:
        return "\n".join(f"- {p.id}: {p.title}. {p.help}" for p in self.st.registry.profiles.values())

    def sync(self) -> None:
        for name in self.registered:
            try:
                self.server.remove_tool(name)
            except ToolError:
                pass
        self.registered = []
        st = self.st
        cat = self.catalog()

        def tile(tile_id: str):
            t = st.registry.tiles.get(tile_id)
            if not t:
                raise ToolError(f"Nie ma kafelka {tile_id!r}. Dostępne: {', '.join(st.registry.tiles)}")
            return t

        # ----- odczyt -----
        def get_status() -> dict:
            s = st.monitor.snapshot
            g = (s.get("gpus") or [None])[0]
            return {
                "cpu_percent": s.get("cpu", {}).get("total"),
                "ram_mib": s.get("mem"), "swap_mib": s.get("swap"),
                "gpu": g and {k: g[k] for k in ("name", "mem_used", "mem_total", "util", "temp", "power_w")},
                "vram_owners": st.owners(),
                "active_profile": st.store.data.get("profile"),
                "tiles": {t.id: st.status(t) for t in st.registry.tiles.values()},
            }
        self._add(get_status, "read", "Stan komputera: CPU, RAM, VRAM z podziałem na właścicieli, "
                  "stan każdego kafelka i aktywny profil.", read_only=True)

        def list_tiles() -> dict:
            return {"tiles": [{"id": t.id, "title": t.title, "type": t.type, "group": t.group,
                               **st.tile_info(t), "budget_mib": _safe_budget(t),
                               "presets": list(t.presets), "help": t.help.strip()}
                              for t in st.registry.tiles.values()]}
        self._add(list_tiles, "read", "Lista kafelków (usługi, aplikacje, kontenery, komendy) ze stanem i VRAM.\n"
                  + cat, read_only=True)

        def describe_tile(tile_id: str) -> dict:
            t = tile(tile_id)
            return {"tile": t.model_dump(by_alias=True, exclude={"source"}), **st.tile_info(t),
                    "command_preview": st.preview(t, t.defaults()), "budget_mib": _safe_budget(t)}
        self._add(describe_tile, "read", "Pełny opis kafelka: help, opcje z opisami, presety, VRAM, zależności "
                  "on_start i podgląd komendy dla wartości domyślnych.", read_only=True)

        def list_processes(vram_only: bool = False, query: str = "", limit: int = 30) -> dict:
            rows = [p for p in st.monitor.processes if (p["vram"] or not vram_only)
                    and (not query or query.lower() in (p["name"] + " " + p["cmd"]).lower())]
            rows.sort(key=lambda p: (-p["vram"], -p["cpu"], -p["rss"]))
            return {"processes": [{k: p[k] for k in ("pid", "name", "user", "cpu", "rss", "vram", "cmd")}
                                  for p in rows[:limit]],
                    "units": "cpu w %, rss i vram w MiB"}
        self._add(list_processes, "read", "Procesy posortowane po VRAM, CPU, RAM. query filtruje po nazwie "
                  "i linii poleceń.", read_only=True)

        def list_containers() -> dict:
            return {"containers": st.docker.containers, "error": st.docker.error}
        self._add(list_containers, "read", "Kontenery Dockera z projektem compose i stanem.", read_only=True)

        async def tail_log(tile_id: str, lines: int = 60) -> dict:
            t = tile(tile_id)
            job = st.runner.jobs.get(t.id)
            out: dict[str, Any] = {"run_output": list(job.lines)[-lines:] if job else []}
            if t.log:
                p = Path(t.log).expanduser()
                if p.exists():
                    out["log_file"] = str(p)
                    out["log_tail"] = p.read_text(errors="replace").splitlines()[-lines:]
            if t.container:
                out["container_log"] = (await asyncio.to_thread(st.docker.logs, t.container, lines)).splitlines()
            return out
        self._add(tail_log, "read", "Ostatnie linie: wyjście uruchomienia z panelu, plik log usługi "
                  "albo logi kontenera.", read_only=True)

        def plan_start(tile_id: str, preset: str | None = None, values: dict[str, Any] | None = None) -> dict:
            tile(tile_id)
            return st.plan(tile_id, preset, values).to_dict()
        self._add(plan_start, "read", "Plan arbitra VRAM dla startu kafelka (bez wykonania): kogo odsunie, "
                  "ile VRAM potrzeba i ile zostanie. values = opcje kafelka (patrz describe_tile).", read_only=True)

        def list_profiles() -> dict:
            return {"active": st.store.data.get("profile"),
                    "profiles": [p.model_dump(exclude={"source"}) for p in st.registry.profiles.values()]}
        self._add(list_profiles, "read", "Profile (zestawy usług) i aktywny profil.\n" + self.profiles_catalog(),
                  read_only=True)

        def plan_profile(profile_id: str) -> dict:
            return st.plan_profile(profile_id).to_dict()
        self._add(plan_profile, "read", "Plan przełączenia na profil (bez wykonania).", read_only=True)

        def get_action_log(limit: int = 30, source: str | None = None) -> dict:
            return {"actions": st.actions.recent(limit, source)}
        self._add(get_action_log, "read", "Dziennik akcji (źródło: gui, cli, api, mcp, arbiter).", read_only=True)

        # ----- sterowanie -----
        async def start_tile(tile_id: str, preset: str | None = None, values: dict[str, Any] | None = None,
                             confirm: bool = False) -> dict:
            tile(tile_id)
            return await st.start(tile_id, preset, values, confirm, "mcp")
        self._add(start_tile, "control",
                  "Uruchamia kafelek przez arbitra VRAM. Jeśli start odsunie inne usługi albo brakuje VRAM, "
                  "bez confirm=true zwraca tylko plan (needs_confirm): pokaż go użytkownikowi i dopiero po zgodzie "
                  "wywołaj ponownie z confirm=true. Kafelki:\n" + cat)

        async def stop_tile(tile_id: str) -> dict:
            tile(tile_id)
            return await st.stop(tile_id, "mcp")
        self._add(stop_tile, "control", "Zatrzymuje kafelek; to, co odsunął przy starcie, wraca (on_stop: restore).",
                  destructive=True)

        async def apply_profile(profile_id: str, confirm: bool = False) -> dict:
            return await st.apply_profile(profile_id, confirm, "mcp")
        self._add(apply_profile, "control", "Przełącza na profil. Bez confirm=true zwraca plan do pokazania "
                  "użytkownikowi.\n" + self.profiles_catalog(), destructive=True)

        async def container_action(name: str, action: Literal["start", "stop", "restart"]) -> dict:
            return {"message": await st.container_action(name, action, "mcp")}
        self._add(container_action, "control", "Start / stop / restart kontenera Dockera po nazwie.", destructive=True)

        # ----- pełny -----
        async def kill_process(pid: int, force: bool = False) -> dict:
            return {"message": await st.kill(pid, force, "mcp")}
        self._add(kill_process, "full", "Wysyła SIGTERM (force=true: SIGKILL) do procesu. Procesy usług lepiej "
                  "zatrzymywać przez stop_tile.", destructive=True)

        async def run_command(command: str, timeout_s: int = 60) -> dict:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-lc", command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL, env=session_env(), cwd=str(Path.home()), start_new_session=True)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                return {"exit_code": None, "output": f"przekroczono {timeout_s} s"}
            text = out.decode(errors="replace")
            return {"exit_code": proc.returncode, "output": text[-20000:]}
        self._add(run_command, "full", "Wykonuje komendę bash (powłoka logowania, katalog domowy Jana) "
                  "i zwraca wyjście (do 20 000 znaków).", destructive=True)

        def read_config_file(path: str) -> dict:
            return {"path": path, "content": editor.read(path), "files": editor.list_files()}
        self._add(read_config_file, "full", "Czyta plik konfiguracji panelu (config.yaml, tiles/*.yaml, "
                  "profiles/*.yaml, plugins/*.py).", read_only=True)

        def edit_tile(path: str, content: str) -> dict:
            msg = editor.save(path, content, "mcp")
            st.reload()
            return {"message": msg}
        self._add(edit_tile, "full", "Zapisuje cały plik konfiguracji (np. tiles/ai.yaml). Treść jest walidowana "
                  "przed zapisem, a każda zmiana trafia do git w ~/.config/command-center.", destructive=True)

        for fn, level, description, read_only, destructive in self.extra:
            self._add(fn, level, description, read_only, destructive)

    def add_plugin_tool(self, fn: Callable, level: str = "read", description: str = "",
                        read_only: bool = True, destructive: bool = False) -> None:
        self.extra.append((fn, level, description or (fn.__doc__ or fn.__name__), read_only, destructive))
        self.sync()


def _safe_budget(t) -> int | None:
    try:
        return t.budget()
    except Exception:
        return None
