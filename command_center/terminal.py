"""Jedna trwała powłoka bash na pty, współdzielona przez wszystkie karty przeglądarki.

Przeładowanie strony nie zabija powłoki: nowy widok dostaje bufor ostatniego wyjścia.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
from typing import Callable

from .tiles import session_env

BUFFER = 256 * 1024


class Terminal:
    def __init__(self) -> None:
        self.fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self.buffer = bytearray()
        self.subscribers: set[Callable[[bytes], None]] = set()
        self.size = (24, 80)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.fd is not None

    def ensure(self) -> None:
        if self.alive:
            return
        master, slave = pty.openpty()
        env = session_env()
        env["TERM"] = "xterm-256color"
        self.proc = subprocess.Popen(
            ["bash", "-i"], stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, env=env, cwd=os.path.expanduser("~"),
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        self.fd = master
        self.resize(*self.size)
        asyncio.get_running_loop().add_reader(master, self._on_read)

    def _on_read(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.fd)
            os.close(self.fd)
            self.fd = None
            data = b"\r\n[powloka zakonczona; kliknij Nowa powloka]\r\n"
        self.buffer += data
        if len(self.buffer) > BUFFER:
            del self.buffer[:len(self.buffer) - BUFFER]
        for cb in list(self.subscribers):
            try:
                cb(data)
            except Exception:
                self.subscribers.discard(cb)

    def subscribe(self, cb: Callable[[bytes], None]) -> Callable[[], None]:
        if self.buffer:
            cb(bytes(self.buffer))
        self.subscribers.add(cb)
        return lambda: self.subscribers.discard(cb)

    def write(self, text: str) -> None:
        if self.fd is not None:
            os.write(self.fd, text.encode())

    def resize(self, rows: int, cols: int) -> None:
        self.size = (rows, cols)
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def restart(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        if self.fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self.fd)
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self.buffer.clear()
        self.ensure()

