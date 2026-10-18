# CLAUDE.md: musicbot

General conventions (git workflow, PR titles, releases, attribution) come from the `std` plugin (pebba/claude-standards) and are active in every session. Put only what is specific to this repo here; delete sections that stay empty.

Never add Claude attribution (Co-Authored-By, "Generated with Claude Code", session links) to commits, PRs, tags, release notes or CHANGELOG.

## What this is

A self-hosted Discord music bot (discord.py, slash commands only) with YouTube Music, Spotify, live radio, autoplay, lyrics and per-server playlists. It runs as the systemd service `musicbot` on a VM with a self-hosted GitHub Actions runner.

## Commands

- Setup: `pip install -r requirements.txt` (in a venv; the server uses `venv/`)
- Run: `python bot.py` (reads `DISCORD_TOKEN`, optionally `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`, from `.env`)
- Check (same as the required CI job `lint`):
  - `python -m py_compile bot.py cogs/constants.py cogs/utils.py cogs/models.py cogs/views.py cogs/music.py`
  - `python -c "import json; json.load(open('radio_stations.json'))"`

There is no test suite and no formatter configured.

## Layout

- `bot.py`: entry point, loads the `cogs.music` extension and syncs slash commands.
- `cogs/`: the music cog and its models, views, utils and constants.
- `radio_stations.json`: stations for `/radio`.
- `scripts/`: systemd units, crash notification and the one-time VM setup (`setup-vm.sh`).
- `.github/workflows/`: `ci.yml` (lint), `deploy.yml` (deploy to the VM), `update-ytdlp.yml` (weekly yt-dlp upgrade), `dependabot-automerge.yml`, `release.yml`, `pr.yml`.

## Hard constraints (never violate)

- `yt-dlp[default]` stays unpinned in `requirements.txt`: `update-ytdlp.yml` upgrades it weekly and a pin would downgrade it on the next deploy.
- No privileged `message_content` intent; the bot uses slash commands only.
- The bot keeps `.busy` fresh while anyone is listening; `deploy.yml` relies on it to avoid restarting mid-song.

## Deviations from the standards

- Python without uv/ruff/pytest: dependencies are pinned in `requirements.txt`, and the repo keeps its own `ci.yml` (syntax and JSON check). `pr.yml` adds only the std PR-title/attribution check.
- Deployment stays in `deploy.yml` (push to `main`, plus an hourly poll because auto-merged Dependabot PRs don't trigger workflows). Releases use the generic std release (no build artifact).

## Release artifacts

- None (server bot). Latest: https://github.com/pebba/musicbot/releases/latest
