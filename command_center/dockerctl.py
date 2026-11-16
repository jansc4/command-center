"""Cienka warstwa nad docker SDK. Gdy Docker jest niedostępny, zwraca puste listy."""
from __future__ import annotations

try:
    import docker
except Exception:
    docker = None

ACTIONS = ("start", "stop", "restart")


class DockerCtl:
    def __init__(self) -> None:
        self._client = None
        self.containers: list[dict] = []
        self.error: str | None = None

    def _c(self):
        if docker is None:
            raise RuntimeError("Brak biblioteki docker")
        if self._client is None:
            self._client = docker.from_env(timeout=10)
        return self._client

    def refresh(self) -> list[dict]:
        try:
            rows = []
            for c in self._c().containers.list(all=True, sparse=True):
                a = c.attrs
                labels = a.get("Labels") or {}
                rows.append({
                    "id": a["Id"],
                    "name": (a.get("Names") or ["?"])[0].lstrip("/"),
                    "image": a.get("Image", ""),
                    "state": a.get("State", ""),      # running / exited / ...
                    "status": a.get("Status", ""),    # "Up 3 hours (healthy)"
                    "project": labels.get("com.docker.compose.project", ""),
                })
            rows.sort(key=lambda r: (r["project"] == "", r["project"], r["name"]))
            self.containers, self.error = rows, None
        except Exception as e:
            self._client = None
            self.containers, self.error = [], f"Docker niedostępny: {e}"
        return self.containers

    def name_of(self, container_id: str) -> str | None:
        for c in self.containers:
            if c["id"] == container_id:
                return c["name"]
        return None

    def state(self, name: str) -> str:
        for c in self.containers:
            if c["name"] == name:
                return c["state"]
        return "missing"

    def project(self, project: str) -> list[dict]:
        return [c for c in self.containers if c["project"] == project]

    def action(self, name: str, action: str) -> str:
        if action not in ACTIONS:
            raise ValueError(f"Nieznana akcja: {action}")
        getattr(self._c().containers.get(name), action)()
        return f"{name}: {action}"

    def logs(self, name: str, tail: int = 200) -> str:
        raw = self._c().containers.get(name).logs(tail=tail, timestamps=False)
        return raw.decode(errors="replace")
