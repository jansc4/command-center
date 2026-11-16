# Command Center

Panel do zarządzania zasobami komputera i lokalnymi usługami. Projekt: dokument „Command Center — projekt aplikacji”.

## Uruchomienie

Panel działa jako usługa użytkownika i startuje z sesją graficzną: http://127.0.0.1:8770

```bash
systemctl --user status command-center     # stan
systemctl --user restart command-center    # po zmianie kodu
journalctl --user -u command-center -f     # log demona
systemctl --user disable --now command-center   # wyłączenie
```

Jednostka: `systemd/command-center.service`, zainstalowana w `~/.config/systemd/user/`. `KillMode=process`
sprawia, że restart panelu nie zabija usług uruchomionych z kafelków.

Ręcznie (bez usługi): `uv run command-center --show`.

## CLI

`cctl` (dowiązanie w `~/.local/bin`) rozmawia z demonem:

```bash
cctl status
cctl tiles
cctl show llm -p "stary układ" ctx=32768     # tylko podgląd komendy
cctl start llm cache=6000
cctl stop llm
cctl log llm
cctl ps --vram
```

Opcje: `--port N`, `--host ADRES` (domyślnie tylko 127.0.0.1), `--native` (okno pywebview, jeszcze nietestowane).

## Co jest (etapy 1-4)

- **Pasek górny**: CPU, RAM, VRAM podzielony na właścicieli (kafelek, kontener, proces).
- **Launcher**: kafelki z `~/.config/command-center/tiles/*.yaml`; opcje jako kontrolki, presety, help, podgląd komendy, log uruchomienia.
- **Zasoby**: rdzenie CPU, RAM, swap, GPU NVIDIA (NVML) i AMD (sysfs), historia 5 min.
- **Procesy**: filtr, sortowanie, SIGTERM / SIGKILL z ostrzeżeniem, gdy proces należy do usługi.
- **Docker**: kontenery z projektem compose, start / stop / restart, logi.
- **Terminal**: bash z Twoimi aliasami w karcie panelu; przycisk „W terminalu” w opcjach kafelka wkleja tam komendę.
- **REST API** pod `/api/...` (status, processes, containers, tiles): baza dla CLI i MCP.

## Kafelki

Przy pierwszym starcie katalog `tiles/` z repozytorium jest kopiowany do `~/.config/command-center/tiles/`.
Od tej chwili edytujesz kopię w `~/.config`; przycisk „Wczytaj definicje ponownie” przeładowuje pliki.
Inny katalog konfiguracji: zmienna `CC_CONFIG`.

Pola kafelka: `id`, `title`, `type` (app, service, container, stack, command), `group`, `help`,
`shell_init`, `start`, `stop`, `pidfile`, `match` (regex na linię poleceń), `container`,
`compose_project`, `url`, `options`, `presets`, `vram`.

Opcja z `flag` trafia do `{args}` (tylko gdy różni się od wartości domyślnej; bool jako sama flaga).
Opcja bez `flag` jest wstawiana w miejsce `{id}` w szablonie.

## Jeszcze nie ma

Arbiter VRAM i profile (5), serwer MCP (6), wtyczki, zasobnik, usługa systemd (7).
Na razie zależności między usługami (RAG na CPU, stop kokoro) są wpisane w komendy kafelków, tak jak w `llm-start`.
