"""Interfejs NiceGUI: Launcher, Zasoby, Procesy, Docker."""
from __future__ import annotations

import asyncio
import fcntl
import inspect
import os
import pty
import signal
import struct
import subprocess
import termios
import time

from nicegui import ui

from .state import State
from .tiles import Tile, session_env

COLORS = ["#5b8def", "#e6a23c", "#67c23a", "#f56c6c", "#b37feb", "#36cfc9", "#ff85c0", "#9aa0a6"]
STATUS = {
    "running": ("positive", "działa"),
    "starting": ("warning", "startuje"),
    "partial": ("warning", "częściowo"),
    "stopped": ("grey-7", "zatrzymany"),
}
TYPE_LABEL = {"app": "aplikacja", "service": "usługa", "container": "kontener",
              "stack": "stos", "command": "komenda"}


def gb(mib: float | None) -> str:
    return "–" if mib is None else f"{mib / 1024:.1f}"


def register(st: State) -> None:
    @ui.page("/")
    def index() -> None:
        ui.dark_mode().enable()

        # ---------------- pasek górny ----------------
        with ui.header().classes("items-center q-py-sm").style("gap:20px"):
            ui.label("Command Center").classes("text-h6")
            cpu_l = ui.label().classes("text-body2")
            ram_l = ui.label().classes("text-body2")
            vram_l = ui.label().classes("text-body2")
            vram_bar = ui.element("div").classes("col").style(
                "display:flex;height:18px;min-width:160px;border-radius:4px;overflow:hidden;background:#3a3a3a")

        with ui.tabs().classes("w-full") as tabs:
            for name in ("Launcher", "Zasoby", "Procesy", "Docker", "Terminal"):
                ui.tab(name)

        with ui.tab_panels(tabs, value="Launcher").classes("w-full"):
            # ---------------- Launcher ----------------
            cards: dict[str, tuple] = {}
            with ui.tab_panel("Launcher"):
                for err in st.registry.errors:
                    ui.label(f"Błąd definicji: {err}").classes("text-negative")
                for group, tiles in st.registry.groups().items():
                    ui.label(group).classes("text-subtitle1 q-mt-md")
                    with ui.row().classes("w-full").style("gap:12px"):
                        for t in tiles:
                            with ui.card().style("width:270px"):
                                with ui.row().classes("items-center justify-between w-full no-wrap"):
                                    ui.label(t.title).classes("text-subtitle2")
                                    badge = ui.badge("…", color="grey-7")
                                ui.label(TYPE_LABEL[t.type]).classes("text-caption text-grey")
                                vram = ui.label("").classes("text-caption")
                                with ui.row().style("gap:6px"):
                                    ui.button("Start", on_click=lambda t=t: do_start(t, None)).props("dense color=positive")
                                    if t.type != "command":
                                        ui.button("Stop", on_click=lambda t=t: do_stop(t, None)).props("dense color=negative outline")
                                    ui.button("Opcje", on_click=lambda t=t: open_tile(t)).props("dense flat")
                                cards[t.id] = (badge, vram)
                with ui.row().classes("q-mt-lg items-center"):
                    ui.button("Wczytaj definicje ponownie", on_click=lambda: reload_tiles()).props("flat dense")
                    ui.label(f"Kafelki: {st.registry.dir}").classes("text-caption text-grey")

            # ---------------- Zasoby ----------------
            with ui.tab_panel("Zasoby"):
                with ui.row().classes("w-full no-wrap").style("gap:24px"):
                    with ui.column().style("width:320px"):
                        ui.label("CPU").classes("text-subtitle1")
                        core_bars = []
                        cpu_info = ui.label().classes("text-caption")
                        cores_box = ui.column().classes("w-full").style("gap:4px")
                        ui.label("Pamięć").classes("text-subtitle1 q-mt-md")
                        mem_bar = ui.linear_progress(0, show_value=False).props("size=14px")
                        mem_info = ui.label().classes("text-caption")
                        swap_bar = ui.linear_progress(0, show_value=False).props("size=14px color=warning")
                        swap_info = ui.label().classes("text-caption")
                    with ui.column().classes("col"):
                        ui.label("GPU").classes("text-subtitle1")
                        gpu_info = ui.label().classes("text-body2")
                        amd_info = ui.label().classes("text-caption text-grey")
                        owners_table = ui.table(
                            columns=[
                                {"name": "owner", "label": "Właściciel VRAM", "field": "owner", "align": "left"},
                                {"name": "kind", "label": "Rodzaj", "field": "kind", "align": "left"},
                                {"name": "mib", "label": "MiB", "field": "mib"},
                                {"name": "pids", "label": "PID", "field": "pids"},
                            ], rows=[], row_key="owner").classes("w-full").props("dense flat")
                chart = ui.echart({
                    "animation": False,
                    "tooltip": {"trigger": "axis"},
                    "legend": {"top": 0, "textStyle": {"color": "#ccc"}},
                    "grid": {"left": 40, "right": 16, "top": 32, "bottom": 24},
                    "xAxis": {"type": "category", "data": [], "axisLabel": {"color": "#999"}},
                    "yAxis": {"type": "value", "min": 0, "max": 100, "axisLabel": {"color": "#999"}},
                    "series": [{"name": n, "type": "line", "showSymbol": False, "data": []}
                               for n in ("CPU %", "RAM %", "VRAM %", "GPU %")],
                }).classes("w-full").style("height:260px")

            # ---------------- Procesy ----------------
            with ui.tab_panel("Procesy"):
                with ui.row().classes("items-center").style("gap:12px"):
                    pfilter = ui.input("Filtr").props("dense clearable")
                    vram_only = ui.switch("Tylko procesy z VRAM")
                    ui.button("Zakończ (SIGTERM)", on_click=lambda: kill_selected(False)).props("dense color=warning")
                    ui.button("Zabij (SIGKILL)", on_click=lambda: kill_selected(True)).props("dense color=negative")
                ptable = ui.table(
                    columns=[
                        {"name": "pid", "label": "PID", "field": "pid", "sortable": True},
                        {"name": "name", "label": "Nazwa", "field": "name", "sortable": True, "align": "left"},
                        {"name": "user", "label": "Użytkownik", "field": "user", "sortable": True, "align": "left"},
                        {"name": "cpu", "label": "CPU %", "field": "cpu", "sortable": True},
                        {"name": "rss", "label": "RAM MiB", "field": "rss", "sortable": True},
                        {"name": "vram", "label": "VRAM MiB", "field": "vram", "sortable": True},
                        {"name": "cmd", "label": "Komenda", "field": "cmd", "align": "left",
                         "style": "max-width:520px;overflow:hidden;text-overflow:ellipsis"},
                    ], rows=[], row_key="pid", selection="single",
                    pagination={"rowsPerPage": 25, "sortBy": "cpu", "descending": True},
                ).classes("w-full").props("dense flat")
                ptable.bind_filter_from(pfilter, "value")

            # ---------------- Docker ----------------
            with ui.tab_panel("Docker"):
                docker_err = ui.label().classes("text-negative")
                with ui.row().style("gap:8px"):
                    for label, action in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart")):
                        ui.button(label, on_click=lambda a=action: docker_action(a)).props("dense")
                    ui.button("Logi", on_click=lambda: docker_logs()).props("dense flat")
                    ui.link("Portainer", "https://localhost:9443", new_tab=True).classes("q-ml-md self-center")
                dtable = ui.table(
                    columns=[
                        {"name": "project", "label": "Projekt compose", "field": "project", "sortable": True, "align": "left"},
                        {"name": "name", "label": "Kontener", "field": "name", "sortable": True, "align": "left"},
                        {"name": "state", "label": "Stan", "field": "state", "sortable": True, "align": "left"},
                        {"name": "status", "label": "Status", "field": "status", "align": "left"},
                        {"name": "image", "label": "Obraz", "field": "image", "align": "left"},
                    ], rows=[], row_key="name", selection="single",
                    pagination={"rowsPerPage": 50},
                ).classes("w-full").props("dense flat")

            # ---------------- Terminal ----------------
            with ui.tab_panel("Terminal"):
                term = ui.xterm({"cursorBlink": True, "fontSize": 13, "scrollback": 5000}) \
                    .classes("w-full").style("height:72vh")

        shell: dict = {"fd": None, "proc": None}

        def shell_start() -> None:
            if shell["proc"] and shell["proc"].poll() is None:
                return
            master, slave = pty.openpty()
            env = session_env()
            env["TERM"] = "xterm-256color"
            proc = subprocess.Popen(
                ["bash", "-i"], stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, env=env, cwd=os.path.expanduser("~"),
                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
            os.close(slave)
            shell.update(fd=master, proc=proc)
            loop = asyncio.get_running_loop()

            def on_read() -> None:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                try:
                    if data:
                        term.write(data)
                    else:
                        loop.remove_reader(master)
                        os.close(master)
                        shell["fd"] = None
                        term.writeln("\r\n[powłoka zakończona; wejdź ponownie w kartę Terminal, aby uruchomić nową]")
                except Exception:
                    pass

            loop.add_reader(master, on_read)

        def shell_write(text: str) -> None:
            if shell["fd"] is not None:
                os.write(shell["fd"], text.encode())

        def shell_resize(e) -> None:
            if shell["fd"] is not None:
                fcntl.ioctl(shell["fd"], termios.TIOCSWINSZ, struct.pack("HHHH", e.rows, e.cols, 0, 0))

        term.on_data(lambda e: shell_write(e.data))
        term.on_resize(shell_resize)

        async def show_terminal() -> None:
            shell_start()
            await asyncio.sleep(0.3)
            r = term.fit()
            if inspect.isawaitable(r):
                await r

        async def on_tab(e) -> None:
            if e.value == "Terminal":
                await show_terminal()

        tabs.on_value_change(on_tab)

        def shell_cleanup() -> None:
            p = shell["proc"]
            if p and p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass

        client = ui.context.client
        (getattr(client, "on_delete", None) or client.on_disconnect)(shell_cleanup)

        async def run_in_terminal(cmd: str) -> None:
            tabs.set_value("Terminal")
            await show_terminal()
            shell_write(cmd + "\n")

        # ---------------- akcje ----------------
        async def do_start(t: Tile, values: dict | None) -> None:
            try:
                ui.notify(await st.start_tile(t, values), type="positive")
            except Exception as e:
                ui.notify(f"{t.title}: {e}", type="negative")

        async def do_stop(t: Tile, values: dict | None) -> None:
            try:
                ui.notify(await st.stop_tile(t, values))
            except Exception as e:
                ui.notify(f"{t.title}: {e}", type="negative")

        def reload_tiles() -> None:
            st.registry.load()
            ui.navigate.reload()

        def open_tile(t: Tile) -> None:
            values = t.defaults()
            controls: dict[str, ui.element] = {}
            seen = {"n": st.runner.jobs[t.id].count if t.id in st.runner.jobs else 0}

            with ui.dialog() as dlg, ui.card().style("width:860px;max-width:95vw"):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label(t.title).classes("text-h6")
                    ui.label(f"{TYPE_LABEL[t.type]} · {t.source}").classes("text-caption text-grey")
                if t.help:
                    ui.label(t.help).classes("text-body2").style("white-space:pre-line")

                preview = ui.code("", language="bash").classes("w-full")

                def refresh_preview() -> None:
                    preview.set_content(st.preview(t, values) or "(brak komendy)")

                def set_value(opt_id: str, v) -> None:
                    values[opt_id] = v
                    refresh_preview()

                def apply_preset(name: str) -> None:
                    merged = {**t.defaults(), **t.presets.get(name, {})}
                    for k, v in merged.items():
                        if k in controls:
                            controls[k].value = v
                    values.update(merged)
                    refresh_preview()

                if t.presets:
                    ui.select(list(t.presets), label="Preset", value=None,
                              on_change=lambda e: apply_preset(e.value)).props("dense").style("width:240px")

                for o in t.options:
                    with ui.row().classes("items-center w-full no-wrap").style("gap:16px"):
                        label = o.label or o.id
                        if o.type == "bool":
                            c = ui.switch(label, value=bool(o.default))
                        elif o.type == "int":
                            lo, hi = (o.range or [None, None])[:2]
                            c = ui.number(label, value=o.default, min=lo, max=hi, format="%d").props("dense")
                        elif o.type == "enum":
                            c = ui.select(o.values or {}, label=label, value=o.default).props("dense")
                        else:
                            c = ui.input(label, value=o.default or "").props("dense")
                        c.style("min-width:230px")
                        c.on_value_change(lambda e, o=o: set_value(o.id, e.value))
                        controls[o.id] = c
                        hint = f"{o.flag}  " if o.flag else ""
                        ui.label(hint + o.help).classes("text-caption text-grey col")

                with ui.row().style("gap:8px"):
                    ui.button("Start", on_click=lambda: do_start(t, dict(values))).props("color=positive")
                    if t.type != "command":
                        ui.button("Stop", on_click=lambda: do_stop(t, dict(values))).props("color=negative outline")
                    if t.start:
                        async def to_terminal() -> None:
                            dlg.close()
                            await run_in_terminal(st.preview(t, values))
                        ui.button("W terminalu", on_click=to_terminal).props("outline")
                    if t.url:
                        ui.link("Otwórz", t.url, new_tab=True).classes("self-center")
                    ui.button("Zamknij", on_click=dlg.close).props("flat")

                log = ui.log(max_lines=300).classes("w-full").style("height:180px")

                def pump() -> None:
                    job = st.runner.jobs.get(t.id)
                    if not job:
                        return
                    if job.count < seen["n"]:
                        seen["n"] = 0
                    new = job.count - seen["n"]
                    if new > 0:
                        for line in list(job.lines)[-new:]:
                            log.push(line)
                        seen["n"] = job.count

                timer = ui.timer(0.5, pump)
                dlg.on("hide", timer.cancel)
                refresh_preview()
            dlg.open()

        async def confirm(text: str) -> bool:
            with ui.dialog() as d, ui.card():
                ui.label(text).style("white-space:pre-line")
                with ui.row():
                    ui.button("Tak", on_click=lambda: d.submit(True)).props("color=negative")
                    ui.button("Anuluj", on_click=lambda: d.submit(False)).props("flat")
            return bool(await d)

        async def kill_selected(force: bool) -> None:
            if not ptable.selected:
                ui.notify("Zaznacz proces w tabeli")
                return
            row = ptable.selected[0]
            kind, owner = st.resolve_owner(row["pid"])
            text = f"{'Zabić' if force else 'Zakończyć'} {row['name']} (PID {row['pid']})?"
            if kind == "kafelek":
                text += f"\nTen proces należy do kafelka „{owner}”. Lepiej użyć jego przycisku Stop, który sprząta po usłudze."
            elif kind == "kontener":
                text += f"\nTen proces działa w kontenerze „{owner}”. Lepiej zatrzymać kontener."
            if await confirm(text):
                try:
                    ui.notify(st.monitor.kill(row["pid"], force))
                    ptable.selected.clear()
                except Exception as e:
                    ui.notify(str(e), type="negative")

        async def docker_action(action: str) -> None:
            if not dtable.selected:
                ui.notify("Zaznacz kontener w tabeli")
                return
            name = dtable.selected[0]["name"]
            try:
                ui.notify(await asyncio.to_thread(st.docker.action, name, action))
                await asyncio.to_thread(st.docker.refresh)
            except Exception as e:
                ui.notify(str(e), type="negative")

        async def docker_logs() -> None:
            if not dtable.selected:
                ui.notify("Zaznacz kontener w tabeli")
                return
            name = dtable.selected[0]["name"]
            try:
                text = await asyncio.to_thread(st.docker.logs, name)
            except Exception as e:
                ui.notify(str(e), type="negative")
                return
            with ui.dialog() as d, ui.card().style("width:1000px;max-width:95vw"):
                ui.label(f"Logi: {name}").classes("text-h6")
                box = ui.log().classes("w-full").style("height:60vh")
                for line in text.splitlines():
                    box.push(line)
                ui.button("Zamknij", on_click=d.close).props("flat")
            d.open()

        # ---------------- odświeżanie ----------------
        def refresh() -> None:
            snap = st.monitor.snapshot
            if not snap:
                return
            cpu, mem, swap = snap["cpu"], snap["mem"], snap["swap"]
            cpu_l.set_text(f"CPU {cpu['total']:.0f}%")
            ram_l.set_text(f"RAM {gb(mem['used'])}/{gb(mem['total'])} GB")

            owners = st.owners()
            gpus = snap["gpus"]
            vram_bar.clear()
            if gpus:
                g = gpus[0]
                vram_l.set_text(f"VRAM {gb(g['mem_used'])}/{gb(g['mem_total'])} GB")
                with vram_bar:
                    accounted = 0
                    for i, o in enumerate(owners):
                        accounted += o["mib"]
                        with ui.element("div").style(
                                f"width:{100 * o['mib'] / g['mem_total']:.2f}%;background:{COLORS[i % len(COLORS)]}"):
                            ui.tooltip(f"{o['owner']} ({o['kind']}): {o['mib']} MiB")
                    other = g["mem_used"] - accounted
                    if other > 50:
                        with ui.element("div").style(f"width:{100 * other / g['mem_total']:.2f}%;background:#777"):
                            ui.tooltip(f"inne / system: {other} MiB")
                gpu_info.set_text(
                    f"{g['name']}: {g['mem_used']} / {g['mem_total']} MiB, obciążenie {g['util']}%, "
                    f"{g['temp']}°C, {g['power_w']} W")
            else:
                vram_l.set_text("VRAM: brak danych NVML")
                gpu_info.set_text("Brak karty NVIDIA lub NVML niedostępny")
            amd_info.set_text(" · ".join(
                f"{a['name']}: obciążenie {a['util']}%, VRAM {a['mem_used']}/{a['mem_total']} MiB"
                for a in snap["amd"]))
            owners_table.rows[:] = [{**o, "pids": ", ".join(map(str, o["pids"]))} for o in owners]
            owners_table.update()

            # zasoby
            if len(core_bars) != len(cpu["per_core"]):
                cores_box.clear()
                core_bars.clear()
                with cores_box:
                    for _ in cpu["per_core"]:
                        core_bars.append(ui.linear_progress(0, show_value=False).props("size=10px"))
            for bar, v in zip(core_bars, cpu["per_core"]):
                bar.set_value(v / 100)
            temp = f", {cpu['temp']:.0f}°C" if cpu["temp"] else ""
            cpu_info.set_text(f"{cpu['total']:.0f}% średnio, {len(cpu['per_core'])} rdzeni{temp}")
            mem_bar.set_value(mem["percent"] / 100)
            mem_info.set_text(f"RAM {mem['used']} / {mem['total']} MiB ({mem['percent']:.0f}%)")
            swap_bar.set_value(swap["percent"] / 100)
            swap_info.set_text(f"Swap {swap['used']} / {swap['total']} MiB")

            hist = list(st.monitor.history)[-300:]
            chart.options["xAxis"]["data"] = [time.strftime("%H:%M:%S", time.localtime(h["ts"])) for h in hist]
            for s, key in zip(chart.options["series"], ("cpu", "mem", "vram", "gpu")):
                s["data"] = [h[key] for h in hist]
            chart.update()

            # kafelki
            for tid, (badge, vram) in cards.items():
                t = st.registry.tiles.get(tid)
                if not t:
                    continue
                color, text = STATUS[st.status(t)]
                badge.set_text(text)
                badge.props(f"color={color}")
                used = st.vram_of(t)
                vram.set_text(f"VRAM {used} MiB" if used else "")

            # procesy
            rows = [p for p in st.monitor.processes if p["vram"] or not vram_only.value]
            rows.sort(key=lambda p: (-p["vram"], -p["cpu"], -p["rss"]))
            ptable.rows[:] = rows[:400]
            ptable.update()

            # docker
            docker_err.set_text(st.docker.error or "")
            dtable.rows[:] = [{k: c[k] for k in ("project", "name", "state", "status", "image")}
                              for c in st.docker.containers]
            dtable.update()

        ui.timer(2.0, refresh)
        ui.timer(0.3, refresh, once=True)
