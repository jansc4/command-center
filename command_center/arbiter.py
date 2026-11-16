"""Arbiter VRAM: plan (kto ustępuje, ile zostanie wolnego), wykonanie i przywracanie.

Zależności między usługami żyją w definicjach kafelków:
  vram: {budget_mib, priority, yield, resume}   jak dużo zajmuje, jak ważny, jak ustępuje i wraca
  on_start: [{yield: X, if/unless: opcja}]      kogo zawsze odsunąć przy starcie
  on_stop: restore                              po zatrzymaniu przywróć to, co odsunięto
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from .tiles import Profile, Step, Tile

if TYPE_CHECKING:
    from .state import State

ACTIVE = ("running", "partial", "starting", "unhealthy")


@dataclass
class PlanStep:
    action: str                  # stop | yield | start
    tile: str
    title: str
    detail: str
    mib: int = 0                 # zwalnia (stop/yield) albo potrzebuje (start)
    by: str | None = None        # yield: na rzecz którego kafelka
    how: dict | None = None      # yield: krok (stop / http_post)
    values: dict | None = None   # start: wartości opcji
    estimate: str = ""           # start: skąd liczba (budżet / pomiar)


@dataclass
class Plan:
    target: str
    title: str
    total_mib: int
    free_now: int
    margin_mib: int
    need_mib: int = 0
    free_after: int = 0
    steps: list[PlanStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.free_after >= 0

    @property
    def trivial(self) -> bool:
        """Sam start bez odsuwania innych i bez ostrzeżeń: można wykonać bez pytania."""
        return not self.warnings and all(s.action == "start" for s in self.steps)

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok, "trivial": self.trivial, "text": self.text()}

    def text(self) -> str:
        lines = [f"Plan: {self.title}"]
        verbs = {"stop": "zatrzymaj", "yield": "odsuń", "start": "uruchom"}
        for i, s in enumerate(self.steps, 1):
            line = f"{i}. {verbs[s.action]} {s.title}"
            if s.detail:
                line += f": {s.detail}"
            if s.mib:
                line += f" (zwalnia ~{s.mib} MiB)" if s.action != "start" else f" (~{s.mib} MiB, {s.estimate})"
            lines.append(line)
        lines.append(f"VRAM: wolne teraz {self.free_now} MiB, zapas {self.margin_mib} MiB, "
                     f"potrzeba {self.need_mib} MiB, po wykonaniu {self.free_after} MiB")
        lines += [f"Uwaga: {w}" for w in self.warnings]
        lines += [f"({n})" for n in self.notes]
        return "\n".join(lines)


class Sim:
    """Symulowany stan karty na czas układania planu."""

    def __init__(self, st: State) -> None:
        gpus = st.monitor.snapshot.get("gpus") or []
        g = gpus[0] if gpus else {"mem_total": 0, "mem_used": 0}
        self.total = g["mem_total"]
        self.free_now = g["mem_total"] - g["mem_used"]
        self.margin = st.config.vram_margin_mib
        self.free = self.free_now - self.margin
        self.running: dict[str, int] = {t.id: st.vram_of(t) for t in st.registry.tiles.values()
                                        if st.status(t) in ACTIVE}
        self.gone: set[str] = set()      # zatrzymane albo odsunięte w tym planie


def values_key(values: dict | None) -> str:
    return json.dumps(values or {}, sort_keys=True, default=str)


def estimate(st: State, t: Tile, values: dict) -> tuple[int, str]:
    measured = st.store["measured"].get(t.id, {}).get(values_key(values))
    if measured:
        return int(measured), "pomiar"
    try:
        b = t.budget(values)
    except Exception as e:
        return 0, f"błąd budżetu: {e}"
    return (b, "budżet") if b is not None else (0, "brak budżetu")


def _yield(st: State, sim: Sim, plan: Plan, target: Tile, by: Tile, reason: str) -> None:
    how = target.vram.yield_step() if target.vram else Step(kind="stop")
    frees = sim.running.get(target.id, 0)
    detail = ("zatrzymanie" if how.kind == "stop" else f"przełączenie ({how.describe()})") + f"; {reason}"
    plan.steps.append(PlanStep("yield", target.id, target.title, detail, frees, by=by.id,
                               how=how.model_dump(by_alias=True)))
    sim.free += frees
    sim.gone.add(target.id)
    if how.kind == "stop":
        sim.running.pop(target.id, None)
    else:
        sim.running[target.id] = 0


def plan_start(st: State, sim: Sim, plan: Plan, t: Tile, values: dict, profile: bool = False) -> None:
    if t.id in sim.running and t.id not in sim.gone:
        current = st.store["started"].get(t.id, {}).get("values")
        if not profile:
            plan.warnings.append(f"{t.title} już działa; pomijam start (zatrzymaj, aby zmienić opcje)")
            return
        if t.type in ("container", "stack") or current is None or values_key(current) == values_key(values):
            plan.notes.append(f"{t.title} już działa")
            return
        frees = sim.running.pop(t.id)
        sim.free += frees
        sim.gone.add(t.id)
        plan.steps.append(PlanStep("stop", t.id, t.title, "działa z innymi opcjami, restart", frees))
    need, src = estimate(st, t, values)
    plan.need_mib += need
    prio = t.vram.priority if t.vram else 50
    for h in t.on_start:
        target = st.registry.tiles.get(h.yield_)
        if target and h.active(values) and target.id in sim.running and target.id not in sim.gone:
            _yield(st, sim, plan, target, t, "zależność on_start")
    if need > sim.free:
        candidates = sorted(
            (st.registry.tiles[i] for i, mib in sim.running.items()
             if i not in sim.gone and mib > 0 and i in st.registry.tiles),
            key=lambda x: x.vram.priority if x.vram else 50)
        for c in candidates:
            if need <= sim.free:
                break
            if c.vram and c.vram.priority < prio:
                _yield(st, sim, plan, c, t, f"brak VRAM, priorytet {c.vram.priority} < {prio}")
    if need > sim.free:
        blockers = [f"{st.registry.tiles[i].title} ({mib} MiB)" for i, mib in sim.running.items()
                    if i not in sim.gone and mib > 0 and i in st.registry.tiles]
        plan.warnings.append(f"{t.title}: brakuje ~{need - sim.free} MiB VRAM"
                             + (f"; zajmują: {', '.join(blockers)}" if blockers else ""))
    plan.steps.append(PlanStep("start", t.id, t.title, "", need, values=values, estimate=src))
    sim.free -= need
    sim.running[t.id] = need


def _finish(plan: Plan, sim: Sim) -> Plan:
    plan.free_after = sim.free
    return plan


def plan_tile(st: State, t: Tile, values: dict) -> Plan:
    sim = Sim(st)
    plan = Plan(t.id, t.title, sim.total, sim.free_now, sim.margin)
    plan_start(st, sim, plan, t, values)
    return _finish(plan, sim)


def plan_profile(st: State, p: Profile) -> Plan:
    sim = Sim(st)
    plan = Plan(f"profile:{p.id}", f"profil „{p.title}”", sim.total, sim.free_now, sim.margin)
    for tid in p.stop:
        t = st.registry.tiles.get(tid)
        if t and tid in sim.running:
            frees = sim.running.pop(tid)
            sim.free += frees
            sim.gone.add(tid)
            plan.steps.append(PlanStep("stop", tid, t.title, "z listy stop profilu", frees))
    for item in p.start:
        t = st.registry.tiles.get(item.tile)
        if not t:
            plan.warnings.append(f"Nie ma kafelka {item.tile}")
            continue
        try:
            vals = t.values(item.preset, item.values)
        except ValueError as e:
            plan.warnings.append(str(e))
            continue
        plan_start(st, sim, plan, t, vals, profile=True)
    return _finish(plan, sim)


# ---------------- wykonanie ----------------
def http_post(url: str, payload: dict | None) -> str:
    req = urllib.request.Request(url, data=json.dumps(payload or {}).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode(errors="replace")


async def do_step(st: State, t: Tile, step: Step) -> str:
    if step.kind == "stop":
        if st.status(t) == "stopped":
            return f"{t.title}: już zatrzymany"
        msg = await st.stop_tile(t, restore=False)
        await st.wait_status(t, ("stopped",), 40)
        return msg
    if step.kind == "start":
        if st.status(t) in ACTIVE:
            return f"{t.title}: już działa"
        return await st.start_tile(t, t.defaults())
    out = await asyncio.to_thread(http_post, step.url, step.json_)
    if '"restarting":true' in out.replace(" ", ""):
        await asyncio.sleep(4)   # proces oddaje kartę
    return f"{t.title}: {step.describe()} -> {out[:120]}"


def _record(st: State, by: str, target: str, how: dict) -> None:
    rows = st.store["yielded"].setdefault(by, [])
    if not any(r["target"] == target for r in rows):
        rows.append({"target": target, "how": how, "ts": time.time()})
    st.store.save()


async def execute(st: State, plan: Plan, source: str) -> list[str]:
    msgs: list[str] = []
    orphans: dict[str, dict] = {}
    async with st.arbiter_lock:
        for i, s in enumerate(plan.steps):
            t = st.registry.tiles.get(s.tile)
            if not t:
                msgs.append(f"Nie ma kafelka {s.tile}")
                continue
            try:
                if s.action == "stop":
                    for r in st.store["yielded"].pop(t.id, []):
                        orphans[r["target"]] = r
                    st.store.save()
                    msgs.append(await do_step(st, t, Step(kind="stop")))
                elif s.action == "yield":
                    if s.how.get("kind") == "stop":
                        for r in st.store["yielded"].pop(t.id, []):
                            orphans[r["target"]] = r
                    msgs.append(await do_step(st, t, Step(**s.how)))
                    _record(st, s.by, t.id, s.how)
                    orphans.pop(t.id, None)
                elif s.action == "start":
                    msgs.append(await st.start_tile(t, s.values))
                    st.schedule_measure(t, s.values or {})
                    if i < len(plan.steps) - 1:
                        await st.wait_status(t, ACTIVE, 60)
            except Exception as e:
                msgs.append(f"{t.title}: błąd: {e}")
        # odsunięte przez zatrzymane kafelki, których nikt nie przejął i nie ma na liście stop
        stopped = {s.tile for s in plan.steps if s.action == "stop"}
        for target, r in orphans.items():
            if target not in stopped:
                msgs += await resume_one(st, target, r)
    st.actions.add(source, "plan", {"target": plan.target}, msgs, True)
    return msgs


async def resume_one(st: State, target_id: str, record: dict) -> list[str]:
    t = st.registry.tiles.get(target_id)
    if not t:
        return []
    for other, rows in st.store["yielded"].items():
        if any(r["target"] == target_id for r in rows):
            return [f"{t.title}: zostaje odsunięty (potrzebuje go {other})"]
    yielded = Step(**record["how"]) if record.get("how") else Step(kind="stop")
    resume = t.vram.resume_step() if t.vram else (Step(kind="start") if yielded.kind == "stop" else None)
    if not resume:
        return []
    try:
        return [await do_step(st, t, resume)]
    except Exception as e:
        return [f"{t.title}: nie udało się przywrócić: {e}"]


async def restore(st: State, t: Tile, source: str) -> list[str]:
    """Przywraca to, co kafelek odsunął przy starcie (odpowiednik kokoro_was_running)."""
    rows = st.store["yielded"].pop(t.id, [])
    st.store.save()
    if t.on_stop == "none" or not rows:
        return []
    msgs: list[str] = []
    for r in rows:
        msgs += await resume_one(st, r["target"], r)
    if msgs:
        st.actions.add(source, "restore", {"tile": t.id}, msgs, True)
    return msgs
