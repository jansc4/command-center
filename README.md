# Command Center

🇬🇧 English · [🇵🇱 Polski](README.pl.md)

A local control panel for your computer's resources and self-hosted services: CPU, RAM and VRAM
(split by owner), processes, Docker, and your own launch commands as configurable tiles.
Runs as a background daemon at `http://127.0.0.1:8770`, with a CLI (`cctl`), a tray icon and an MCP server
so an assistant can operate the machine too. Everything stays local, nothing goes to the cloud.

> Status: early (v0.1.0), built for a single Linux workstation (Kubuntu, KDE Plasma, NVIDIA GPU).
> The bundled tiles are examples tuned to the author's setup; adapt them to yours.

## Features

- **Top bar**: CPU, RAM, VRAM split between the processes that own it; profile switcher.
- **Launcher**: tiles loaded from `~/.config/command-center/tiles/*.yaml`. Options (advanced ones collapsed),
  presets, help text, command preview, VRAM plan, start log, "in terminal", "edit YAML". Each card shows the state
  (running / starting / not responding), VRAM in use, measured VRAM vs. budget, and who the tile displaced.
- **VRAM arbiter**: before a start it computes a plan (who has to yield, how much is left). If other services
  would be displaced it asks first (GUI: dialog, CLI: prompt, MCP: `confirm=true`). After Stop it restores what was
  displaced. If a tile disappears by other means (e.g. `llm-stop` in a shell) it restores them itself after ~90 s.
  State lives in `~/.config/command-center/state.json`.
- **Profiles**: `~/.config/command-center/profiles/*.yaml`; switching a profile executes an arbiter plan
  for the whole list (stop first, then start in order).
- **Resources**, **Processes** (filter, tree view, SIGTERM/SIGKILL, nice), **Docker** (containers and whole
  compose projects, logs).
- **Terminal**: one persistent bash shell; reloading the page does not close it.
- **Action log**: every action from gui / cli / api / mcp / tray / arbiter goes to
  `~/.config/command-center/actions.jsonl`.
- **Configuration**: YAML editor (validated before saving, every save is a git commit in
  `~/.config/command-center` with history and restore), MCP server status, ready-made client entries, plugin list.
- **Live reload** of files in `~/.config/command-center` (no button needed).
- **MCP server** at `http://127.0.0.1:8770/mcp` (streamable HTTP, local only).
- **Tray icon**: VRAM in the icon, tooltip with owners, profile menu, start/stop services, apps, kiosk view.
- **Kiosk view** for a second monitor: `http://127.0.0.1:8770/kiosk`.
- **Plugins**: `~/.config/command-center/plugins/*.py` with `register(ctx)`; example: `plugins/dyski.py`
  (a card plus an MCP tool).

## Requirements

- Linux with systemd user services (developed on Kubuntu / KDE Plasma Wayland)
- Python 3.12–3.13 and [uv](https://docs.astral.sh/uv/)
- NVIDIA GPU with drivers (VRAM monitoring uses `nvidia-ml-py`)
- Docker (optional, for the Docker tab)
- System `python3` with PyQt6 for the tray icon (e.g. `sudo apt install python3-pyqt6`)

## Installation

```bash
git clone https://github.com/jansc4/command-center.git ~/projects/command_center
cd ~/projects/command_center
uv sync

# user services (the unit files assume the repo lives in ~/projects/command_center, edit paths otherwise)
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now command-center command-center-tray

# CLI
mkdir -p ~/.local/bin && ln -s "$PWD/.venv/bin/cctl" ~/.local/bin/cctl
```

On first run the bundled `tiles/` and `profiles/` are copied to `~/.config/command-center/`; the copy there
is the one that runs. Open <http://127.0.0.1:8770>.

## Running

```bash
systemctl --user restart command-center              # daemon (after a code change)
journalctl --user -u command-center -f               # daemon log
systemctl --user restart command-center-tray         # tray icon
systemctl --user disable --now command-center-tray   # without the icon
uv run command-center --show                         # manually, opens the browser
```

`KillMode=process` in the unit means that restarting the panel does not kill services started from tiles.
An "app-like" window: `chromium --app=http://127.0.0.1:8770` (this is also what "Open panel" in the tray does).

## CLI

```bash
cctl status                 # resources, VRAM owners, profile
cctl tiles                  # tile state, measured VRAM, who they displaced
cctl plan llm -p "less VRAM (cache 6000, ctx 32k)"
cctl start llm cache=6000   # shows the plan and asks; -y runs without asking
cctl stop llm               # + restores displaced services
cctl profiles; cctl profile llm-rag
cctl ps --vram; cctl kill PID; cctl renice PID 10
cctl actions -n 20 -s mcp
```

## Tiles

Fields: `id`, `title`, `type` (app, service, container, stack, command), `group`, `help`, `shell_init`, `start`,
`stop`, `pidfile`, `match` (regex on the command line), `container`, `compose_project`, `url`, `health`
(GET, 2xx = ready), `options`, `presets`, `vram`, `on_start`, `on_stop`.

An option with `flag` goes into `{args}` (only when it differs from the default; bool as a bare flag).
An option without `flag` is substituted for `{id}` (bool as 1/0). `custom: true` on an enum allows a free value
(e.g. a `.gguf` path).

Dependencies between tiles:

```yaml
vram:
  budget_mib: "11400 if legacy else 3016 + cache"   # a number or an expression over the options
  priority: 80                                      # lower yields first when VRAM runs short
  yield: {http_post: "http://127.0.0.1:8100/debug/gpu", json: {mode: cpu}}   # default: stop
  resume: {http_post: "http://127.0.0.1:8100/debug/gpu", json: {mode: auto}} # default: start
on_start:
  - {yield: rag, if: rag_cpu}               # always displace when the option is true
  - {yield: kokoro-tts, unless: keep_kokoro}
on_stop: restore                            # or none
```

## MCP

The level is set in `~/.config/command-center/config.yaml` under `mcp_level`: `read` | `control` (default) | `full`.
Ready-made entries for Hermes, opencode, Claude Desktop and Claude Code are in the Configuration tab.

## Remote access (disabled)

`remote.enabled: true` in `config.yaml` plus a token (at least 16 characters): the panel then listens on
`remote.host`, and requests from outside 127.0.0.1 must present the token (`Authorization: Bearer`, once as
`?token=` in the URL, then a cookie). Requires a daemon restart. The MCP server stays local only.

## Project layout

| File | Role |
|---|---|
| `config.py` | paths, `config.yaml`, `StateStore` (state.json), git in `~/.config` |
| `tiles.py` | `Tile`/`Option`/`Vram`/`Step`/`Hook`/`Profile` models, rendering, safe budget evaluation, `Registry`, `Runner` |
| `arbiter.py` | VRAM plans: `plan_tile`, `plan_profile`, `execute`, `restore` |
| `state.py` | main loop (sampling, file watching, health checks, auto-restore), start/stop, VRAM measurement |
| `api.py`, `mcp_server.py` | REST API and MCP server |
| `ui.py` | NiceGUI interface, `/kiosk` page |
| `terminal.py`, `editor.py`, `actions.py`, `plugins.py`, `tray.py`, `cli.py` | as the names say |

Built with [NiceGUI](https://nicegui.io/) and the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).

## Not yet verified

Real LLM/Strata starts from tiles, VRAM measurement after start vs. budget, the tray menu in daily use,
"in terminal" and Docker actions from the GUI, `remote` mode and `--native`. The arbiter has been exercised on
dummy processes, not on the real services.

## License

[MIT](LICENSE)
