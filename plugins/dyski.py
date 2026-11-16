"""Przykładowa wtyczka: zajętość dysków (w tym zewnętrznych). Kolektor + karta GUI + narzędzie MCP.

Wzór dla własnych wtyczek: plik w ~/.config/command-center/plugins/, funkcja register(ctx).
Plik z nazwą zaczynającą się od "_" jest pomijany.
"""
import psutil

SKIP = ("/snap", "/boot/efi", "/var/lib/docker")
data: dict = {"disks": []}


def collect() -> None:
    rows = []
    for p in psutil.disk_partitions(all=False):
        if p.mountpoint.startswith(SKIP) or p.fstype in ("squashfs", "tmpfs", "overlay"):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        rows.append({"mount": p.mountpoint, "device": p.device, "fstype": p.fstype,
                     "used_gb": round(u.used / 1e9, 1), "total_gb": round(u.total / 1e9, 1),
                     "percent": u.percent})
    data["disks"] = rows


def disk_usage() -> dict:
    """Zajętość zamontowanych dysków (GB, %), także zewnętrznych."""
    collect()
    return data


def build() -> None:
    from nicegui import ui

    table = ui.table(columns=[
        {"name": "mount", "label": "Punkt montowania", "field": "mount", "align": "left", "sortable": True},
        {"name": "device", "label": "Urządzenie", "field": "device", "align": "left"},
        {"name": "fstype", "label": "System plików", "field": "fstype", "align": "left"},
        {"name": "used_gb", "label": "Zajęte GB", "field": "used_gb", "sortable": True},
        {"name": "total_gb", "label": "Razem GB", "field": "total_gb", "sortable": True},
        {"name": "percent", "label": "%", "field": "percent", "sortable": True},
    ], rows=[], row_key="mount").classes("w-full").props("dense flat")

    def refresh() -> None:
        table.rows[:] = data["disks"]
        table.update()

    ui.timer(5, refresh)
    ui.timer(0.2, refresh, once=True)


def register(ctx) -> None:
    collect()
    ctx.add_collector(collect, every=30)
    ctx.add_tab("Dyski", build)
    ctx.add_mcp_tool(disk_usage, level="read")
