#!/usr/bin/env python3
"""Ikona Command Center w zasobniku KDE.

Samodzielny skrypt na systemowym Pythonie z PyQt6 (bez zależności od venv panelu); rozmawia z demonem
przez REST API. Ikona pokazuje zajętość VRAM, menu: otwórz panel, profile, start/stop usług, kiosk.

  /usr/bin/python3 ~/projects/command_center/command_center/tray.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("CC_URL", "http://127.0.0.1:8770")


def fix_env() -> None:
    """Usługa systemd może nie mieć zmiennych sesji graficznej; odtwórz je z gniazd."""
    run = Path(os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if not os.environ.get("WAYLAND_DISPLAY"):
        socks = sorted(f.name for f in run.glob("wayland-*") if not f.name.endswith(".lock"))
        if socks:
            os.environ["WAYLAND_DISPLAY"] = socks[0]
    if not os.environ.get("DISPLAY"):
        xs = sorted(f.name[1:] for f in Path("/tmp/.X11-unix").glob("X*") if f.name[1:].isdigit())
        if xs:
            os.environ["DISPLAY"] = f":{xs[0]}"
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS", "disabled:").startswith("disabled:") and (run / "bus").exists():
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={run / 'bus'}"


fix_env()

from PyQt6.QtCore import QObject, QRectF, Qt, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon  # noqa: E402


def call(path: str, data: dict | None = None, post: bool = False, timeout: float = 300):
    req = urllib.request.Request(BASE + path, data=json.dumps(data or {}).encode() if post else None,
                                 headers={"content-type": "application/json", "x-cc-source": "tray"},
                                 method="POST" if post else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def open_app(url: str) -> None:
    for browser in ("chromium", "chromium-browser", "google-chrome", "brave-browser"):
        if shutil.which(browser):
            subprocess.Popen([browser, f"--app={url}"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    subprocess.Popen(["xdg-open", url], start_new_session=True)


class Bridge(QObject):
    done = pyqtSignal(object, object)   # (callback, wynik albo wyjątek)


class Tray:
    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.bridge = Bridge()
        self.bridge.done.connect(lambda cb, res: cb(res))
        self.tray = QSystemTrayIcon(self.icon(None))
        self.tray.setToolTip("Command Center")
        self.menu = QMenu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_activated)
        self.status: dict | None = None
        self.tiles: list[dict] = []
        self.profiles: dict = {"profiles": [], "active": None}
        self.build_menu()
        self.tray.show()
        self.timer = QTimer()
        self.timer.timeout.connect(self.poll)
        self.timer.start(3000)
        self.poll()

    # ---------- wątki ----------
    def bg(self, fn, cb) -> None:
        def run() -> None:
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                res = e
            self.bridge.done.emit(cb, res)
        threading.Thread(target=run, daemon=True).start()

    # ---------- ikona ----------
    @staticmethod
    def icon(frac: float | None) -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#2b2b2b"))
        p.drawRoundedRect(QRectF(4, 4, 56, 56), 10, 10)
        if frac is None:
            p.setBrush(QColor("#777"))
            p.drawRoundedRect(QRectF(14, 14, 36, 36), 6, 6)
        else:
            color = "#67c23a" if frac < 0.7 else "#e6a23c" if frac < 0.9 else "#f56c6c"
            h = 48 * max(min(frac, 1), 0.04)
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(8, 56 - h, 48, h), 6, 6)
        p.end()
        return QIcon(pm)

    def poll(self) -> None:
        def fetch():
            return call("/api/status", timeout=3), call("/api/tiles", timeout=3), call("/api/profiles", timeout=3)
        self.bg(fetch, self.on_poll)

    def on_poll(self, res) -> None:
        if isinstance(res, Exception):
            self.status = None
            self.tray.setIcon(self.icon(None))
            self.tray.setToolTip("Command Center: demon nie odpowiada\nsystemctl --user status command-center")
            return
        self.status, self.tiles, self.profiles = res
        s = self.status
        g = (s.get("gpus") or [None])[0]
        frac = g["mem_used"] / g["mem_total"] if g else None
        self.tray.setIcon(self.icon(frac))
        lines = [f"CPU {s['cpu']['total']:.0f}%   RAM {s['mem']['used'] / 1024:.1f}/{s['mem']['total'] / 1024:.1f} GB"]
        if g:
            lines.append(f"VRAM {g['mem_used'] / 1024:.1f}/{g['mem_total'] / 1024:.1f} GB, {g['temp']}°C")
        for o in s.get("owners", [])[:4]:
            lines.append(f"  {o['owner']}: {o['mib']} MiB")
        active = next((p["title"] for p in self.profiles["profiles"] if p["id"] == self.profiles["active"]), "-")
        lines.append(f"Profil: {active}")
        self.tray.setToolTip("\n".join(lines))

    # ---------- menu ----------
    def build_menu(self) -> None:
        self.menu.aboutToShow.connect(self.fill_menu)

    def fill_menu(self) -> None:
        m = self.menu
        m.clear()
        m.addAction("Otwórz panel", lambda: open_app(BASE))
        if self.status is None:
            a = m.addAction("Demon nie odpowiada")
            a.setEnabled(False)
            m.addSeparator()
            m.addAction("Zamknij ikonę", self.app.quit)
            return
        pm = m.addMenu("Profil")
        group = QActionGroup(pm)
        for p in self.profiles["profiles"]:
            act = QAction(p["title"], pm, checkable=True)
            act.setChecked(p["id"] == self.profiles["active"])
            act.setToolTip(p["help"])
            act.triggered.connect(lambda _=False, p=p: self.apply_profile(p))
            group.addAction(act)
            pm.addAction(act)
        sm = m.addMenu("Usługi")
        for t in self.tiles:
            if t["type"] not in ("service", "container", "stack"):
                continue
            running = t["status"] in ("running", "starting", "partial", "unhealthy")
            label = f"{'■ Stop' if running else '▶ Start'}  {t['title']}"
            if t.get("vram_mib"):
                label += f"  ({t['vram_mib']} MiB)"
            sm.addAction(label, (lambda t=t: self.stop(t)) if running else (lambda t=t: self.start(t)))
        am = m.addMenu("Aplikacje")
        for t in self.tiles:
            if t["type"] == "app":
                am.addAction(t["title"], lambda t=t: self.start(t))
        m.addSeparator()
        m.addAction("Widok kiosk (drugi monitor)", lambda: open_app(BASE + "/kiosk"))
        m.addAction("Zamknij ikonę", self.app.quit)

    def on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            open_app(BASE)

    # ---------- akcje ----------
    def ask(self, title: str, text: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, title, text)
        box.addButton("Wykonaj", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Anuluj", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is not cancel

    def report(self, title: str, res) -> None:
        if isinstance(res, urllib.error.HTTPError):
            try:
                detail = json.loads(res.read().decode()).get("detail")
            except Exception:  # noqa: BLE001
                detail = str(res)
            self.tray.showMessage(title, str(detail), QSystemTrayIcon.MessageIcon.Warning)
        elif isinstance(res, Exception):
            self.tray.showMessage(title, str(res), QSystemTrayIcon.MessageIcon.Warning)
        else:
            msgs = res.get("messages") or [res.get("message", "gotowe")]
            self.tray.showMessage(title, "\n".join(msgs)[:400])
        self.poll()

    def apply_profile(self, p: dict) -> None:
        def got_plan(plan) -> None:
            if isinstance(plan, Exception):
                return self.report(p["title"], plan)
            if self.ask(f"Profil: {p['title']}", plan["text"]):
                self.bg(lambda: call(f"/api/profiles/{p['id']}/apply?confirm=true", post=True),
                        lambda r: self.report(f"Profil: {p['title']}", r))
        self.bg(lambda: call(f"/api/profiles/{p['id']}/plan", post=True), got_plan)

    def start(self, t: dict) -> None:
        def first(res) -> None:
            if isinstance(res, Exception) or not res.get("needs_confirm"):
                return self.report(t["title"], res)
            if self.ask(f"Start: {t['title']}", res["plan"]["text"]):
                self.bg(lambda: call(f"/api/tiles/{t['id']}/start?confirm=true", post=True),
                        lambda r: self.report(t["title"], r))
        self.bg(lambda: call(f"/api/tiles/{t['id']}/start", post=True), first)

    def stop(self, t: dict) -> None:
        self.bg(lambda: call(f"/api/tiles/{t['id']}/stop", post=True), lambda r: self.report(t["title"], r))


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Command Center")
    app.setDesktopFileName("command-center")
    if not QSystemTrayIcon.isSystemTrayAvailable():
        sys.exit("Zasobnik systemowy niedostępny")
    tray = Tray(app)  # noqa: F841  (referencja trzyma ikonę przy życiu)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
