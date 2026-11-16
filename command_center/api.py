"""REST API rdzenia. Te same operacje co GUI i MCP (State.start/stop/apply_profile...)."""
from __future__ import annotations

from typing import Any

from fastapi import Body, HTTPException, Request
from nicegui import app

from . import editor
from .state import State


def source_of(request: Request) -> str:
    return request.headers.get("x-cc-source", "api")[:20]


def split_body(body: dict[str, Any]) -> tuple[str | None, dict]:
    """Ciało {preset, values} albo (dawny format) same wartości opcji."""
    if "values" in body or "preset" in body:
        return body.get("preset"), body.get("values") or {}
    return None, body


def register(st: State) -> None:
    async def guard(fn, *args, **kw):
        try:
            return await fn(*args, **kw)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e))

    def sync_guard(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except (ValueError, RuntimeError, OSError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/status")
    def status():
        return {**st.monitor.snapshot, "owners": st.owners(), "profile": st.store.data.get("profile")}

    @app.get("/api/history")
    def history():
        return list(st.monitor.history)

    @app.get("/api/processes")
    def processes(vram_only: bool = False, limit: int = 100, q: str = ""):
        rows = [p for p in st.monitor.processes if (p["vram"] or not vram_only)
                and (not q or q.lower() in (p["name"] + " " + p["cmd"]).lower())]
        rows.sort(key=lambda p: (-p["vram"], -p["cpu"], -p["rss"]))
        return rows[:limit]

    @app.post("/api/processes/{pid}/kill")
    async def kill(pid: int, request: Request, force: bool = False):
        return {"message": await guard(st.kill, pid, force, source_of(request))}

    @app.post("/api/processes/{pid}/renice")
    async def renice(pid: int, nice: int, request: Request):
        async def run():
            return st.monitor.renice(pid, nice)
        return {"message": await guard(st.act, source_of(request), "renice", {"pid": pid, "nice": nice}, run)}

    @app.get("/api/containers")
    def containers():
        return {"containers": st.docker.containers, "error": st.docker.error}

    @app.post("/api/containers/{name}/{action}")
    async def container_action(name: str, action: str, request: Request):
        return {"message": await guard(st.container_action, name, action, source_of(request))}

    @app.post("/api/projects/{project}/{action}")
    async def project_action(project: str, action: str, request: Request):
        return {"message": await guard(st.project_action, project, action, source_of(request))}

    @app.get("/api/tiles")
    def tiles():
        return [{**t.model_dump(by_alias=True), **st.tile_info(t)} for t in st.registry.tiles.values()]

    @app.post("/api/tiles/{tile_id}/preview")
    def preview(tile_id: str, body: dict = Body(default={})):
        preset, values = split_body(body)
        t = sync_guard(st.tile, tile_id)
        return {"command": st.preview(t, sync_guard(t.values, preset, values))}

    @app.post("/api/tiles/{tile_id}/plan")
    def plan(tile_id: str, body: dict = Body(default={})):
        preset, values = split_body(body)
        return sync_guard(st.plan, tile_id, preset, values).to_dict()

    @app.post("/api/tiles/{tile_id}/start")
    async def start(tile_id: str, request: Request, confirm: bool = False, body: dict = Body(default={})):
        preset, values = split_body(body)
        return await guard(st.start, tile_id, preset, values, confirm, source_of(request))

    @app.post("/api/tiles/{tile_id}/stop")
    async def stop(tile_id: str, request: Request):
        return await guard(st.stop, tile_id, source_of(request))

    @app.get("/api/tiles/{tile_id}/log")
    def log(tile_id: str):
        job = st.runner.jobs.get(sync_guard(st.tile, tile_id).id)
        return {"lines": list(job.lines) if job else [], "rc": job.rc if job else None}

    @app.get("/api/profiles")
    def profiles():
        return {"active": st.store.data.get("profile"),
                "profiles": [p.model_dump() for p in st.registry.profiles.values()]}

    @app.post("/api/profiles/{profile_id}/plan")
    def profile_plan(profile_id: str):
        return sync_guard(st.plan_profile, profile_id).to_dict()

    @app.post("/api/profiles/{profile_id}/apply")
    async def profile_apply(profile_id: str, request: Request, confirm: bool = False):
        return await guard(st.apply_profile, profile_id, confirm, source_of(request))

    @app.get("/api/actions")
    def actions(limit: int = 100, source: str | None = None):
        return st.actions.recent(limit, source)

    @app.get("/api/config/files")
    def config_files():
        return editor.list_files()

    @app.get("/api/config/file")
    def config_read(path: str):
        return {"path": path, "content": sync_guard(editor.read, path), "history": sync_guard(editor.history, path)}

    @app.put("/api/config/file")
    async def config_write(request: Request, body: dict = Body(...)):
        async def run():
            msg = editor.save(body["path"], body["content"], source_of(request))
            st.reload()
            return msg
        return {"message": await guard(st.act, source_of(request), "edit_file", {"path": body.get("path")}, run)}

    @app.post("/api/reload")
    def reload():
        return st.reload()
