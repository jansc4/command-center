"""cctl: cienki klient API demona.

  cctl status                          zasoby, właściciele VRAM, aktywny profil
  cctl tiles                           lista kafelków ze stanem
  cctl show ID [-p PRESET] [k=v ...]   podgląd komendy
  cctl plan ID [-p PRESET] [k=v ...]   plan arbitra VRAM (bez wykonania)
  cctl start ID [-p PRESET] [k=v] [-y] start; gdy plan odsuwa inne usługi, pyta (albo -y)
  cctl stop ID                         stop (+ przywrócenie odsuniętych)
  cctl log ID                          wyjście ostatniego uruchomienia
  cctl profiles                        lista profili
  cctl profile ID [-y]                 przełączenie profilu (plan + potwierdzenie)
  cctl ps [--vram] [-q TEKST]          procesy
  cctl kill PID [--force]
  cctl renice PID NICE
  cctl actions [-n N] [-s ŹRÓDŁO]      dziennik akcji
  cctl reload                          przeładuj definicje
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("CC_URL", "http://127.0.0.1:8770")


def call(path: str, data: dict | None = None, post: bool = False):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data or {}).encode() if post else None,
        headers={"content-type": "application/json", "x-cc-source": "cli"},
        method="POST" if post else "GET")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            body = json.loads(body).get("detail", body)
        except ValueError:
            pass
        sys.exit(f"Błąd {e.code}: {body}")
    except urllib.error.URLError as e:
        sys.exit(f"Demon nie odpowiada pod {BASE} ({e.reason}). Sprawdź: systemctl --user status command-center")


def parse_values(pairs: list[str]) -> dict:
    out = {}
    for p in pairs:
        k, _, v = p.partition("=")
        out[k] = {"true": True, "false": False}.get(v.lower(), v)
    return out


def ask(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{prompt} [t/N] ").strip().lower() in ("t", "tak", "y", "yes")


def main() -> None:
    ap = argparse.ArgumentParser(prog="cctl", description="Klient Command Center",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("tiles")
    for name in ("show", "plan", "start"):
        sp = sub.add_parser(name)
        sp.add_argument("id")
        sp.add_argument("-p", "--preset")
        sp.add_argument("values", nargs="*", help="opcja=wartość")
        if name == "start":
            sp.add_argument("-y", "--yes", action="store_true", help="wykonaj plan bez pytania")
    sub.add_parser("stop").add_argument("id")
    sub.add_parser("log").add_argument("id")
    sub.add_parser("profiles")
    pp = sub.add_parser("profile")
    pp.add_argument("id")
    pp.add_argument("-y", "--yes", action="store_true")
    ps = sub.add_parser("ps")
    ps.add_argument("--vram", action="store_true")
    ps.add_argument("-q", "--query", default="")
    kp = sub.add_parser("kill")
    kp.add_argument("pid", type=int)
    kp.add_argument("--force", action="store_true")
    rp = sub.add_parser("renice")
    rp.add_argument("pid", type=int)
    rp.add_argument("nice", type=int)
    lp = sub.add_parser("actions")
    lp.add_argument("-n", type=int, default=20)
    lp.add_argument("-s", "--source")
    sub.add_parser("reload")
    a = ap.parse_args()

    body = lambda: {"preset": a.preset, "values": parse_values(a.values)}  # noqa: E731

    if a.cmd == "status":
        s = call("/api/status")
        print(f"CPU {s['cpu']['total']:.0f}%   RAM {s['mem']['used']}/{s['mem']['total']} MiB   "
              f"swap {s['swap']['used']}/{s['swap']['total']} MiB   profil: {s.get('profile') or '-'}")
        for g in s["gpus"]:
            print(f"{g['name']}: VRAM {g['mem_used']}/{g['mem_total']} MiB, {g['util']}%, {g['temp']}°C")
        for o in s["owners"]:
            print(f"  {o['mib']:>6} MiB  {o['owner']} ({o['kind']})")
    elif a.cmd == "tiles":
        for t in call("/api/tiles"):
            vram = f"  VRAM {t['vram_mib']} MiB" if t["vram_mib"] else ""
            m = t.get("measured")
            meas = f"  (zmierzone {m['mib']})" if m else ""
            ys = f"  odsunął: {', '.join(t['yielded'])}" if t.get("yielded") else ""
            print(f"{t['id']:<16} {t['status']:<10} {t['title']}{vram}{meas}{ys}")
    elif a.cmd == "show":
        print(call(f"/api/tiles/{a.id}/preview", body(), post=True)["command"])
    elif a.cmd == "plan":
        print(call(f"/api/tiles/{a.id}/plan", body(), post=True)["text"])
    elif a.cmd == "start":
        res = call(f"/api/tiles/{a.id}/start?confirm={'true' if a.yes else 'false'}", body(), post=True)
        if res.get("needs_confirm"):
            print(res["plan"]["text"])
            if not ask("Wykonać?"):
                sys.exit("Anulowano (użyj -y, aby wykonać bez pytania).")
            res = call(f"/api/tiles/{a.id}/start?confirm=true", body(), post=True)
        print("\n".join(res.get("messages", [])))
    elif a.cmd == "stop":
        print(call(f"/api/tiles/{a.id}/stop", post=True)["message"])
    elif a.cmd == "log":
        print("\n".join(call(f"/api/tiles/{a.id}/log")["lines"]))
    elif a.cmd == "profiles":
        r = call("/api/profiles")
        for p in r["profiles"]:
            mark = "*" if p["id"] == r["active"] else " "
            print(f"{mark} {p['id']:<12} {p['title']:<26} {p['help']}")
    elif a.cmd == "profile":
        plan = call(f"/api/profiles/{a.id}/plan", post=True)
        print(plan["text"])
        if not a.yes and not ask("Przełączyć?"):
            sys.exit("Anulowano.")
        res = call(f"/api/profiles/{a.id}/apply?confirm=true", post=True)
        print("\n".join(res.get("messages", [])))
    elif a.cmd == "ps":
        q = urllib.parse.quote(a.query)
        for p in call(f"/api/processes?limit=30&q={q}&vram_only={'true' if a.vram else 'false'}"):
            print(f"{p['pid']:>7} {p['cpu']:>5}% {p['rss']:>6}M {p['vram']:>6}V  {p['name']:<20} {p['cmd'][:70]}")
    elif a.cmd == "kill":
        print(call(f"/api/processes/{a.pid}/kill?force={'true' if a.force else 'false'}", post=True)["message"])
    elif a.cmd == "renice":
        print(call(f"/api/processes/{a.pid}/renice?nice={a.nice}", post=True)["message"])
    elif a.cmd == "actions":
        src = f"&source={a.source}" if a.source else ""
        for e in reversed(call(f"/api/actions?limit={a.n}{src}")):
            ts = time.strftime("%m-%d %H:%M:%S", time.localtime(e["ts"]))
            res = e["result"] if isinstance(e["result"], str) else json.dumps(e["result"], ensure_ascii=False)
            print(f"{ts} {e['source']:<7} {'✓' if e['ok'] else '✗'} {e['action']:<16} {json.dumps(e['args'], ensure_ascii=False)[:60]}  {res[:100]}")
    elif a.cmd == "reload":
        r = call("/api/reload", post=True)
        print(f"Kafelki: {r['tiles']}, profile: {r['profiles']}")
        for e in r["errors"]:
            print("  błąd:", e)


if __name__ == "__main__":
    main()
