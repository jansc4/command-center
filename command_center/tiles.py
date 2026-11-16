"""Definicje kafelków i profili (YAML), składanie komend i uruchamianie."""
from __future__ import annotations

import ast
import asyncio
import os
import re
import shlex
import signal
from collections import deque
from pathlib import Path
from typing import Any, Literal

import psutil
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import CONFIG_DIR, seed_dir


def session_env() -> dict[str, str]:
    """Środowisko dla uruchamianych programów: bez venv panelu, z dostępem do sesji graficznej.

    Demon może wystartować bez DISPLAY / WAYLAND_DISPLAY (systemd, zdalna powłoka),
    więc brakujące zmienne są odtwarzane z gniazd w XDG_RUNTIME_DIR.
    """
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None) or str(Path(__file__).resolve().parent.parent / ".venv")
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


# ---------------- modele ----------------
class Option(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    label: str = ""
    type: Literal["bool", "int", "str", "enum"] = "str"
    flag: str | None = None          # brak flagi = wartość wstawiana w miejsce {id}
    default: Any = None
    values: dict[str, str] | None = None   # dla enum: wartość -> opis
    custom: bool = False             # enum przyjmuje też własną wartość (np. ścieżkę .gguf)
    range: list[int] | None = None
    help: str = ""
    advanced: bool = False


class Step(BaseModel):
    """Jak usługa ustępuje (yield) albo wraca (resume)."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    kind: Literal["stop", "start", "http_post"]
    url: str | None = None
    json_: dict | None = Field(default=None, alias="json")

    @classmethod
    def parse(cls, v: Any) -> Any:
        if v is None or isinstance(v, cls):
            return v
        if isinstance(v, str):
            return {"kind": v}
        if isinstance(v, dict) and "http_post" in v:
            return {"kind": "http_post", "url": v["http_post"], "json": v.get("json")}
        return v

    def describe(self) -> str:
        if self.kind == "http_post":
            return f"POST {self.url} {self.json_ or ''}".strip()
        return {"stop": "zatrzymanie", "start": "uruchomienie"}[self.kind]


class Vram(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    # liczba albo wyrażenie po opcjach kafelka, np. "11400 if legacy else 3016 + cache"
    budget_mib: int | str | None = None
    priority: int = 50               # wyższy = ważniejszy; niższe ustępują pierwsze
    yield_: Step | None = Field(default=None, alias="yield")   # domyślnie: stop
    resume: Step | None = None       # domyślnie: odwrotność yield (stop -> start)

    @field_validator("yield_", "resume", mode="before")
    @classmethod
    def _step(cls, v: Any) -> Any:
        return Step.parse(v)

    def yield_step(self) -> Step:
        return self.yield_ or Step(kind="stop")

    def resume_step(self) -> Step | None:
        if self.resume:
            return self.resume
        return Step(kind="start") if self.yield_step().kind == "stop" else None


class Hook(BaseModel):
    """on_start: [{yield: kokoro-tts, unless: keep_kokoro}]"""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    yield_: str = Field(alias="yield")
    if_: str | None = Field(default=None, alias="if")      # tylko gdy opcja prawdziwa
    unless: str | None = None                               # pomiń, gdy opcja prawdziwa

    def active(self, values: dict[str, Any]) -> bool:
        if self.if_ and not truthy(values.get(self.if_)):
            return False
        if self.unless and truthy(values.get(self.unless)):
            return False
        return True


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
    health: str | None = None        # GET; 2xx = gotowe
    options: list[Option] = Field(default_factory=list)
    presets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    vram: Vram | None = None
    on_start: list[Hook] = Field(default_factory=list)
    on_stop: Literal["restore", "none"] = "restore"
    source: str = ""                 # plik, z którego pochodzi definicja

    def defaults(self) -> dict[str, Any]:
        return {o.id: o.default for o in self.options}

    def values(self, preset: str | None = None, values: dict | None = None) -> dict[str, Any]:
        if preset and preset not in self.presets:
            raise ValueError(f"{self.id}: nie ma presetu {preset!r} (są: {', '.join(self.presets) or '-'})")
        return {**self.defaults(), **(self.presets.get(preset, {}) if preset else {}), **(values or {})}

    def budget(self, values: dict[str, Any] | None = None) -> int | None:
        if not self.vram or self.vram.budget_mib is None:
            return None
        b = self.vram.budget_mib
        if isinstance(b, int):
            return b
        return int(safe_eval(b, self.values(values=values)))

    def budget_safe(self, values: dict[str, Any] | None = None) -> int | None:
        try:
            return self.budget(values)
        except Exception:
            return None


class ProfileItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tile: str
    preset: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class Profile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    title: str
    help: str = ""
    stop: list[str] = Field(default_factory=list)
    start: list[ProfileItem] = Field(default_factory=list)
    source: str = ""


def truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off", "nie")
    return bool(v)


_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load, ast.IfExp,
            ast.Compare, ast.BoolOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.USub,
            ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.And, ast.Or, ast.Not)


def safe_eval(expr: str, names: dict[str, Any]) -> Any:
    """Arytmetyka i warunki na opcjach kafelka; nic więcej."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"Niedozwolone w wyrażeniu budżetu: {type(node).__name__}")
    env = {}
    for k, v in names.items():
        if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
            v = int(v)
        env[k] = v
    return eval(compile(tree, "<budget>", "eval"), {"__builtins__": {}}, env)


# ---------------- składanie komend ----------------
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
                if truthy(v):
                    args.append(o.flag)
            elif v not in (None, "") and v != o.default:
                args += [o.flag, shlex.quote(str(v))]
        elif o.type == "bool":
            ctx[o.id] = "1" if truthy(v) else "0"
        else:
            ctx[o.id] = "" if v in (None, "") else shlex.quote(str(v))
    ctx["args"] = " ".join(args)
    cmd = tile.start if template is None else template
    for k, v in ctx.items():
        cmd = cmd.replace("{" + k + "}", v)
    return re.sub(r"[ \t]+\n", "\n", cmd).strip()


def script(tile: Tile, body: str) -> str:
    return f"{tile.shell_init}\n{body}" if tile.shell_init else body


# ---------------- rejestr plików ----------------
def parse_tiles(text: str, source: str = "") -> list[Tile]:
    return [Tile(**doc, source=source) for doc in yaml.safe_load_all(text) if doc]


def parse_profiles(text: str, source: str = "") -> list[Profile]:
    return [Profile(**doc, source=source) for doc in yaml.safe_load_all(text) if doc]


class Registry:
    """Kafelki z tiles/*.yaml i profile z profiles/*.yaml; przeładowanie po zmianie plików."""

    def __init__(self) -> None:
        self.dir = CONFIG_DIR / "tiles"
        self.profiles_dir = CONFIG_DIR / "profiles"
        self.tiles: dict[str, Tile] = {}
        self.profiles: dict[str, Profile] = {}
        self.errors: list[str] = []
        self.version = 0
        self._mtimes: dict[str, float] = {}

    def _files(self) -> list[Path]:
        return sorted([*self.dir.glob("*.y*ml"), *self.profiles_dir.glob("*.y*ml")])

    def _stamp(self) -> dict[str, float]:
        out = {}
        for f in self._files():
            try:
                out[str(f)] = f.stat().st_mtime
            except OSError:
                pass
        return out

    def changed(self) -> bool:
        return self._stamp() != self._mtimes

    def load(self) -> None:
        seed_dir("tiles")
        seed_dir("profiles")
        tiles, profiles, errors = {}, {}, []
        for f in sorted(self.dir.glob("*.y*ml")):
            try:
                for t in parse_tiles(f.read_text(), str(f)):
                    if t.id in tiles:
                        errors.append(f"{f.name}: kafelek {t.id} zdefiniowany drugi raz")
                    tiles[t.id] = t
            except Exception as e:
                errors.append(f"{f.name}: {e}")
        for f in sorted(self.profiles_dir.glob("*.y*ml")):
            try:
                for p in parse_profiles(f.read_text(), str(f)):
                    profiles[p.id] = p
            except Exception as e:
                errors.append(f"profiles/{f.name}: {e}")
        for p in profiles.values():
            for ref in [*p.stop, *(i.tile for i in p.start)]:
                if ref not in tiles:
                    errors.append(f"profil {p.id}: nie ma kafelka {ref}")
        for t in tiles.values():
            for h in t.on_start:
                if h.yield_ not in tiles:
                    errors.append(f"{t.id}: on_start wskazuje nieznany kafelek {h.yield_}")
        self.tiles, self.profiles, self.errors = tiles, profiles, errors
        self._mtimes = self._stamp()
        self.version += 1

    def groups(self) -> dict[str, list[Tile]]:
        out: dict[str, list[Tile]] = {}
        for t in self.tiles.values():
            out.setdefault(t.group, []).append(t)
        return out


def validate_file(rel: str, text: str) -> str:
    """Sprawdza plik konfiguracji przed zapisem. Zwraca opis albo rzuca ValueError."""
    from .config import Config
    try:
        if rel.startswith("tiles/"):
            items = parse_tiles(text, rel)
            ids = [t.id for t in items]
            if len(ids) != len(set(ids)):
                raise ValueError("powtórzone id w pliku")
            for t in items:
                t.budget()  # sprawdza wyrażenie budżetu
            return f"{len(items)} kafelków"
        if rel.startswith("profiles/"):
            return f"{len(parse_profiles(text, rel))} profili"
        if rel == "config.yaml":
            Config(**(yaml.safe_load(text) or {}))
            return "ustawienia"
        if rel.startswith("plugins/"):
            compile(text, rel, "exec")
            return "wtyczka"
    except Exception as e:
        raise ValueError(str(e)) from e
    raise ValueError("Nieobsługiwany plik")


# ---------------- uruchamianie ----------------
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

    async def run_wait(self, key: str, body: str, cwd: str | None = None, timeout: float = 120) -> Job:
        job = await self.run(key, body, cwd)
        t0 = asyncio.get_running_loop().time()
        while job.rc is None and asyncio.get_running_loop().time() - t0 < timeout:
            await asyncio.sleep(0.3)
        return job

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
