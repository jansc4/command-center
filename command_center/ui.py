"""Interfejs NiceGUI: Launcher, Zasoby, Procesy, Docker, Terminal, Dziennik, Konfiguracja (+ karty wtyczek)."""
from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict

from nicegui import ui

from . import editor
from .config import CONFIG_DIR
from .plugins import LOADED, TABS
from .state import State
from .tiles import Tile

COLORS = ["#5b8def", "#e6a23c", "#67c23a", "#f56c6c", "#b37feb", "#36cfc9", "#ff85c0", "#9aa0a6"]
STATUS = {
    "running": ("positive", "działa"),
    "starting": ("warning", "startuje"),
    "partial": ("warning", "częściowo"),
    "unhealthy": ("negative", "nie odpowiada"),
    "stopped": ("grey-7", "zatrzymany"),
}
TYPE_LABEL = {"app": "aplikacja", "service": "usługa", "container": "kontener",
              "stack": "stos", "command": "komenda"}
SOURCE_COLOR = {"gui": "primary", "cli": "teal", "api": "teal", "mcp": "purple", "arbiter": "orange", "tray": "cyan"}


def gb(mib: float | None) -> str:
    return "–" if mib is None else f"{mib / 1024:.1f}"


def tree_order(rows: list[dict]) -> list[dict]:
    """Procesy w kolejności drzewa (rodzic, potem dzieci) z wcięciem w nazwie."""
    by_pid = {r["pid"]: r for r in rows}
    kids: dict[int, list[dict]] = defaultdict(list)
    roots = []
    for r in rows:
        (kids[r["ppid"]] if r.get("ppid") in by_pid and r["ppid"] != r["pid"] else roots).append(r)
    out: list[dict] = []
    stack = [(r, 0) for r in sorted(roots, key=lambda x: -x["pid"])]
    while stack:
        r, d = stack.pop()
        out.append({**r, "name": (" " * d + "└ " if d else "") + r["name"]})
        stack += [(c, d + 1) for c in sorted(kids[r["pid"]], key=lambda x: -x["pid"])]
    return out


def mcp_snippets(port: int = 8770) -> dict[str, str]:
    url = f"http://127.0.0.1:{port}/mcp"
    return {
        "Hermes (~/.hermes/config.yaml, sekcja mcp_servers)": f"""mcp_servers:
  command-center:
    url: {url}
    timeout: 300
    connect_timeout: 30""",
        "opencode (~/.config/opencode/opencode.json)": f"""{{
  "mcp": {{
    "command-center": {{ "type": "remote", "url": "{url}", "enabled": true }}
  }}
}}""",
        "Claude Desktop (claude_desktop_config.json, most stdio przez mcp-remote)": f"""{{
  "mcpServers": {{
    "command-center": {{ "command": "npx", "args": ["-y", "mcp-remote", "{url}"] }}
  }}
}}""",
        "Claude Code (terminal)": f"claude mcp add --transport http command-center {url}",
    }


def register(st: State, mcp=None) -> None:
    @ui.page("/")
    def index() -> None:
        ui.dark_mode().enable()
        seen = {"version": st.registry.version, "actions": -1}

        # ================= wspólne dialogi =================
        async def confirm(text: str, ok: str = "Tak", color: str = "negative") -> bool:
            with ui.dialog() as d, ui.card():
                ui.label(text).style("white-space:pre-line")
                with ui.row():
                    ui.button(ok, on_click=lambda: d.submit(True)).props(f"color={color}")
                    ui.button("Anuluj", on_click=lambda: d.submit(False)).props("flat")
            return bool(await d)

        def plan_view(plan: dict) -> None:
            verbs = {"stop": ("stop", "negative", "zatrzymaj"), "yield": ("swap_horiz", "warning", "odsuń"),
                     "start": ("play_arrow", "positive", "uruchom")}
            for s in plan["steps"]:
                icon, color, verb = verbs[s["action"]]
                with ui.row().classes("items-center no-wrap w-full").style("gap:8px"):
                    ui.icon(icon, color=color)
                    ui.label(f"{verb} {s['title']}").classes("text-body2 text-weight-medium")
                    extra = s["detail"]
                    if s["mib"]:
                        extra += (f" · zwalnia ~{s['mib']} MiB" if s["action"] != "start"
                                  else f" · ~{s['mib']} MiB ({s['estimate']})")
                    ui.label(extra.strip(" ·")).classes("text-caption text-grey")
            total = max(plan["total_mib"], 1)
            used_after = total - plan["free_after"] - plan["margin_mib"]
            ui.label(f"VRAM: wolne teraz {plan['free_now']} MiB · potrzeba {plan['need_mib']} MiB · "
                     f"po wykonaniu {plan['free_after']} MiB (zapas {plan['margin_mib']} MiB)").classes("text-caption q-mt-sm")
            ui.linear_progress(min(max(used_after / total, 0), 1), show_value=False).props(
                f"size=12px color={'positive' if plan['ok'] else 'negative'}")
            for w in plan["warnings"]:
                ui.label("⚠ " + w).classes("text-warning text-body2")
            for n in plan.get("notes", []):
                ui.label(n).classes("text-caption text-grey")

        async def confirm_plan(plan: dict, title: str) -> bool:
            with ui.dialog() as d, ui.card().style("width:720px;max-width:95vw"):
                ui.label(title).classes("text-h6")
                plan_view(plan)
                with ui.row().classes("q-mt-md"):
                    ui.button("Wykonaj" if plan["ok"] else "Wykonaj mimo to", on_click=lambda: d.submit(True)) \
                        .props(f"color={'positive' if plan['ok'] else 'warning'}")
                    ui.button("Anuluj", on_click=lambda: d.submit(False)).props("flat")
            return bool(await d)

        def notify_result(res: dict) -> None:
            msgs = res.get("messages") or ([res["message"]] if res.get("message") else [])
            if msgs:
                ui.notify("\n".join(msgs), type="positive", multi_line=True, timeout=6000,
                          classes="multi-line-notification")

        # ================= akcje kafelków i profili =================
        async def do_start(t: Tile, values: dict | None) -> None:
            try:
                res = await st.start(t.id, None, values, False, "gui")
                if res.get("needs_confirm"):
                    if not await confirm_plan(res["plan"], f"Start: {t.title}"):
                        return
                    res = await st.start(t.id, None, values, True, "gui")
                notify_result(res)
            except Exception as e:
                ui.notify(f"{t.title}: {e}", type="negative")

        async def do_stop(t: Tile) -> None:
            try:
                notify_result(await st.stop(t.id, "gui"))
            except Exception as e:
                ui.notify(f"{t.title}: {e}", type="negative")

        async def do_profile(pid: str) -> None:
            p = st.registry.profiles[pid]
            try:
                res = await st.apply_profile(pid, False, "gui")
                if not await confirm_plan(res["plan"], f"Profil: {p.title}"):
                    return
                ui.notify(f"Przełączam na profil {p.title}…")
                notify_result(await st.apply_profile(pid, True, "gui"))
                header_profiles.refresh()
            except Exception as e:
                ui.notify(f"Profil {p.title}: {e}", type="negative")

        # ================= pasek górny =================
        with ui.header().classes("items-center q-py-sm").style("gap:20px"):
            ui.label("Command Center").classes("text-h6")
            cpu_l = ui.label().classes("text-body2")
            ram_l = ui.label().classes("text-body2")
            vram_l = ui.label().classes("text-body2")
            vram_bar = ui.element("div").classes("col").style(
                "display:flex;height:18px;min-width:160px;border-radius:4px;overflow:hidden;background:#3a3a3a")

            @ui.refreshable
            def header_profiles() -> None:
                active = st.store.data.get("profile")
                title = st.registry.profiles[active].title if active in st.registry.profiles else "brak"
                with ui.dropdown_button(f"Profil: {title}", icon="tune", auto_close=True, color=None) \
                        .props("flat dense no-caps text-color=white"):
                    for p in st.registry.profiles.values():
                        with ui.item(on_click=lambda p=p: do_profile(p.id)):
                            with ui.item_section():
                                ui.item_label(("● " if p.id == active else "") + p.title)
                                ui.item_label(p.help).props("caption")
            header_profiles()

        tab_names = ["Launcher", "Zasoby", "Procesy", "Docker", "Terminal", "Dziennik", "Konfiguracja"]
        tab_names += [name for name, _ in TABS]
        with ui.tabs().classes("w-full") as tabs:
            for name in tab_names:
                ui.tab(name)

        cards: dict[str, dict] = {}
        with ui.tab_panels(tabs, value="Launcher").classes("w-full"):
            # ---------------- Launcher ----------------
            with ui.tab_panel("Launcher"):
                @ui.refreshable
                def launcher() -> None:
                    cards.clear()
                    for err in st.registry.errors:
                        ui.label(f"Błąd definicji: {err}").classes("text-negative")
                    for group, tiles in st.registry.groups().items():
                        ui.label(group).classes("text-subtitle1 q-mt-md")
                        with ui.row().classes("w-full").style("gap:12px"):
                            for t in tiles:
                                with ui.card().style("width:280px"):
                                    with ui.row().classes("items-center justify-between w-full no-wrap"):
                                        ui.label(t.title).classes("text-subtitle2")
                                        badge = ui.badge("…", color="grey-7")
                                    kind = TYPE_LABEL[t.type]
                                    if t.vram and t.budget_safe() is not None:
                                        kind += f" · budżet {t.budget_safe()} MiB"
                                    ui.label(kind).classes("text-caption text-grey")
                                    vram = ui.label("").classes("text-caption")
                                    extra = ui.label("").classes("text-caption text-grey")
                                    with ui.row().style("gap:6px"):
                                        ui.button("Start", on_click=lambda t=t: do_start(t, None)).props("dense color=positive")
                                        if t.type != "command":
                                            ui.button("Stop", on_click=lambda t=t: do_stop(t)).props("dense color=negative outline")
                                        ui.button("Opcje", on_click=lambda t=t: open_tile(t)).props("dense flat")
                                        if t.url:
                                            ui.button(icon="open_in_new").props(f"dense flat href={t.url} target=_blank")
                                    cards[t.id] = {"badge": badge, "vram": vram, "extra": extra}
                    with ui.row().classes("q-mt-lg items-center"):
                        ui.button("Wczytaj definicje ponownie", on_click=lambda: st.reload()).props("flat dense")
                        ui.label(f"Kafelki: {st.registry.dir} (zmiany plików są wczytywane same)").classes(
                            "text-caption text-grey")
                launcher()

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
                    tree_mode = ui.switch("Drzewo")
                    ui.button("Zakończ (SIGTERM)", on_click=lambda: kill_selected(False)).props("dense color=warning")
                    ui.button("Zabij (SIGKILL)", on_click=lambda: kill_selected(True)).props("dense color=negative")
                    nice_in = ui.number("nice", value=10, min=-20, max=19, format="%d").props("dense").style("width:80px")
                    ui.button("Ustaw priorytet", on_click=lambda: renice_selected()).props("dense outline")
                ptable = ui.table(
                    columns=[
                        {"name": "pid", "label": "PID", "field": "pid", "sortable": True},
                        {"name": "name", "label": "Nazwa", "field": "name", "sortable": True, "align": "left",
                         "style": "white-space:pre"},
                        {"name": "user", "label": "Użytkownik", "field": "user", "sortable": True, "align": "left"},
                        {"name": "cpu", "label": "CPU %", "field": "cpu", "sortable": True},
                        {"name": "rss", "label": "RAM MiB", "field": "rss", "sortable": True},
                        {"name": "vram", "label": "VRAM MiB", "field": "vram", "sortable": True},
                        {"name": "nice", "label": "nice", "field": "nice", "sortable": True},
                        {"name": "cmd", "label": "Komenda", "field": "cmd", "align": "left",
                         "style": "max-width:520px;overflow:hidden;text-overflow:ellipsis"},
                    ], rows=[], row_key="pid", selection="single",
                    pagination={"rowsPerPage": 25, "sortBy": "cpu", "descending": True},
                ).classes("w-full").props("dense flat")
                ptable.bind_filter_from(pfilter, "value")

                def on_tree(e) -> None:
                    ptable.pagination = {"rowsPerPage": 50 if e.value else 25,
                                         "sortBy": None if e.value else "cpu", "descending": not e.value}
                tree_mode.on_value_change(on_tree)

            # ---------------- Docker ----------------
            with ui.tab_panel("Docker"):
                docker_err = ui.label().classes("text-negative")
                with ui.row().classes("items-center").style("gap:8px"):
                    ui.label("Kontener:").classes("text-caption")
                    for label, action in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart")):
                        ui.button(label, on_click=lambda a=action: docker_action(a)).props("dense")
                    ui.button("Logi", on_click=lambda: docker_logs()).props("dense flat")
                    ui.separator().props("vertical")
                    ui.label("Cały projekt compose:").classes("text-caption")
                    for label, action in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart")):
                        ui.button(label, on_click=lambda a=action: project_action(a)).props("dense outline")
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
                with ui.row().classes("items-center").style("gap:8px"):
                    ui.label("Jedna powłoka dla wszystkich kart; przeładowanie strony jej nie zamyka.").classes(
                        "text-caption text-grey")
                    ui.button("Nowa powłoka", on_click=lambda: new_shell()).props("dense flat")
                term = ui.xterm({"cursorBlink": True, "fontSize": 13, "scrollback": 5000}) \
                    .classes("w-full").style("height:70vh")

            # ---------------- Dziennik ----------------
            with ui.tab_panel("Dziennik"):
                with ui.row().classes("items-center").style("gap:12px"):
                    log_source = ui.select({"": "wszystkie", "gui": "gui", "cli": "cli", "api": "api",
                                            "mcp": "mcp", "arbiter": "arbiter", "tray": "tray"}, value="", label="Źródło") \
                        .props("dense").style("width:160px")
                    log_source.on_value_change(lambda: seen.update(actions=-1))
                    ui.label(f"Plik: {CONFIG_DIR / 'actions.jsonl'}").classes("text-caption text-grey")
                atable = ui.table(
                    columns=[
                        {"name": "time", "label": "Czas", "field": "time", "align": "left"},
                        {"name": "source", "label": "Źródło", "field": "source", "align": "left"},
                        {"name": "action", "label": "Akcja", "field": "action", "align": "left"},
                        {"name": "args", "label": "Argumenty", "field": "args", "align": "left",
                         "style": "max-width:360px;overflow:hidden;text-overflow:ellipsis"},
                        {"name": "ok", "label": "OK", "field": "ok"},
                        {"name": "result", "label": "Wynik", "field": "result", "align": "left",
                         "style": "max-width:520px;white-space:normal"},
                    ], rows=[], row_key="key", pagination={"rowsPerPage": 30}).classes("w-full").props("dense flat")
                atable.add_slot("body-cell-source", """
                    <q-td :props="props"><q-badge :color="props.row.color">{{ props.value }}</q-badge></q-td>""")

            # ---------------- Konfiguracja ----------------
            with ui.tab_panel("Konfiguracja"):
                with ui.row().classes("w-full no-wrap").style("gap:16px"):
                    with ui.column().classes("col"):
                        with ui.row().classes("items-center w-full").style("gap:8px"):
                            file_sel = ui.select(editor.list_files(), label="Plik", value="tiles/ai.yaml") \
                                .props("dense").style("min-width:260px")
                            new_name = ui.input("nowy plik, np. tiles/moje.yaml").props("dense").style("width:240px")
                            ui.button("Utwórz", on_click=lambda: create_file()).props("dense flat")
                            ui.button("Zapisz", on_click=lambda: save_file()).props("dense color=positive")
                            ui.button("Wczytaj z dysku", on_click=lambda: load_file()).props("dense flat")
                        cfg_status = ui.label("").classes("text-caption")
                        cm = ui.codemirror("", language="YAML", theme="oneDark").classes("w-full") \
                            .style("height:62vh")
                    with ui.column().style("width:360px"):
                        ui.label("Historia pliku (git)").classes("text-subtitle2")
                        hist_box = ui.column().classes("w-full").style("gap:2px")
                        ui.separator()
                        ui.label("Serwer MCP").classes("text-subtitle2")
                        mcp_info = ui.label("").classes("text-caption")
                        with ui.expansion("Wpisy dla klientów MCP").classes("w-full"):
                            for name, snippet in mcp_snippets().items():
                                ui.label(name).classes("text-caption q-mt-sm")
                                ui.code(snippet, language="yaml" if "Hermes" in name else "json").classes("w-full")
                        ui.separator()
                        ui.label("Wtyczki").classes("text-subtitle2")
                        for p in LOADED:
                            ui.label(f"{'✓' if p['ok'] else '✗'} {p['name']}").classes(
                                "text-caption " + ("text-positive" if p["ok"] else "text-negative"))
                            if p["error"]:
                                ui.label(p["error"]).classes("text-caption text-negative").style("white-space:pre-wrap")
                        if not LOADED:
                            ui.label(f"Brak. Katalog: {CONFIG_DIR / 'plugins'}").classes("text-caption text-grey")

            for name, build in TABS:
                with ui.tab_panel(name):
                    try:
                        build()
                    except Exception as e:
                        ui.label(f"Wtyczka: {e}").classes("text-negative")

        # ================= terminal =================
        def on_term_data(data: bytes) -> None:
            term.write(data)

        unsubscribe = {"fn": None}

        async def show_terminal() -> None:
            st.terminal.ensure()
            if unsubscribe["fn"] is None:
                unsubscribe["fn"] = st.terminal.subscribe(on_term_data)
            await asyncio.sleep(0.3)
            r = term.fit()
            if inspect.isawaitable(r):
                await r

        def new_shell() -> None:
            term.clear() if hasattr(term, "clear") else None
            st.terminal.restart()

        term.on_data(lambda e: st.terminal.write(e.data))
        term.on_resize(lambda e: st.terminal.resize(e.rows, e.cols))

        async def run_in_terminal(cmd: str) -> None:
            tabs.set_value("Terminal")
            await show_terminal()
            st.terminal.write(cmd + "\n")

        # ================= konfiguracja =================
        def load_file(rel: str | None = None) -> None:
            rel = rel or file_sel.value
            if not rel:
                return
            try:
                cm.value = editor.read(rel)
                cm.props(f'language="{"Python" if rel.endswith(".py") else "YAML"}"')
                cfg_status.set_text(f"{CONFIG_DIR / rel}")
                cfg_status.classes(replace="text-caption text-grey")
            except Exception as e:
                cfg_status.set_text(str(e))
            refresh_history(rel)

        def refresh_history(rel: str) -> None:
            hist_box.clear()
            with hist_box:
                rows = editor.history(rel)
                if not rows:
                    ui.label("brak wpisów").classes("text-caption text-grey")
                for h in rows:
                    with ui.row().classes("items-center no-wrap w-full").style("gap:6px"):
                        ui.label(f"{h['time']} {h['message']}").classes("text-caption col ellipsis")
                        ui.button("Pokaż", on_click=lambda h=h: show_version(rel, h["hash"])).props("dense flat size=sm")

        def show_version(rel: str, rev: str) -> None:
            try:
                cm.value = editor.version(rel, rev)
                cfg_status.set_text(f"Wersja {rev} w edytorze; Zapisz, aby ją przywrócić.")
                cfg_status.classes(replace="text-caption text-warning")
            except Exception as e:
                ui.notify(str(e), type="negative")

        async def save_file() -> None:
            rel = file_sel.value
            try:
                msg = await st.act("gui", "edit_file", {"path": rel}, lambda: _save(rel, cm.value))
                cfg_status.set_text(msg)
                cfg_status.classes(replace="text-caption text-positive")
                refresh_history(rel)
            except Exception as e:
                cfg_status.set_text(f"Nie zapisano: {e}")
                cfg_status.classes(replace="text-caption text-negative")

        async def _save(rel: str, text: str) -> str:
            msg = editor.save(rel, text, "gui")
            st.reload()
            return msg

        def create_file() -> None:
            rel = (new_name.value or "").strip()
            if not rel:
                return
            try:
                editor._safe(rel)
            except ValueError as e:
                ui.notify(str(e), type="negative")
                return
            file_sel.options = sorted(set(editor.list_files()) | {rel})
            file_sel.update()
            file_sel.value = rel
            cm.value = "" if rel.endswith(".py") else "id: nowy\ntitle: Nowy kafelek\ntype: command\ngroup: Inne\nstart: echo hej\n"

        file_sel.on_value_change(lambda e: load_file(e.value))

        def edit_tile_yaml(t: Tile) -> None:
            rel = editor.tile_file(t.source)
            tabs.set_value("Konfiguracja")
            file_sel.value = rel
            load_file(rel)

        # ================= okno kafelka =================
        def open_tile(t: Tile) -> None:
            values = t.defaults()
            controls: dict[str, ui.element] = {}
            seen_lines = {"n": st.runner.jobs[t.id].count if t.id in st.runner.jobs else 0}

            with ui.dialog() as dlg, ui.card().style("width:880px;max-width:95vw"):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label(t.title).classes("text-h6")
                    ui.label(f"{TYPE_LABEL[t.type]} · {t.source}").classes("text-caption text-grey")
                if t.help:
                    ui.label(t.help).classes("text-body2").style("white-space:pre-line")
                deps = []
                if t.on_start:
                    deps.append("przy starcie odsuwa: " + ", ".join(
                        h.yield_ + (f" (gdy {h.if_})" if h.if_ else "") + (f" (chyba że {h.unless})" if h.unless else "")
                        for h in t.on_start))
                if t.vram:
                    deps.append(f"priorytet VRAM {t.vram.priority}")
                if deps:
                    ui.label(" · ".join(deps)).classes("text-caption text-grey")

                preview = ui.code("", language="bash").classes("w-full").style("max-height:240px;overflow:auto")
                plan_box = ui.column().classes("w-full").style("gap:2px")

                def refresh_preview() -> None:
                    preview.set_content(st.preview(t, values) or "(brak komendy)")
                    plan_box.clear()
                    if t.vram or t.on_start:
                        try:
                            plan = st.plan(t.id, None, values).to_dict()
                        except Exception as e:
                            with plan_box:
                                ui.label(f"Plan: {e}").classes("text-negative text-caption")
                            return
                        with plan_box:
                            with ui.expansion("Plan arbitra VRAM", value=not plan["trivial"]).classes("w-full"):
                                plan_view(plan)

                def set_value(opt_id: str, v) -> None:
                    values[opt_id] = v
                    refresh_preview()

                def apply_preset(name: str | None) -> None:
                    if not name:
                        return
                    merged = {**t.defaults(), **t.presets.get(name, {})}
                    for k, v in merged.items():
                        if k in controls:
                            controls[k].value = v
                    values.update(merged)
                    refresh_preview()

                if t.presets:
                    ui.select(list(t.presets), label="Preset", value=None,
                              on_change=lambda e: apply_preset(e.value)).props("dense").style("width:300px")

                def option_row(o) -> None:
                    with ui.row().classes("items-center w-full no-wrap").style("gap:16px"):
                        label = o.label or o.id
                        if o.type == "bool":
                            c = ui.switch(label, value=bool(o.default))
                        elif o.type == "int":
                            lo, hi = (o.range or [None, None])[:2]
                            c = ui.number(label, value=o.default, min=lo, max=hi, format="%d").props("dense")
                        elif o.type == "enum":
                            opts = dict(o.values or {})
                            c = ui.select(opts, label=label, value=o.default,
                                          new_value_mode="add-unique" if o.custom else None).props("dense")
                        else:
                            c = ui.input(label, value=o.default or "").props("dense")
                        c.style("min-width:240px")
                        c.on_value_change(lambda e, o=o: set_value(o.id, e.value))
                        controls[o.id] = c
                        hint = f"{o.flag}  " if o.flag else ""
                        ui.label(hint + o.help).classes("text-caption text-grey col")

                for o in [o for o in t.options if not o.advanced]:
                    option_row(o)
                adv = [o for o in t.options if o.advanced]
                if adv:
                    with ui.expansion(f"Zaawansowane ({len(adv)})").classes("w-full"):
                        for o in adv:
                            option_row(o)

                with ui.row().style("gap:8px"):
                    ui.button("Start", on_click=lambda: do_start(t, dict(values))).props("color=positive")
                    if t.type != "command":
                        ui.button("Stop", on_click=lambda: do_stop(t)).props("color=negative outline")
                    if t.start:
                        async def to_terminal() -> None:
                            dlg.close()
                            body = st.preview(t, values)
                            await run_in_terminal(f"( {t.shell_init}\n{body}\n)" if t.shell_init else body)
                        ui.button("W terminalu", on_click=to_terminal).props("outline")
                    ui.button("Edytuj YAML", on_click=lambda: (dlg.close(), edit_tile_yaml(t))).props("flat")
                    if t.url:
                        ui.link("Otwórz", t.url, new_tab=True).classes("self-center")
                    ui.button("Zamknij", on_click=dlg.close).props("flat")

                log = ui.log(max_lines=300).classes("w-full").style("height:180px")

                def pump() -> None:
                    job = st.runner.jobs.get(t.id)
                    if not job:
                        return
                    if job.count < seen_lines["n"]:
                        seen_lines["n"] = 0
                    new = job.count - seen_lines["n"]
                    if new > 0:
                        for line in list(job.lines)[-new:]:
                            log.push(line)
                        seen_lines["n"] = job.count

                timer = ui.timer(0.5, pump)
                dlg.on("hide", timer.cancel)
                refresh_preview()
            dlg.open()

        # ================= procesy i docker =================
        async def kill_selected(force: bool) -> None:
            if not ptable.selected:
                ui.notify("Zaznacz proces w tabeli")
                return
            row = ptable.selected[0]
            kind, owner = st.resolve_owner(row["pid"])
            text = f"{'Zabić' if force else 'Zakończyć'} {row['name'].strip(' └ ')} (PID {row['pid']})?"
            if kind == "kafelek":
                text += f"\nTen proces należy do kafelka „{owner}”. Lepiej użyć jego przycisku Stop, który sprząta po usłudze."
            elif kind == "kontener":
                text += f"\nTen proces działa w kontenerze „{owner}”. Lepiej zatrzymać kontener."
            if await confirm(text):
                try:
                    ui.notify(await st.kill(row["pid"], force, "gui"))
                    ptable.selected.clear()
                except Exception as e:
                    ui.notify(str(e), type="negative")

        async def renice_selected() -> None:
            if not ptable.selected:
                ui.notify("Zaznacz proces w tabeli")
                return
            pid, nice = ptable.selected[0]["pid"], int(nice_in.value or 0)

            async def run():
                return st.monitor.renice(pid, nice)
            try:
                ui.notify(await st.act("gui", "renice", {"pid": pid, "nice": nice}, run))
            except Exception as e:
                ui.notify(f"{e} (obniżenie nice poniżej obecnego wymaga uprawnień roota)", type="negative")

        async def docker_action(action: str) -> None:
            if not dtable.selected:
                ui.notify("Zaznacz kontener w tabeli")
                return
            name = dtable.selected[0]["name"]
            try:
                ui.notify(await st.container_action(name, action, "gui"))
            except Exception as e:
                ui.notify(str(e), type="negative")

        async def project_action(action: str) -> None:
            if not dtable.selected or not dtable.selected[0]["project"]:
                ui.notify("Zaznacz kontener należący do projektu compose")
                return
            project = dtable.selected[0]["project"]
            n = len(st.docker.project(project))
            if action != "start" and not await confirm(f"{action} całego projektu {project} ({n} kontenerów)?"):
                return
            try:
                ui.notify(await st.project_action(project, action, "gui"))
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

        # ================= odświeżanie =================
        def refresh() -> None:
            if st.registry.version != seen["version"]:
                seen["version"] = st.registry.version
                launcher.refresh()
                header_profiles.refresh()
                file_sel.options = editor.list_files()
                file_sel.update()
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

            active_tab = tabs.value
            if active_tab == "Zasoby":
                owners_table.rows[:] = [{**o, "pids": ", ".join(map(str, o["pids"]))} for o in owners]
                owners_table.update()
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

            if active_tab == "Launcher":
                for tid, c in cards.items():
                    t = st.registry.tiles.get(tid)
                    if not t:
                        continue
                    info = st.tile_info(t)
                    color, text = STATUS[info["status"]]
                    c["badge"].set_text(text)
                    c["badge"].props(f"color={color}")
                    c["vram"].set_text(f"VRAM {info['vram_mib']} MiB" if info["vram_mib"] else "")
                    parts = []
                    m = info["measured"]
                    if info["measuring"]:
                        parts.append("mierzę VRAM…")
                    elif m and m.get("budget") is not None:
                        parts.append(f"zmierzone {m['mib']} MiB ({m['diff']:+d} vs budżet)")
                    elif m:
                        parts.append(f"zmierzone {m['mib']} MiB")
                    if info["yielded"]:
                        parts.append("odsunięte: " + ", ".join(info["yielded"]))
                    c["extra"].set_text(" · ".join(parts))

            if active_tab == "Procesy":
                rows = [p for p in st.monitor.processes if p["vram"] or not vram_only.value]
                if tree_mode.value:
                    rows = tree_order(rows)
                else:
                    rows.sort(key=lambda p: (-p["vram"], -p["cpu"], -p["rss"]))
                ptable.rows[:] = rows[:600]
                ptable.update()

            if active_tab == "Docker":
                docker_err.set_text(st.docker.error or "")
                dtable.rows[:] = [{k: c[k] for k in ("project", "name", "state", "status", "image")}
                                  for c in st.docker.containers]
                dtable.update()

            if active_tab == "Dziennik" and st.actions.count != seen["actions"]:
                seen["actions"] = st.actions.count
                atable.rows[:] = [{
                    "key": f"{a['ts']}-{i}",
                    "time": time.strftime("%m-%d %H:%M:%S", time.localtime(a["ts"])),
                    "source": a["source"], "color": SOURCE_COLOR.get(a["source"], "grey"),
                    "action": a["action"], "ok": "✓" if a["ok"] else "✗",
                    "args": ", ".join(f"{k}={v}" for k, v in a["args"].items() if v not in (None, {}, "")),
                    "result": a["result"] if isinstance(a["result"], str) else str(a["result"]),
                } for i, a in enumerate(st.actions.recent(300, log_source.value or None))]
                atable.update()

            if active_tab == "Konfiguracja":
                mcp_info.set_text(
                    f"http://127.0.0.1:8770/mcp · poziom {st.config.mcp_level} · narzędzia: "
                    + (", ".join(mcp.registered) if mcp else "wyłączony")
                    + " · poziom zmienisz w config.yaml (mcp_level)")

        async def on_tab(e) -> None:
            seen["actions"] = -1
            if e.value == "Terminal":
                await show_terminal()
            refresh()

        tabs.on_value_change(on_tab)

        client = ui.context.client

        def cleanup() -> None:
            if unsubscribe["fn"]:
                unsubscribe["fn"]()
        (getattr(client, "on_delete", None) or client.on_disconnect)(cleanup)

        ui.timer(2.0, refresh)
        ui.timer(0.3, refresh, once=True)
        ui.timer(0.5, lambda: load_file("tiles/ai.yaml"), once=True)

    @ui.page("/kiosk")
    def kiosk() -> None:
        """Widok na drugi monitor: duży pasek VRAM, kafelki usług, bez kart i przycisków."""
        ui.dark_mode().enable()
        ui.query("body").style("background:#111")
        title = ui.label().classes("text-h4 q-mb-md")
        bar = ui.element("div").style("display:flex;height:48px;width:100%;border-radius:8px;overflow:hidden;"
                                      "background:#333")
        legend = ui.row().classes("q-mt-sm").style("gap:18px")
        grid = ui.row().classes("q-mt-lg").style("gap:16px")

        def tick() -> None:
            snap = st.monitor.snapshot
            gpus = snap.get("gpus") or []
            if not gpus:
                return
            g = gpus[0]
            title.set_text(f"VRAM {gb(g['mem_used'])} / {gb(g['mem_total'])} GB · CPU {snap['cpu']['total']:.0f}% · "
                           f"RAM {gb(snap['mem']['used'])} GB · {g['temp']}°C")
            bar.clear()
            legend.clear()
            for i, o in enumerate(st.owners()):
                with bar:
                    ui.element("div").style(f"width:{100 * o['mib'] / g['mem_total']:.2f}%;"
                                            f"background:{COLORS[i % len(COLORS)]}")
                with legend:
                    ui.label(f"■ {o['owner']} {o['mib']} MiB").style(f"color:{COLORS[i % len(COLORS)]}")
            grid.clear()
            with grid:
                for t in st.registry.tiles.values():
                    if t.type in ("app", "command"):
                        continue
                    status = st.status(t)
                    color, text = STATUS[status]
                    with ui.card().style("width:220px;background:#1d1d1d"):
                        ui.label(t.title).classes("text-subtitle1")
                        ui.badge(text, color=color)
                        used = st.vram_of(t)
                        if used:
                            ui.label(f"{used} MiB").classes("text-caption")

        ui.timer(2.0, tick)
        ui.timer(0.2, tick, once=True)
