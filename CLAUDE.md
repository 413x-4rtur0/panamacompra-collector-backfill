# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Linux-only, low-resource monitoring/archival system for PanamaCompra procurement opportunities: changedetection.io (docker) watches the portal → webhook → a staged, flock-locked collection pipeline on the host → SQLite (`var/data/panamacompra.db`) + per-record folders (`var/records/<NUMERO>/`) → WhatsApp announcements via the WAHA container. Collection is deliberately **sequential, single-browser** — never parallelize it without explicit approval (CONTRIBUTING.md).

`docs/ARCHITECTURE.md` is the verified deep reference (every claim cites file:line); `docs/MONITOR_RELATIONSHIPS.md` and `README.md` complement it.

## Commands

```bash
./bin/pcc <cmd>                 # CLI hub for everything (case statement in bin/pcc)
./bin/pcc start 5               # test run: index scan + up to 5 details
./bin/pcc watch|db|set|calendar # terminal monitor / counters / settings / calendar
./setup.sh                      # full install (apt + .venv + Playwright + docker)

# Required checks before a PR (CONTRIBUTING.md):
python -m compileall -q .
./scripts/validate-installation.sh --skip-browser
while IFS= read -r f; do bash -n "$f"; done < <(find . -maxdepth 4 -type f -name '*.sh' -not -path './.git/*' -not -path './.venv/*')

# Tests are stdlib unittest, one file each:
.venv/bin/python -m unittest tests.test_waha_directory_search        # single file
.venv/bin/python -m unittest discover tests                          # all
```

Use `.venv/bin/python` for anything importing project code. Numbered scripts (`NNN-name.py`) can't be imported normally — load via `importlib.util.spec_from_file_location` (see the top of `001b-monitor-web.py` for the pattern).

## Critical operational traps

- **Auto-stash wipes uncommitted work.** `PC_RUN_UPDATE_BEFORE_RUN=1` makes each collector run (~every 30 min) execute `src/20_pipeline/000-update-before-run.sh`, which `git stash push`es uncommitted **tracked** edits and never pops them. **Commit immediately after changes pass tests.** Recover from `git stash list` ("update-before-run autostash").
- **Runtime config lives outside git** in `var/data/config/` (gitignored): `monitor_settings.env` (all `PC_*` settings incl. manager login + WAHA key), `waha_clients.json` (client profiles), `monitor_users.json` (staff users), `monitor_web_secret.txt` (session HMAC key). Never commit credentials into `VALUE_SETTING_DEFAULTS` defaults.
- **Blank-save protection:** `save_monitor_setting()` in `001b-monitor-web.py` ignores blank writes to `PROTECTED_NONBLANK_SETTINGS` (WAHA key, manager login, Firebase config). To intentionally clear one, edit `monitor_settings.env` by hand.
- **WAHA API key resolution:** `monitor_common.waha_api_key()` = env → monitor settings → repo `.env` fallback (the key the container was started with). A 401 from WAHA in the monitor almost always means an empty key setting, not a WAHA problem; the sender reads the env and keeps working.
- **"STOP all runners" (`120a-stop-everything.sh`) deliberately leaves all monitors running.** Docker containers are a separate lifecycle (`src/50_tools/010-docker-stack.sh`); updating one container: `docker compose pull <svc> && docker compose up -d --no-deps <svc>`. changedetection state persists in `var/integrations/changedetection` (container-owned files — back up with `docker cp`, not tar).
- Real logs are in `var/data/logs/` (`PC_LOG_DIR`), **not** `var/log/`.

## Web monitor (`src/40_monitor/001b-monitor-web.py`) — the big one

A single stdlib `ThreadingHTTPServer` file (~4000 lines) serving three self-contained pages plus all APIs; runs as systemd user unit `panamacompra-monitor-web.service` on port 8766 (LAN-exposed). Restart after edits: `systemctl --user restart panamacompra-monitor-web.service`.

- **Three embedded HTML pages:** `HTML` (admin dashboard) is one giant **f-string — all JS/CSS braces must be doubled `{{ }}`**; `LOGIN_HTML` and `CLIENT_CALENDAR_HTML` are plain strings (single braces). `_LOC_TOOLTIP_JS` is shared verbatim between admin and client pages. The client page intentionally **mirrors** the admin calendar CSS/JS rather than sharing an include — when changing calendar appearance/behavior, update both.
- **Auth model:** front page `/` is a login gate. `127.0.0.1` always bypasses (never locked out). Manager = `PC_ADMIN_USERNAME`/`PC_ADMIN_PASSWORD`; staff users (`monitor_users.json`) get only their granted tabs (`STAFF_POST_TAB_MAP` gates POSTs, `SENSITIVE_SETTING_KEYS` redacts `/api/status`); clients sign in via Firebase (email/Google; ID tokens verified server-side through identitytoolkit `accounts:lookup`, needs `PC_FIREBASE_WEB_API_KEY`) and land on `/client-calendar`, scoped by `?uid=`/profile. Sessions = HMAC cookies (`make_session_token`/`parse_session_token`). Open (no-auth) endpoints are listed in `OPEN_GET_PATHS`/`OPEN_POST_PATHS`. The server **trusts Firebase UIDs without per-request verification** (documented model, same as the Android app in `android/README.md`).
- **Client filters** (`client_filter_fn`) reuse the WhatsApp sender's `parse_filter_rules`/matching semantics (accent/case-insensitive substring; comma=OR, `+`=AND, leading `-`=NOT) over `_HAYSTACK_CACHE` (normalized, mtime-invalidated). If sender semantics change in `020-notify-whatsapp.py`, mirror there.
- **Design system:** pages are styled with the ARL-89 / HP-23 tokens (concrete/ink/amber/blueprint palette, IBM Plex + Archivo) pulled from the Claude Design project `129dfa45-…`; keep new UI on those CSS variables.
- After editing the page strings, validate: `python -m py_compile`, then render the page and `node --check` the extracted `<script>` blocks.

## Pipeline / notification map

- `src/10_webhook/` listener (systemd `panamacompra-webhook.service`, port 8765). changedetection triggers are **ignored in manual mode** — `PC_WEBHOOK_AUTO_RUN=0` logs "automatic runs are disabled"; that flag, not networking, is the usual reason runs stop.
- `src/20_pipeline/100-run-worker.sh` runs STEP 0–8 (index → notify → details → views/templates → detail notify → repair deadlines → calendar → summary); progress lands in `var/run/run_all_progress.env`, read by all monitors.
- `src/30_notify/010-waha-client.py` sends WhatsApp; per-purpose destinations + filters live in `var/data/config/waha_*` files; every send is mirrored to the `app_notifications` table for the client apps.
- Shared engines: `src/common.py` (paths, DB, settings precedence env > `monitor_settings.env` > default) and `src/40_monitor/monitor_common.py` (KPI/data used by web + Tk + `pcc kpi` so numbers agree).

## Review standards (from CONTRIBUTING.md)

- Never overwrite archived record payloads unless the script is explicitly a repair/re-download tool.
- Update README/docs for user-facing command, config, monitor, or notification changes.
- Prefer env-var feature flags for risky operational changes.
