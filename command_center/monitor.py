"""Próbkowanie CPU, RAM, GPU i procesów. Bez zależności od GUI."""
from __future__ import annotations

import glob
import os
import signal
import time
from collections import deque
from typing import Callable

import psutil

try:
    import pynvml
except Exception:  # brak biblioteki = brak danych NVIDIA, reszta działa
    pynvml = None

MIB = 1024 * 1024
HISTORY = 600  # próbek (1 Hz = 10 minut)


def _try(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


class Monitor:
    def __init__(self) -> None:
        self.snapshot: dict = {}
        self.history: deque[dict] = deque(maxlen=HISTORY)
        self.processes: list[dict] = []
        # (pid) -> (rodzaj, nazwa); ustawiane przez State
        self.resolve_owner: Callable[[int], tuple[str, str]] = lambda pid: ("proces", str(pid))
        self._nvml_ok = False
        psutil.cpu_percent(percpu=True)  # pierwsze wywołanie ustawia punkt odniesienia

    # ---------- GPU ----------
    def _nvidia(self) -> list[dict]:
        if pynvml is None:
            return []
        try:
            if not self._nvml_ok:
                pynvml.nvmlInit()
                self._nvml_ok = True
            out = []
            for i in range(pynvml.nvmlDeviceGetCount()):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                name = pynvml.nvmlDeviceGetName(h)
                if isinstance(name, bytes):
                    name = name.decode()
                util = _try(pynvml.nvmlDeviceGetUtilizationRates, h)
                power = _try(pynvml.nvmlDeviceGetPowerUsage, h)
                per_pid: dict[int, int] = {}
                for fn in (pynvml.nvmlDeviceGetComputeRunningProcesses,
                           pynvml.nvmlDeviceGetGraphicsRunningProcesses):
                    for p in _try(fn, h) or []:
                        mib = (p.usedGpuMemory or 0) // MIB
                        per_pid[p.pid] = max(per_pid.get(p.pid, 0), mib)
                procs = []
                for pid, mib in sorted(per_pid.items(), key=lambda kv: -kv[1]):
                    kind, owner = self.resolve_owner(pid)
                    procs.append({"pid": pid, "mib": mib, "kind": kind, "owner": owner})
                out.append({
                    "index": i,
                    "name": name,
                    "mem_used": mem.used // MIB,
                    "mem_total": mem.total // MIB,
                    "util": util.gpu if util else None,
                    "temp": _try(pynvml.nvmlDeviceGetTemperature, h, pynvml.NVML_TEMPERATURE_GPU),
                    "power_w": round(power / 1000, 1) if power else None,
                    "procs": procs,
                })
            return out
        except Exception:
            self._nvml_ok = False
            return []

    @staticmethod
    def _amd() -> list[dict]:
        """Karty AMD przez sysfs (RX 570 od obrazu)."""
        out, seen = [], set()
        for dev in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
            real = os.path.realpath(dev)
            if real in seen:
                continue
            seen.add(real)

            def read(name: str) -> str | None:
                try:
                    with open(os.path.join(real, name)) as f:
                        return f.read().strip()
                except OSError:
                    return None

            if read("vendor") != "0x1002":
                continue
            used, total, busy = read("mem_info_vram_used"), read("mem_info_vram_total"), read("gpu_busy_percent")
            out.append({
                "name": "AMD " + os.path.basename(os.path.dirname(dev)),
                "util": int(busy) if busy and busy.isdigit() else None,
                "mem_used": int(used) // MIB if used else None,
                "mem_total": int(total) // MIB if total else None,
            })
        return out

    # ---------- próbka ----------
    def sample(self) -> dict:
        per_core = psutil.cpu_percent(percpu=True)
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        temps = _try(psutil.sensors_temperatures) or {}
        core_temp = max((t.current for t in temps.get("coretemp", [])), default=None)
        snap = {
            "ts": time.time(),
            "cpu": {
                "total": round(sum(per_core) / max(len(per_core), 1), 1),
                "per_core": per_core,
                "temp": core_temp,
            },
            "mem": {"used": (vm.total - vm.available) // MIB, "total": vm.total // MIB, "percent": vm.percent},
            "swap": {"used": sw.used // MIB, "total": sw.total // MIB, "percent": sw.percent},
            "gpus": self._nvidia(),
            "amd": self._amd(),
        }
        self.snapshot = snap
        g = snap["gpus"][0] if snap["gpus"] else None
        self.history.append({
            "ts": snap["ts"],
            "cpu": snap["cpu"]["total"],
            "mem": vm.percent,
            "vram": round(100 * g["mem_used"] / g["mem_total"], 1) if g else None,
            "gpu": g["util"] if g else None,
        })
        return snap

    def sample_processes(self) -> list[dict]:
        vram = {p["pid"]: p["mib"] for g in self.snapshot.get("gpus", []) for p in g["procs"]}
        rows = []
        for p in psutil.process_iter(["pid", "ppid", "name", "username", "cpu_percent", "memory_info", "cmdline", "nice"]):
            i = p.info
            mi = i.get("memory_info")
            try:
                sid = os.getsid(i["pid"])
            except OSError:
                sid = None
            rows.append({
                "pid": i["pid"],
                "sid": sid,
                "ppid": i.get("ppid"),
                "nice": i.get("nice"),
                "name": i.get("name") or "",
                "user": i.get("username") or "",
                "cpu": round(i.get("cpu_percent") or 0.0, 1),
                "rss": round(mi.rss / MIB) if mi else 0,
                "vram": vram.get(i["pid"], 0),
                "cmd": " ".join(i.get("cmdline") or [])[:400],
            })
        self.processes = rows
        return rows

    # ---------- akcje ----------
    @staticmethod
    def kill(pid: int, force: bool = False) -> str:
        if pid <= 1 or pid == os.getpid():
            raise ValueError("Tego procesu nie zakończę.")
        p = psutil.Process(pid)
        name = p.name()
        p.send_signal(signal.SIGKILL if force else signal.SIGTERM)
        return f"Wysłano {'SIGKILL' if force else 'SIGTERM'} do {name} (PID {pid})"

    @staticmethod
    def renice(pid: int, nice: int) -> str:
        if pid <= 1:
            raise ValueError("Tego procesu nie ruszam.")
        if not -20 <= nice <= 19:
            raise ValueError("nice musi być w zakresie -20..19")
        p = psutil.Process(pid)
        old = p.nice()
        p.nice(nice)
        return f"{p.name()} (PID {pid}): nice {old} -> {nice}"
