"""Katalog konfiguracji, config.yaml, plik stanu (state.json) i wersjonowanie w git."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONFIG_DIR = Path(os.environ.get("CC_CONFIG", "~/.config/command-center")).expanduser()
REPO_DIR = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG = """\
# Command Center: ustawienia ogólne. Zmiany są wczytywane automatycznie.

# Poziom serwera MCP pod http://127.0.0.1:8770/mcp
#   read    - tylko odczyt (status, kafelki, procesy, logi, plan)
#   control - odczyt + start/stop kafelków, profile, akcje kontenerów
#   full    - jak control + kill procesu, dowolna komenda, edycja kafelków
mcp_level: control

# Zapas VRAM (MiB), którego arbiter nie rozdaje (sterownik, pulpit, skoki przy ładowaniu)
vram_margin_mib: 300

# Dostęp spoza tej maszyny (np. przez Tailscale). Wyłączony domyślnie.
# Po włączeniu panel nasłuchuje na host, a każde żądanie spoza 127.0.0.1 musi podać token
# (nagłówek "Authorization: Bearer <token>" albo raz ?token=<token> w adresie, potem ciasteczko).
remote:
  enabled: false
  host: 0.0.0.0
  token: ""
"""


class Remote(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    host: str = "0.0.0.0"
    token: str = ""


class Config(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mcp_level: Literal["read", "control", "full"] = "control"
    vram_margin_mib: int = 300
    remote: Remote = Field(default_factory=Remote)


def config_path() -> Path:
    return CONFIG_DIR / "config.yaml"


def load_config() -> Config:
    p = config_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_CONFIG)
    try:
        return Config(**(yaml.safe_load(p.read_text()) or {}))
    except Exception as e:
        print(f"[config] błąd w {p}: {e}; używam domyślnych")
        return Config()


def seed_dir(name: str) -> Path:
    """Tworzy ~/.config/command-center/<name> z kopii w repozytorium, jeśli go nie ma."""
    target = CONFIG_DIR / name
    src = REPO_DIR / name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.copytree(src, target)
        else:
            target.mkdir()
    return target


# ---------------- stan trwały ----------------
class StateStore:
    """Mały plik JSON: co arbiter wyłączył, zmierzony VRAM, sesje aplikacji, aktywny profil."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG_DIR / "state.json"
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {"yielded": {}, "started": {}, "measured": {}, "profile": None}
        try:
            self.data.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
            tmp.replace(self.path)

    def __getitem__(self, key: str) -> Any:
        return self.data.setdefault(key, {})

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()


# ---------------- git w katalogu konfiguracji ----------------
def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(CONFIG_DIR), *args], capture_output=True, text=True, timeout=20)


def git_init() -> None:
    if not shutil.which("git") or (CONFIG_DIR / ".git").exists():
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main")
    (CONFIG_DIR / ".gitignore").write_text("state.json\nstate.tmp\nactions.jsonl\n__pycache__/\n")
    git_commit("Stan początkowy konfiguracji")


def git_commit(message: str) -> str:
    if not (CONFIG_DIR / ".git").exists():
        return ""
    _git("add", "-A")
    r = _git("-c", "user.name=Command Center", "-c", "user.email=command-center@localhost",
             "commit", "-q", "-m", message)
    return r.stdout.strip() or r.stderr.strip()


def git_log(path: str | None = None, n: int = 15) -> list[dict]:
    if not (CONFIG_DIR / ".git").exists():
        return []
    args = ["log", f"-{n}", "--format=%h\t%ct\t%s"]
    if path:
        args += ["--", path]
    out = []
    for line in _git(*args).stdout.splitlines():
        h, ts, msg = line.split("\t", 2)
        out.append({"hash": h, "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts))), "message": msg})
    return out


def git_show(rev: str, path: str) -> str:
    r = _git("show", f"{rev}:{path}")
    if r.returncode:
        raise ValueError(r.stderr.strip())
    return r.stdout
