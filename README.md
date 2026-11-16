# Command Center

Panel do zarządzania zasobami komputera i lokalnymi usługami: http://127.0.0.1:8770
Projekt: dokument „Command Center — projekt aplikacji”. Stan i dalsze kroki: `HANDOFF.md`.

## Uruchomienie

Dwie usługi użytkownika startują z sesją graficzną:

```bash
systemctl --user restart command-center          # demon (po zmianie kodu)
journalctl --user -u command-center -f           # log demona
systemctl --user restart command-center-tray     # ikona w zasobniku
systemctl --user disable --now command-center-tray   # bez ikony
```

Jednostki: `systemd/*.service`, zainstalowane w `~/.config/systemd/user/`. `KillMode=process` sprawia,
że restart panelu nie zabija usług uruchomionych z kafelków. Ręcznie: `uv run command-center --show`.
Okno „jak aplikacja”: `chromium --app=http://127.0.0.1:8770` (to samo robi „Otwórz panel” w zasobniku).
Widok na drugi monitor: http://127.0.0.1:8770/kiosk.

## Co jest

- **Pasek górny**: CPU, RAM, VRAM podzielony na właścicieli; przełącznik profili.
- **Launcher**: kafelki z `~/.config/command-center/tiles/*.yaml`; opcje (zaawansowane zwinięte), presety, help,
  podgląd komendy, plan arbitra VRAM, log uruchomienia, „W terminalu”, „Edytuj YAML”. Na karcie: stan
  (działa / startuje / nie odpowiada), zajęty VRAM, zmierzony VRAM względem budżetu, kogo kafelek odsunął.
- **Arbiter VRAM**: przed startem liczy plan (kogo odsunąć, ile zostanie). Gdy start odsuwa inne usługi,
  pyta (GUI: dialog, CLI: t/N, MCP: `confirm=true`). Po Stop przywraca odsunięte. Gdy kafelek zniknie inaczej
  (np. `llm-stop` w terminalu), przywraca je sam po ok. 90 s. Stan w `~/.config/command-center/state.json`.
- **Profile**: `~/.config/command-center/profiles/*.yaml` (LLM, LLM + RAG oszczędny, Strata, TTS, Wszystko wyłączone).
- **Zasoby**, **Procesy** (filtr, widok drzewa, SIGTERM/SIGKILL, nice), **Docker** (kontener i cały projekt compose, logi).
- **Terminal**: jedna trwała powłoka bash; przeładowanie strony jej nie zamyka.
- **Dziennik**: każda akcja z gui / cli / api / mcp / tray / arbiter, plik `~/.config/command-center/actions.jsonl`.
- **Konfiguracja**: edytor YAML (walidacja przed zapisem, commit w git w `~/.config/command-center`, historia
  i przywracanie wersji), stan serwera MCP, gotowe wpisy dla klientów, lista wtyczek.
- **Zmiany plików** w `~/.config/command-center` są wczytywane same (bez przycisku).
- **Serwer MCP** pod http://127.0.0.1:8770/mcp (streamable HTTP, tylko lokalnie).
- **Ikona w zasobniku**: VRAM w ikonie, podpowiedź z właścicielami, menu profili, start/stop usług, aplikacje, kiosk.
- **Wtyczki**: `~/.config/command-center/plugins/*.py` z `register(ctx)`; przykład: `dyski.py` (karta + narzędzie MCP).

## CLI

`cctl` (dowiązanie w `~/.local/bin`):

```bash
cctl status                 # zasoby, właściciele VRAM, profil
cctl tiles                  # stan kafelków, zmierzony VRAM, kogo odsunęły
cctl plan llm -p "mniej VRAM (cache 6000, ctx 32k)"
cctl start llm cache=6000   # pokaże plan i zapyta; -y wykonuje bez pytania
cctl stop llm               # + przywrócenie RAG i kokoro
cctl profiles; cctl profile llm-rag
cctl ps --vram; cctl kill PID; cctl renice PID 10
cctl actions -n 20 -s mcp
```

## Kafelki

Pola: `id`, `title`, `type` (app, service, container, stack, command), `group`, `help`, `shell_init`, `start`, `stop`,
`pidfile`, `match` (regex na linię poleceń), `container`, `compose_project`, `url`, `health` (GET, 2xx = gotowe),
`options`, `presets`, `vram`, `on_start`, `on_stop`.

Opcja z `flag` trafia do `{args}` (tylko gdy różni się od domyślnej; bool jako sama flaga). Opcja bez `flag`
jest wstawiana w miejsce `{id}` (bool jako 1/0). `custom: true` przy enum pozwala wpisać własną wartość (np. ścieżkę .gguf).

Zależności:

```yaml
vram:
  budget_mib: "11400 if legacy else 3016 + cache"   # liczba albo wyrażenie po opcjach
  priority: 80                                      # niższe ustępują pierwsze, gdy brakuje VRAM
  yield: {http_post: "http://127.0.0.1:8100/debug/gpu", json: {mode: cpu}}   # domyślnie: stop
  resume: {http_post: "http://127.0.0.1:8100/debug/gpu", json: {mode: auto}} # domyślnie: start
on_start:
  - {yield: rag, if: rag_cpu}               # zawsze odsuń, gdy opcja prawdziwa
  - {yield: kokoro-tts, unless: keep_kokoro}
on_stop: restore                            # albo none
```

## MCP

Poziom w `~/.config/command-center/config.yaml`, klucz `mcp_level`: `read` | `control` (domyślny) | `full`.
Wpisy dla Hermesa, opencode, Claude Desktop i Claude Code są w karcie Konfiguracja.

## Dostęp zdalny (wyłączony)

`remote.enabled: true` w `config.yaml` + token (min. 16 znaków): panel nasłuchuje na `remote.host`, a żądania spoza
127.0.0.1 muszą podać token (`Authorization: Bearer`, raz `?token=` w adresie, potem ciasteczko). Wymaga restartu
demona. Serwer MCP zostaje dostępny tylko lokalnie.
