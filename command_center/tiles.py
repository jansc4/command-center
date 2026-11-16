"""Definicje kafelków (YAML), składanie komend i uruchamianie."""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import signal
from collections import deque
from pathlib import Path
from typing import Any, Literal

import psutil
import yaml
from pydantic import BaseModel, ConfigDict, Field

CONFIG_DIR = Path(os.environ.get("CC_CONFIG", "~/.config/command-center")).expanduser()
DEFAULT_TILES = Path(__file__).resolve().parent.parent / "tiles"


def session_env() -> dict[str, str]:
    """Środowisko dla uruchamianych programów: bez venv panelu, z dostępem do sesji graficznej.

    Demon może wystartować bez DISPLAY / WAYLAND_DISPLAY (systemd, zdalna powłoka),
    więc brakujące zmienne są odtwarzane z gniazd w XDG_RUNTIME_DIR.
    """
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        env["PATH"] = os.pathsep.join(
            d for d in env.get("PATH", "").split(os.pathsep) if not d.startswith(venv))
    for k in ("PYTHONHOME", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
        env.pop(k, None)
    run = Path(env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if not env.get("WAYLAND_DISPLAY"):
        socks = sorted(f.name for f in run.glob("wayland-*") if not f.name.endswith(".lock"))
        if socks:
            env["WAYLAND_DISPLAY"] = socks[0]
    if not env.get("DISPLAY"):
        xs = sorted(f.name[1:] for f in Path("/tmp/.X11-unix").glob("X*") if f.name[1:].isdigit())
        if xs:
            env["DISPLAY"] = f":{xs[0]}"
    if not env.get("XAUTHORITY"):
        auth = sorted(run.glob("xauth_*"))
        if auth:
            env["XAUTHORITY"] = str(auth[0])
    if env.get("DBUS_SESSION_BUS_ADDRESS", "disabled:") == "disabled:" and (run / "bus").exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={run / 'bus'}"
    if env.get("WAYLAND_DISPLAY"):
        env.setdefault("XDG_SESSION_TYPE", "wayland")
    return env


class Option(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    label: str = ""
    type: Literal["bool", "int", "str", "enum"] = "str"
    flag: str | None = None          # brak flagi = wartość wstawiana w miejsce {id}
    default: Any = None
    values: dict[str, str] | None = None   # dla enum: wartość -> opis
    range: list[int] | None = None
    help: str = ""
    advanced: bool = False


class Vram(BaseModel):
    model_config = ConfigDict(extra="ignore")
    budget_mib: int | None = None
    priority: int = 50


class Tile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    title: str
    type: Literal["app", "service", "container", "stack", "command"] = "command"
    group: str = "Inne"
    help: str = ""
    shell_init: str = ""             # np. source ~/llm/llm-aliases.sh
    start: str = ""
    stop: str = ""
    cwd: str | None = None
    pidfile: str | None = None
    match: str | None = None         # regex na linię poleceń procesu
    container: str | None = None
    compose_project: str | None = None
    log: str | None = None
    url: str | None = None
    options: list[Option] = Field(default_factory=list)
    presets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    vram: Vram | None = None
    source: str = ""                 # plik, z którego pochodzi definicja

    def defaults(self) -> dict[str, Any]:
        return {o.id: o.default for o in self.options}


def render(tile: Tile, values: dict[str, Any] | None = None, template: str | None = None) -> str:
    """Wstawia opcje do szablonu komendy. {args} = wszystkie opcje z flagą."""
    vals = {**tile.defaults(), **(values or {})}
    args: list[str] = []
    ctx: dict[str, str] = {}
    for o in tile.options:
        v = vals.get(o.id)
        if o.type == "int" and v not in (None, ""):
            v = int(v)
        if o.flag:
            if o.type == "bool":
                if v:
                    args.append(o.flag)
            elif v not in (None, "") and v != o.default:
                args += [o.flag, shlex.quote(str(v))]
        elif o.type == "bool":
            ctx[o.id] = "1" if v else "0"
        else:
            ctx[o.id] = "" if v in (None, "") else shlex.quote(str(v))
    ctx["args"] = " ".join(args)
    cmd = tile.start if template is None else template
    for k, v in ctx.items():
        cmd = cmd.replace("{" + k + "}", v)
    return re.sub(r"[ \t]+\n", "\n", cmd).strip()


def script(tile: Tile, body: str) -> str:
    return f"{tile.shell_init}\n{body}" if tile.shell_init else body


class Registry:
    def __init__(self) -> None:
        self.dir = CONFIG_DIR / "tiles"
        self.tiles: dict[str, Tile] = {}
        self.errors: list[str] = []

    def load(self) -> None:
        if not self.dir.exists() and DEFAULT_TILES.exists():
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(DEFAULT_TILES, self.dir)
        tiles, errors = {}, []
        for f in sorted(self.dir.glob("*.y*ml")):
            try:
                for doc in yaml.safe_load_all(f.read_text()):
                    if doc:
                        t = Tile(**doc, source=str(f))
                        tiles[t.id] = t
            except Exception as e:
                errors.append(f"{f.name}: {e}")
        self.tiles, self.errors = tiles, errors

    def groups(self) -> dict[str, list[Tile]]:
        out: dict[str, list[Tile]] = {}
        for t in self.tiles.values():
            out.setdefault(t.group, []).append(t)
        return out


class Job:
    def __init__(self) -> None:
        self.lines: deque[str] = deque(maxlen=500)
        self.count = 0
        self.proc: asyncio.subprocess.Process | None = None
        self.rc: int | None = None

    def add(self, line: str) -> None:
        self.lines.append(line)
        self.count += 1

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.rc is None


class Runner:
    """Uruchamia skrypty kafelków w osobnej sesji i zbiera ich wyjście."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    async def run(self, key: str, body: str, cwd: str | None = None) -> Job:
        job = Job()
        self.jobs[key] = job
        for line in body.splitlines():
            job.add("$ " + line)
        job.proc = await asyncio.create_subprocess_exec(
            "bash", "-c", body,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            env=session_env(),
            cwd=os.path.expanduser(cwd) if cwd else None,
        )
        asyncio.create_task(self._pump(job))
        return job

    @staticmethod
    async def _pump(job: Job) -> None:
        assert job.proc and job.proc.stdout
        while True:
            raw = await job.proc.stdout.readline()
            if not raw:
                break
            job.add(raw.decode(errors="replace").rstrip())
        job.rc = await job.proc.wait()
        job.add(f"[zakończono, kod {job.rc}]")

    def terminate(self, key: str) -> bool:
        job = self.jobs.get(key)
        if job and job.alive:
            os.killpg(job.proc.pid, signal.SIGTERM)
            return True
        return False


def pid_from_file(path: str | None) -> int | None:
    if not path:
        return None
    try:
        pid = int(Path(path).expanduser().read_text().strip())
        return pid if psutil.pid_exists(pid) else None
    except (OSError, ValueError):
        return None
