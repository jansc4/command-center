"""cctl: cienki klient API demona.

  cctl status                      zasoby i właściciele VRAM
  cctl tiles                       lista kafelków ze stanem
  cctl show ID [k=v ...]           podgląd komendy
  cctl start ID [-p PRESET] [k=v]  start kafelka
  cctl stop ID                     stop kafelka
  cctl log ID                      wyjście ostatniego uruchomienia
  cctl ps [--vram]                 procesy
  cctl kill PID [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("CC_URL", "http://127.0.0.1:8770")


def call(path: str, data: dict | None = None, post: bool = False):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data or {}).encode() if post else None,
        headers={"content-type": "application/json"},
        method="POST" if post else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"Błąd {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"Demon nie odpowiada pod {BASE} ({e.reason}). Sprawdź: systemctl --user status command-center")


def parse_values(pairs: list[str]) -> dict:
    out = {}
    for p in pairs:
        k, _, v = p.partition("=")
        out[k] = {"true": True, "false": False}.get(v.lower(), v)
    return out


def tile_values(tile_id: str, preset: str | None, pairs: list[str]) -> dict:
    values = {}
    if preset:
        tile = next((t for t in call("/api/tiles") if t["id"] == tile_id), None)
        if not tile or preset not in tile["presets"]:
            sys.exit(f"Nie ma presetu {preset!r}. Dostępne: {', '.join(tile['presets']) if tile else '-'}")
        values.update(tile["presets"][preset])
    values.update(parse_values(pairs))
    return values


def main() -> None:
    ap = argparse.ArgumentParser(prog="cctl", description="Klient Command Center")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("tiles")
    for name in ("show", "start"):
        sp = sub.add_parser(name)
        sp.add_argument("id")
        sp.add_argument("-p", "--preset")
        sp.add_argument("values", nargs="*", help="opcja=wartość")
    sub.add_parser("stop").add_argument("id")
    sub.add_parser("log").add_argument("id")
    sub.add_parser("ps").add_argument("--vram", action="store_true")
    kp = sub.add_parser("kill")
    kp.add_argument("pid", type=int)
    kp.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if a.cmd == "status":
        s = call("/api/status")
        print(f"CPU {s['cpu']['total']:.0f}%   RAM {s['mem']['used']}/{s['mem']['total']} MiB   "
              f"swap {s['swap']['used']}/{s['swap']['total']} MiB")
        for g in s["gpus"]:
            print(f"{g['name']}: VRAM {g['mem_used']}/{g['mem_total']} MiB, {g['util']}%, {g['temp']}°C")
        for o in s["owners"]:
            print(f"  {o['mib']:>6} MiB  {o['owner']} ({o['kind']})")
    elif a.cmd == "tiles":
        for t in call("/api/tiles"):
            vram = f"  VRAM {t['vram_mib']} MiB" if t["vram_mib"] else ""
            print(f"{t['id']:<16} {t['status']:<9} {t['title']}{vram}")
    elif a.cmd == "show":
        print(call(f"/api/tiles/{a.id}/preview", tile_values(a.id, a.preset, a.values), post=True)["command"])
    elif a.cmd == "start":
        print(call(f"/api/tiles/{a.id}/start", tile_values(a.id, a.preset, a.values), post=True)["message"])
    elif a.cmd == "stop":
        print(call(f"/api/tiles/{a.id}/stop", post=True)["message"])
    elif a.cmd == "log":
        print("\n".join(call(f"/api/tiles/{a.id}/log")["lines"]))
    elif a.cmd == "ps":
        for p in call(f"/api/processes?limit=30&vram_only={'true' if a.vram else 'false'}"):
            print(f"{p['pid']:>7} {p['cpu']:>5}% {p['rss']:>6}M {p['vram']:>6}V  {p['name']:<20} {p['cmd'][:70]}")
    elif a.cmd == "kill":
        print(call(f"/api/processes/{a.pid}/kill?force={'true' if a.force else 'false'}", post=True)["message"])


if __name__ == "__main__":
    main()
