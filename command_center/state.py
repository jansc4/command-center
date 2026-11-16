"""Wspólny stan rdzenia: monitor, Docker, kafelki, arbiter, dziennik. Używany przez GUI, API i MCP."""
from __future__ import annotations

import asyncio
import os
import re
import signal
import time
import urllib.request
from typing import Any, Awaitable, Callable

import psutil

from . import arbiter
from .actions import ActionLog
from .config import StateStore, config_path, git_init, load_config
from .dockerctl import DockerCtl
from .monitor import Monitor
from .terminal import Terminal
from .tiles import Registry, Runner, Tile, pid_from_file, render, script

_DOCKER_CGROUP = re.compile(r"docker[-/]([0-9a-f]{64})")
STARTUP_GRACE = 300   # s: tyle kafelek z health może się ładować, zanim dostanie stan „nie odpowiada”
RESTORE_GRACE = 90    # s: po starcie nie przywracamy odsuniętych, nawet gdy kafelek jeszcze nie wstał


class State:
    def __init__(self) -> None:
        self.config = load_config()
        self._config_mtime = self._cfg_mtime()
        self.monitor = Monitor()
        self.docker = DockerCtl()
        self.registry = Registry()
        self.runner = Runner()
        self.store = StateStore()
        self.actions = ActionLog()
        self.terminal = Terminal()
        self.health: dict[str, bool] = {}
        self.measure_info: dict[str, dict] = {}
        self.arbiter_lock = asyncio.Lock()
        self.on_reload: list[Callable[[], Any]] = []
        self.collectors: list[tuple[float, Callable[[], Any]]] = []   # (co ile s, funkcja) z wtyczek
        self.monitor.resolve_owner = self.resolve_owner
        self.registry.load()
        git_init()
        self._task: asyncio.Task | None = None
        self._measuring: dict[str, asyncio.Task] = {}

    # ---------- pętla ----------
    async def run_loop(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        tick = 0
        while True:
            try:
                if tick % 3 == 0:
                    await asyncio.to_thread(self.docker.refresh)
                await asyncio.to_thread(self.monitor.sample)
                if tick % 2 == 0:
                    await asyncio.to_thread(self.monitor.sample_processes)
                if tick % 2 == 1:
                    self._watch_files()
                if tick % 5 == 0:
                    await self._check_health()
                    await self._auto_restore()
                for every, fn in self.collectors:
                    if tick % max(int(every), 1) == 0:
                        r = fn()
                        if asyncio.iscoroutine(r):
                            await r
            except Exception as e:  # pętla nie może paść przez pojedynczy błąd
                print(f"[monitor] {type(e).__name__}: {e}")
            tick += 1
            await asyncio.sleep(1)

    @staticmethod
    def _cfg_mtime() -> float:
        try:
            return config_path().stat().st_mtime
        except OSError:
            return 0

    def _watch_files(self) -> None:
        changed = False
        if self.registry.changed():
            self.registry.load()
            changed = True
        if self._cfg_mtime() != self._config_mtime:
            self._config_mtime = self._cfg_mtime()
            self.config = load_config()
            changed = True
        if changed:
            for fn in self.on_reload:
                try:
                    fn()
                except Exception as e:
                    print(f"[reload] {e}")

    def reload(self) -> dict:
        self.registry.load()
        self.config = load_config()
        self._config_mtime = self._cfg_mtime()
        for fn in self.on_reload:
            fn()
        return {"tiles": len(self.registry.tiles), "profiles": len(self.registry.profiles),
                "errors": self.registry.errors}

    async def _check_health(self) -> None:
        async def one(t: Tile) -> None:
            def get() -> bool:
                try:
                    with urllib.request.urlopen(t.health, timeout=2) as r:
                        return 200 <= r.status < 300
                except Exception:
                    return False
            self.health[t.id] = await asyncio.to_thread(get)

        todo = [t for t in self.registry.tiles.values() if t.health and self._base_status(t) != "stopped"]
        for t in self.registry.tiles.values():
            if t.health and t not in todo:
                self.health.pop(t.id, None)
        await asyncio.gather(*(one(t) for t in todo))

    async def _auto_restore(self) -> None:
        """Kafelek, który odsunął innych, przestał działać (np. llm-stop w terminalu): przywróć ich."""
        if self.arbiter_lock.locked():
            return
        for tid in list(self.store["yielded"]):
            t = self.registry.tiles.get(tid)
            started = self.store["started"].get(tid, {}).get("ts", 0)
            if not t or time.time() - started < RESTORE_GRACE or self.status(t) != "stopped":
                continue
            await arbiter.restore(self, t, "arbiter")

    # ---------- właściciel procesu ----------
    def _match_pids(self, t: Tile) -> list[int]:
        if not t.match:
            return []
        rx = re.compile(t.match)
        return [p["pid"] for p in self.monitor.processes if rx.search(p["cmd"])]

    def _session_pids(self, t: Tile) -> list[int]:
        sid = self.store["started"].get(t.id, {}).get("sid")
        if not sid or t.type != "app":
            return []
        return [p["pid"] for p in self.monitor.processes if p.get("sid") == sid]

    def resolve_owner(self, pid: int) -> tuple[str, str]:
        try:
            proc = psutil.Process(pid)
            name, cmd = proc.name(), " ".join(proc.cmdline())
            sid = os.getsid(pid)
        except (psutil.Error, OSError):
            name, cmd, sid = f"PID {pid}", "", None
        for t in self.registry.tiles.values():
            if pid_from_file(t.pidfile) == pid or (t.match and re.search(t.match, cmd)):
                return "kafelek", t.title
            if t.type == "app" and sid and self.store["started"].get(t.id, {}).get("sid") == sid:
                return "kafelek", t.title
        try:
            with open(f"/proc/{pid}/cgroup") as f:
                m = _DOCKER_CGROUP.search(f.read())
        except OSError:
            m = None
        if m:
            cname = self.docker.name_of(m.group(1))
            if cname:
                for t in self.registry.tiles.values():
                    if t.container == cname:
                        return "kafelek", t.title
                return "kontener", cname
        return "proces", name

    # ---------- stan kafelka ----------
    def _base_status(self, t: Tile) -> str:
        job = self.runner.jobs.get(t.id)
        if t.container:
            return "running" if self.docker.state(t.container) == "running" else "stopped"
        if t.compose_project:
            cs = self.docker.project(t.compose_project)
            up = sum(c["state"] == "running" for c in cs)
            return "stopped" if up == 0 else "running" if up == len(cs) else "partial"
        if pid_from_file(t.pidfile) or self._match_pids(t):
            return "running"
        if job and job.alive:
            return "starting" if (t.pidfile or t.match) else "running"
        if self._session_pids(t):
            return "running"
        return "stopped"

    def status(self, t: Tile) -> str:
        base = self._base_status(t)
        if base == "running" and t.health and not self.health.get(t.id):
            started = self.store["started"].get(t.id, {}).get("ts", 0)
            if t.id not in self.health or time.time() - started < STARTUP_GRACE:
                return "starting"
            return "unhealthy"
        return base

    def vram_of(self, t: Tile) -> int:
        names = {t.title}
        if t.compose_project:
            names |= {c["name"] for c in self.docker.project(t.compose_project)}
        return sum(p["mib"] for g in self.monitor.snapshot.get("gpus", [])
                   for p in g["procs"] if p["owner"] in names)

    def owners(self) -> list[dict]:
        """VRAM pierwszej karty NVIDIA zsumowany po właścicielu."""
        gpus = self.monitor.snapshot.get("gpus", [])
        agg: dict[tuple[str, str], dict] = {}
        for p in (gpus[0]["procs"] if gpus else []):
            row = agg.setdefault((p["kind"], p["owner"]),
                                 {"kind": p["kind"], "owner": p["owner"], "mib": 0, "pids": []})
            row["mib"] += p["mib"]
            row["pids"].append(p["pid"])
        return sorted(agg.values(), key=lambda r: -r["mib"])

    def tile_info(self, t: Tile) -> dict:
        last = self.store["measured"].get(t.id, {}).get("_last")
        return {"status": self.status(t), "vram_mib": self.vram_of(t), "health": self.health.get(t.id),
                "measured": last, "measuring": t.id in self._measuring,
                "yielded": [r["target"] for r in self.store["yielded"].get(t.id, [])]}

    async def wait_status(self, t: Tile, want: tuple[str, ...], timeout: float) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.status(t) in want:
                return True
            await asyncio.sleep(1)
            if t.container or t.compose_project:
                await asyncio.to_thread(self.docker.refresh)
            await asyncio.to_thread(self.monitor.sample_processes)
        return False

    def preview(self, t: Tile, values: dict | None = None) -> str:
        if t.start:
            return render(t, values)
        if t.container:
            return f"docker start {t.container}"
        if t.compose_project:
            return "docker start " + " ".join(c["name"] for c in self.docker.project(t.compose_project))
        return ""

    # ---------- niski poziom: bez arbitra ----------
    async def start_tile(self, t: Tile, values: dict | None = None) -> str:
        values = t.values(values=values)
        rec: dict[str, Any] = {"ts": time.time(), "values": values}
        if t.start:
            job = await self.runner.run(t.id, script(t, render(t, values)), t.cwd)
            rec["sid"] = job.proc.pid if job.proc else None
        elif t.container:
            await asyncio.to_thread(self.docker.action, t.container, "start")
        elif t.compose_project:
            for c in self.docker.project(t.compose_project):
                await asyncio.to_thread(self.docker.action, c["name"], "start")
        else:
            raise ValueError("Kafelek nie ma komendy start")
        self.store["started"][t.id] = rec
        self.store.save()
        if t.container or t.compose_project:
            await asyncio.to_thread(self.docker.refresh)
        return f"Start: {t.title}"

    async def stop_tile(self, t: Tile, values: dict | None = None, restore: bool = True) -> str:
        if t.stop:
            await self.runner.run_wait(t.id, script(t, render(t, t.values(values=values), t.stop)), t.cwd, 60)
        elif t.container:
            await asyncio.to_thread(self.docker.action, t.container, "stop")
        elif t.compose_project:
            for c in self.docker.project(t.compose_project):
                await asyncio.to_thread(self.docker.action, c["name"], "stop")
        elif self.runner.terminate(t.id):
            pass
        else:
            sid = self.store["started"].get(t.id, {}).get("sid")
            if t.type == "app" and sid and self._session_pids(t):
                os.killpg(sid, signal.SIGTERM)
            else:
                pids = self._match_pids(t) or [p for p in [pid_from_file(t.pidfile)] if p]
                if not pids:
                    return f"{t.title}: nic nie działa"
                for pid in pids:
                    os.kill(pid, signal.SIGTERM)
        if t.container or t.compose_project:
            await asyncio.to_thread(self.docker.refresh)
        msg = f"Stop: {t.title}"
        if restore and self.store["yielded"].get(t.id):
            await self.wait_status(t, ("stopped",), 30)
            back = await arbiter.restore(self, t, "arbiter")
            if back:
                msg += "; przywrócono: " + "; ".join(back)
        return msg

    # ---------- pomiar VRAM po starcie ----------
    def schedule_measure(self, t: Tile, values: dict) -> None:
        if not t.vram:
            return
        old = self._measuring.pop(t.id, None)
        if old:
            old.cancel()
        self._measuring[t.id] = asyncio.create_task(self._measure(t, values))

    async def _measure(self, t: Tile, values: dict) -> None:
        try:
            if not await self.wait_status(t, ("running",), 300):
                return
            readings: list[int] = []
            for _ in range(60):
                await asyncio.sleep(3)
                readings.append(self.vram_of(t))
                last = readings[-3:]
                if len(last) == 3 and min(last) > 0 and max(last) - min(last) < 64:
                    mib = max(last)
                    budget = t.budget(values)
                    m = self.store["measured"].setdefault(t.id, {})
                    m[arbiter.values_key(values)] = mib
                    m["_last"] = {"mib": mib, "budget": budget, "ts": time.time(),
                                  "diff": None if budget is None else mib - budget}
                    self.store.save()
                    self.actions.add("arbiter", "measure", {"tile": t.id}, m["_last"], True)
                    return
        except asyncio.CancelledError:
            pass
        finally:
            self._measuring.pop(t.id, None)

    # ---------- wysoki poziom: z arbitrem i dziennikiem ----------
    async def act(self, source: str, action: str, args: dict, fn: Callable[[], Awaitable[Any]]) -> Any:
        try:
            res = await fn()
        except Exception as e:
            self.actions.add(source, action, args, f"{type(e).__name__}: {e}", False)
            raise
        self.actions.add(source, action, args, res, True)
        return res

    def tile(self, tile_id: str) -> Tile:
        t = self.registry.tiles.get(tile_id)
        if not t:
            raise KeyError(f"Nie ma kafelka {tile_id}")
        return t

    def plan(self, tile_id: str, preset: str | None = None, values: dict | None = None) -> arbiter.Plan:
        t = self.tile(tile_id)
        return arbiter.plan_tile(self, t, t.values(preset, values))

    def plan_profile(self, profile_id: str) -> arbiter.Plan:
        p = self.registry.profiles.get(profile_id)
        if not p:
            raise KeyError(f"Nie ma profilu {profile_id}")
        return arbiter.plan_profile(self, p)

    async def start(self, tile_id: str, preset: str | None = None, values: dict | None = None,
                    confirm: bool = False, source: str = "gui") -> dict:
        """Start przez arbitra. Bez confirm zwraca plan, jeśli start odsuwa inne usługi."""
        t = self.tile(tile_id)
        plan = self.plan(tile_id, preset, values)
        if not plan.trivial and not confirm:
            return {"started": False, "needs_confirm": True, "plan": plan.to_dict()}
        args = {"tile": tile_id, "preset": preset, "values": values or {}}

        async def run() -> dict:
            if plan.trivial:
                vals = t.values(preset, values)
                msg = await self.start_tile(t, vals)
                self.schedule_measure(t, vals)
                return {"started": True, "messages": [msg] + plan.warnings}
            return {"started": True, "messages": await arbiter.execute(self, plan, source), "plan": plan.to_dict()}
        return await self.act(source, "start_tile", args, run)

    async def stop(self, tile_id: str, source: str = "gui") -> dict:
        t = self.tile(tile_id)

        async def run() -> dict:
            async with self.arbiter_lock:
                return {"message": await self.stop_tile(t)}
        return await self.act(source, "stop_tile", {"tile": tile_id}, run)

    async def apply_profile(self, profile_id: str, confirm: bool = False, source: str = "gui") -> dict:
        plan = self.plan_profile(profile_id)
        if not confirm:
            return {"applied": False, "needs_confirm": True, "plan": plan.to_dict()}

        async def run() -> dict:
            msgs = await arbiter.execute(self, plan, source)
            self.store.set("profile", profile_id)
            return {"applied": True, "messages": msgs, "plan": plan.to_dict()}
        return await self.act(source, "apply_profile", {"profile": profile_id}, run)

    async def container_action(self, name: str, action: str, source: str = "gui") -> str:
        async def run() -> str:
            msg = await asyncio.to_thread(self.docker.action, name, action)
            await asyncio.to_thread(self.docker.refresh)
            return msg
        return await self.act(source, "container_action", {"name": name, "action": action}, run)

    async def project_action(self, project: str, action: str, source: str = "gui") -> str:
        async def run() -> str:
            names = [c["name"] for c in self.docker.project(project)]
            if not names:
                raise ValueError(f"Nie ma kontenerów projektu {project}")
            order = names[::-1] if action == "stop" else names
            for n in order:
                await asyncio.to_thread(self.docker.action, n, action)
            await asyncio.to_thread(self.docker.refresh)
            return f"{project}: {action} ({len(names)} kontenerów)"
        return await self.act(source, "project_action", {"project": project, "action": action}, run)

    async def kill(self, pid: int, force: bool = False, source: str = "gui") -> str:
        async def run() -> str:
            return self.monitor.kill(pid, force)
        return await self.act(source, "kill_process", {"pid": pid, "force": force}, run)
