"""REST API rdzenia. Te same funkcje co GUI; baza dla CLI i serwera MCP."""
from __future__ import annotations

import asyncio

from fastapi import Body, HTTPException
from nicegui import app

from .state import State


def register(st: State) -> None:
    def tile(tile_id: str):
        t = st.registry.tiles.get(tile_id)
        if not t:
            raise HTTPException(404, f"Nie ma kafelka {tile_id}")
        return t

    @app.get("/api/status")
    def status():
        return {**st.monitor.snapshot, "owners": st.owners()}

    @app.get("/api/history")
    def history():
        return list(st.monitor.history)

    @app.get("/api/processes")
    def processes(vram_only: bool = False, limit: int = 100):
        rows = [p for p in st.monitor.processes if p["vram"] or not vram_only]
        rows.sort(key=lambda p: (-p["vram"], -p["cpu"], -p["rss"]))
        return rows[:limit]

    @app.post("/api/processes/{pid}/kill")
    def kill(pid: int, force: bool = False):
        try:
            return {"message": st.monitor.kill(pid, force)}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/api/containers")
    def containers():
        return {"containers": st.docker.containers, "error": st.docker.error}

    @app.post("/api/containers/{name}/{action}")
    async def container_action(name: str, action: str):
        try:
            return {"message": await asyncio.to_thread(st.docker.action, name, action)}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/api/tiles")
    def tiles():
        return [{**t.model_dump(), "status": st.status(t), "vram_mib": st.vram_of(t)}
                for t in st.registry.tiles.values()]

    @app.post("/api/tiles/{tile_id}/preview")
    def preview(tile_id: str, values: dict = Body(default={})):
        return {"command": st.preview(tile(tile_id), values)}

    @app.post("/api/tiles/{tile_id}/start")
    async def start(tile_id: str, values: dict = Body(default={})):
        return {"message": await st.start_tile(tile(tile_id), values)}

    @app.post("/api/tiles/{tile_id}/stop")
    async def stop(tile_id: str, values: dict = Body(default={})):
        return {"message": await st.stop_tile(tile(tile_id), values)}

    @app.get("/api/tiles/{tile_id}/log")
    def log(tile_id: str):
        job = st.runner.jobs.get(tile(tile_id).id)
        return {"lines": list(job.lines) if job else [], "rc": job.rc if job else None}

    @app.post("/api/reload")
    def reload():
        st.registry.load()
        return {"tiles": len(st.registry.tiles), "errors": st.registry.errors}
