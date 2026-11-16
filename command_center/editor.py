"""Edycja plików konfiguracji z walidacją przed zapisem i commitem w git."""
from __future__ import annotations

from pathlib import Path

from .config import CONFIG_DIR, git_commit, git_log, git_show
from .tiles import validate_file

EDITABLE = ("tiles", "profiles", "plugins")


def _safe(rel: str) -> Path:
    p = (CONFIG_DIR / rel).resolve()
    root = CONFIG_DIR.resolve()
    if root not in p.parents and p != root:
        raise ValueError("Ścieżka poza katalogiem konfiguracji")
    top = p.relative_to(root).parts[0]
    if rel != "config.yaml" and top not in EDITABLE:
        raise ValueError("Edytować można config.yaml oraz pliki w tiles/, profiles/, plugins/")
    if rel != "config.yaml" and p.suffix not in (".yaml", ".yml", ".py"):
        raise ValueError("Dozwolone rozszerzenia: .yaml, .yml, .py")
    return p


def list_files() -> list[str]:
    out = ["config.yaml"] if (CONFIG_DIR / "config.yaml").exists() else []
    for d in EDITABLE:
        for f in sorted((CONFIG_DIR / d).glob("*")):
            if f.suffix in (".yaml", ".yml", ".py"):
                out.append(f"{d}/{f.name}")
    return out


def read(rel: str) -> str:
    p = _safe(rel)
    return p.read_text() if p.exists() else ""


def save(rel: str, content: str, source: str = "gui") -> str:
    p = _safe(rel)
    what = validate_file(rel, content)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if content.endswith("\n") else content + "\n")
    git_commit(f"{rel}: zapis z {source}")
    return f"Zapisano {rel} ({what})"


def history(rel: str) -> list[dict]:
    _safe(rel)
    return git_log(rel)


def version(rel: str, rev: str) -> str:
    _safe(rel)
    return git_show(rev, rel)


def tile_file(tile_source: str) -> str:
    """Ścieżka pliku kafelka względem katalogu konfiguracji."""
    return str(Path(tile_source).resolve().relative_to(CONFIG_DIR.resolve()))
