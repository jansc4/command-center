"""Punkt wejścia: uv run command-center [--port 8770] [--show] [--native]"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from nicegui import app, ui

from . import api
from . import ui as gui
from .mcp_server import CCMcp
from .plugins import load_plugins
from .state import State


class TokenGate:
    """Dostęp spoza 127.0.0.1 tylko z tokenem (nagłówek Bearer, ?token= albo ciasteczko cc_token)."""

    def __init__(self, inner, token: str) -> None:
        self.inner, self.token = inner, token

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or self._local(scope):
            return await self.inner(scope, receive, send)
        how = self._ok(scope)
        if how == "query" and scope["type"] == "http":
            async def send_cookie(msg):
                if msg["type"] == "http.response.start":
                    cookie = f"cc_token={self.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
                    msg = {**msg, "headers": [*msg.get("headers", []), (b"set-cookie", cookie.encode())]}
                await send(msg)
            return await self.inner(scope, receive, send_cookie)
        if how:
            return await self.inner(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": "Brak tokenu Command Center".encode()})

    @staticmethod
    def _local(scope) -> bool:
        host = (scope.get("client") or ("", 0))[0]
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _ok(self, scope) -> str:
        """Zwraca, skąd przyszedł poprawny token: header / query / cookie; pusty napis = brak."""
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        c = SimpleCookie(headers.get("cookie", ""))
        candidates = {
            "header": headers.get("authorization", "").removeprefix("Bearer ").strip(),
            "query": (parse_qs(scope.get("query_string", b"").decode()).get("token") or [""])[0],
            "cookie": c["cc_token"].value if "cc_token" in c else "",
        }
        for how, given in candidates.items():
            if given and self.token and hmac.compare_digest(given, self.token):
                return how
        return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Command Center")
    ap.add_argument("--host", default=None, help="adres nasłuchu (domyślnie 127.0.0.1; patrz remote w config.yaml)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--show", action="store_true", help="otwórz w przeglądarce po starcie")
    ap.add_argument("--native", action="store_true", help="okno natywne (wymaga pywebview)")
    args = ap.parse_args()

    st = State()
    api.register(st)
    mcp = CCMcp(st)
    mcp.mount(app)
    load_plugins(st, mcp, app)
    gui.register(st, mcp)
    app.on_startup(st.run_loop)

    host = args.host or "127.0.0.1"
    remote = st.config.remote
    if remote.enabled:
        if not remote.token or len(remote.token) < 16:
            print("[remote] włączony, ale token jest pusty lub krótszy niż 16 znaków; zostaję na 127.0.0.1")
        else:
            host = args.host or remote.host
            app.add_middleware(TokenGate, token=remote.token)
            print(f"[remote] nasłuch na {host}, wymagany token")

    ui.run(host=host, port=args.port, title="Command Center", dark=True, favicon="🎛",
           reload=False, show=args.show, native=args.native, show_welcome_message=False)


if __name__ in {"__main__", "__mp_main__"}:
    main()
