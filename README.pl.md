# Command Center

[🇬🇧 English](README.md) · 🇵🇱 Polski

Lokalny panel do zarządzania zasobami komputera i własnymi usługami: CPU, RAM i VRAM (z podziałem na właścicieli),
procesy, Docker oraz Twoje komendy startowe jako konfigurowalne kafelki. Działa jako demon w tle pod adresem
`http://127.0.0.1:8770`, ma CLI (`cctl`), ikonę w zasobniku i serwer MCP, żeby asystent też mógł obsługiwać
maszynę. Wszystko zostaje lokalnie, nic nie idzie do chmury.

> Stan: wczesny (v0.1.0), pisany pod jedną stację roboczą z Linuksem (Kubuntu, KDE Plasma, karta NVIDIA).
> Dołączone kafelki to przykłady dopasowane do ustawień autora; dostosuj je do siebie.

## Co jest

- **Pasek górny**: CPU, RAM, VRAM podzielony na właścicieli; przełącznik profili.
- **Launcher**: kafelki z `~/.config/command-center/tiles/*.yaml`. Opcje (zaawansowane zwinięte), presety, help,
  podgląd komendy, plan arbitra VRAM, log uruchomienia, „W terminalu”, „Edytuj YAML”. Na karcie: stan
  (działa / startuje / nie odpowiada), zajęty VRAM, zmierzony VRAM względem budżetu, kogo kafelek odsunął.
- **Arbiter VRAM**: przed startem liczy plan (kogo odsunąć, ile zostanie). Gdy start odsuwa inne usługi,
  pyta (GUI: dialog, CLI: t/N, MCP: `confirm=true`). Po Stop przywraca odsunięte. Gdy kafelek zniknie inaczej
  (np. `llm-stop` w terminalu), przywraca je sam po ok. 90 s. Stan w `~/.config/command-center/state.json`.
- **Profile**: `~/.config/command-center/profiles/*.yaml`; przełączenie wykonuje plan arbitra dla całej listy
  (najpierw stop, potem start po kolei).
- **Zasoby**, **Procesy** (filtr, widok drzewa, SIGTERM/SIGKILL, nice), **Docker** (kontener i cały projekt
  compose, logi).
- **Terminal**: jedna trwała powłoka bash; przeładowanie strony jej nie zamyka.
- **Dziennik**: każda akcja z gui / cli / api / mcp / tray / arbiter trafia do
  `~/.config/command-center/actions.jsonl`.
- **Konfiguracja**: edytor YAML (walidacja przed zapisem, każdy zapis to commit w git w
  `~/.config/command-center`, historia i przywracanie wersji), stan serwera MCP, gotowe wpisy dla klientów,
  lista wtyczek.
- **Zmiany plików** w `~/.config/command-center` są wczytywane same (bez przycisku).
- **Serwer MCP** pod `http://127.0.0.1:8770/mcp` (streamable HTTP, tylko lokalnie).
- **Ikona w zasobniku**: VRAM w ikonie, podpowiedź z właścicielami, menu profili, start/stop usług, aplikacje, kiosk.
- **Kiosk** na drugi monitor: `http://127.0.0.1:8770/kiosk`.
- **Wtyczki**: `~/.config/command-center/plugins/*.py` z `register(ctx)`; przykład: `plugins/dyski.py`
  (karta + narzędzie MCP).

## Wymagania

- Linux z usługami użytkownika systemd (rozwijane na Kubuntu / KDE Plasma Wayland)
- Python 3.12–3.13 i [uv](https://docs.astral.sh/uv/)
- Karta NVIDIA ze sterownikami (monitoring VRAM używa `nvidia-ml-py`)
- Docker (opcjonalnie, dla karty Docker)
- Systemowy `python3` z PyQt6 dla ikony w zasobniku (np. `sudo apt install python3-pyqt6`)

## Instalacja

```bash
git clone https://github.com/jansc4/command-center.git ~/projects/command_center
cd ~/projects/command_center
uv sync

# usługi użytkownika (jednostki zakładają repo w ~/projects/command_center, w innym razie popraw ścieżki)
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now command-center command-center-tray

# CLI
mkdir -p ~/.local/bin && ln -s "$PWD/.venv/bin/cctl" ~/.local/bin/cctl
```

Przy pierwszym uruchomieniu dołączone `tiles/` i `profiles/` są kopiowane do `~/.config/command-center/`;
działa ta kopia. Otwórz <http://127.0.0.1:8770>.

## Uruchamianie

```bash
systemctl --user restart command-center              # demon (po zmianie kodu)
journalctl --user -u command-center -f               # log demona
systemctl --user restart command-center-tray         # ikona w zasobniku
systemctl --user disable --now command-center-tray   # bez ikony
uv run command-center --show                         # ręcznie, otwiera przeglądarkę
```

`KillMode=process` w jednostce sprawia, że restart panelu nie zabija usług uruchomionych z kafelków.
Okno „jak aplikacja”: `chromium --app=http://127.0.0.1:8770` (to samo robi „Otwórz panel” w zasobniku).

## CLI

```bash
cctl status                 # zasoby, właściciele VRAM, profil
cctl tiles                  # stan kafelków, zmierzony VRAM, kogo odsunęły
cctl plan llm -p "mniej VRAM (cache 6000, ctx 32k)"
cctl start llm cache=6000   # pokaże plan i zapyta; -y wykonuje bez pytania
cctl stop llm               # + przywrócenie odsuniętych usług
cctl profiles; cctl profile llm-rag
cctl ps --vram; cctl kill PID; cctl renice PID 10
cctl actions -n 20 -s mcp
```

## Kafelki

Pola: `id`, `title`, `type` (app, service, container, stack, command), `group`, `help`, `shell_init`, `start`,
`stop`, `pidfile`, `match` (regex na linię poleceń), `container`, `compose_project`, `url`, `health`
(GET, 2xx = gotowe), `options`, `presets`, `vram`, `on_start`, `on_stop`.

Opcja z `flag` trafia do `{args}` (tylko gdy różni się od domyślnej; bool jako sama flaga). Opcja bez `flag`
jest wstawiana w miejsce `{id}` (bool jako 1/0). `custom: true` przy enum pozwala wpisać własną wartość
(np. ścieżkę `.gguf`).

Zależności między kafelkami:

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
127.0.0.1 muszą podać token (`Authorization: Bearer`, raz `?token=` w adresie, potem ciasteczko). Wymaga
restartu demona. Serwer MCP zostaje dostępny tylko lokalnie.

## Budowa projektu

| Plik | Rola |
|---|---|
| `config.py` | ścieżki, `config.yaml`, `StateStore` (state.json), git w `~/.config` |
| `tiles.py` | modele `Tile`/`Option`/`Vram`/`Step`/`Hook`/`Profile`, render, bezpieczne liczenie budżetu, `Registry`, `Runner` |
| `arbiter.py` | plany VRAM: `plan_tile`, `plan_profile`, `execute`, `restore` |
| `state.py` | pętla (próbkowanie, obserwacja plików, healthchecki, auto-przywracanie), start/stop, pomiar VRAM |
| `api.py`, `mcp_server.py` | REST i serwer MCP |
| `ui.py` | interfejs NiceGUI, strona `/kiosk` |
| `terminal.py`, `editor.py`, `actions.py`, `plugins.py`, `tray.py`, `cli.py` | jak w nazwie |

Zbudowane na [NiceGUI](https://nicegui.io/) i oficjalnym [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
Notatki przekazania i otwarte zadania: [`HANDOFF.md`](HANDOFF.md).

## Jeszcze niesprawdzone

Prawdziwy start LLM/Straty z kafelków, pomiar VRAM po starcie względem budżetu, menu ikony w codziennym użyciu,
„W terminalu” i akcje Dockera z GUI, tryb `remote` i `--native`. Arbiter był testowany na atrapach procesów,
nie na prawdziwych usługach.
