"""Dziennik akcji: kto (gui / cli / mcp / arbiter), co, z jakimi argumentami, z jakim skutkiem."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR

MAX_BYTES = 2_000_000


class ActionLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG_DIR / "actions.jsonl"
        self.items: deque[dict] = deque(maxlen=1000)
        self.count = 0
        self._lock = threading.Lock()
        try:
            for line in self.path.read_text().splitlines()[-1000:]:
                self.items.append(json.loads(line))
        except (OSError, ValueError):
            pass

    def add(self, source: str, action: str, args: dict[str, Any] | None, result: Any, ok: bool = True) -> dict:
        entry = {"ts": time.time(), "source": source, "action": action,
                 "args": args or {}, "ok": ok, "result": _short(result)}
        with self._lock:
            self.items.append(entry)
            self.count += 1
            try:
                with self.path.open("a") as f:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                if self.path.stat().st_size > MAX_BYTES:
                    lines = self.path.read_text().splitlines()[-1000:]
                    self.path.write_text("\n".join(lines) + "\n")
            except OSError:
                pass
        return entry

    def recent(self, n: int = 100, source: str | None = None) -> list[dict]:
        rows = [e for e in self.items if not source or e["source"] == source]
        return rows[-n:][::-1]


def _short(v: Any, limit: int = 600) -> Any:
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + "…"
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    s = json.dumps(v, ensure_ascii=False, default=str)
    return v if len(s) <= limit else s[:limit] + "…"
