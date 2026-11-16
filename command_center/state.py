"""Wspólny stan rdzenia: monitor, Docker, kafelki. Używany przez GUI i API."""
from __future__ import annotations

import asyncio
import os
import re
import signal

import psutil

from .dockerctl import DockerCtl
from .monitor import Monitor
from .tiles import Registry, Runner, Tile, pid_from_file, render, script

_DOCKER_CGROUP = re.compile(r"docker[-/]([0-9a-f]{64})")


class State:
    def __init__(self) -> None:
        self.monitor = Monitor()
        self.docker = DockerCtl()
        self.registry = Registry()
        self.runner = Runner()
        self.monitor.resolve_owner = self.resolve_owner
        self.registry.load()
        self._task: asyncio.Task | None = None

    # ---------- pętla próbkowania ----------
    async def start(self) -> None:
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
            except Exception as e:  # pętla nie może paść przez pojedynczy błąd
                print(f"[monitor] {e}")
            tick += 1
            await asyncio.sleep(1)

    # ---------- właściciel procesu ----------
    def _match_pids(self, t: Tile) -> list[int]:
        if not t.match:
            return []
        rx = re.compile(t.match)
        return [p["pid"] for p in self.monitor.processes if rx.search(p["cmd"])]

    def resolve_owner(self, pid: int) -> tuple[str, str]:
        try:
            proc = psutil.Process(pid)
            name, cmd = proc.name(), " ".join(proc.cmdline())
        except psutil.Error:
            name, cmd = f"PID {pid}", ""
        for t in self.registry.tiles.values():
            if pid_from_file(t.pidfile) == pid or (t.match and re.search(t.match, cmd)):
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

    # ---------- kafelki ----------
    def status(self, t: Tile) -> str:
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
        return "stopped"

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

    def preview(self, t: Tile, values: dict | None = None) -> str:
        if t.start:
            return render(t, values)
        if t.container:
            return f"docker start {t.container}"
        if t.compose_project:
            return "docker start " + " ".join(c["name"] for c in self.docker.project(t.compose_project))
        return ""

    async def start_tile(self, t: Tile, values: dict | None = None) -> str:
        if t.start:
            await self.runner.run(t.id, script(t, render(t, values)), t.cwd)
        elif t.container:
            await asyncio.to_thread(self.docker.action, t.container, "start")
        elif t.compose_project:
            for c in self.docker.project(t.compose_project):
                await asyncio.to_thread(self.docker.action, c["name"], "start")
        else:
            raise ValueError("Kafelek nie ma komendy start")
        return f"Start: {t.title}"

    async def stop_tile(self, t: Tile, values: dict | None = None) -> str:
        if t.stop:
            await self.runner.run(t.id, script(t, render(t, values, t.stop)), t.cwd)
        elif t.container:
            await asyncio.to_thread(self.docker.action, t.container, "stop")
        elif t.compose_project:
            for c in self.docker.project(t.compose_project):
                await asyncio.to_thread(self.docker.action, c["name"], "stop")
        elif self.runner.terminate(t.id):
            pass
        else:
            pids = self._match_pids(t) or [p for p in [pid_from_file(t.pidfile)] if p]
            if not pids:
                return f"{t.title}: nic nie działa"
            for pid in pids:
                os.kill(pid, signal.SIGTERM)
        return f"Stop: {t.title}"
