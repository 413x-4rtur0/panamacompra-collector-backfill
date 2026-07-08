# Architecture

Verified architecture reference for the PanamaCompra Collector. Every relationship
cites the file (and where useful, the line or function) that proves it. Produced
by the July 2026 codebase audit; update alongside structural changes.

## Overview

The system watches the PanamaCompra portal for opportunity table changes, runs a
staged collection pipeline on the host, stores results in SQLite + per-record
folders, and announces new/changed opportunities over WhatsApp via WAHA. Two
GUIs (native Tk and web) plus a terminal watcher expose progress, KPIs, records
and settings.

```
SOURCE                 panamacompra.gob.pa (BASE_URL, src/common.py:76)
  │
  ├─[watch]─ changedetection container (docker-compose.yml) + browser-steps JS
  │             └─ notification json://host:8765/panamacompra/<token>
  │                   └─ src/10_webhook/010-webhook-listener.py  (handle_trigger)
  │                        ├─ direct: 060-run-collector.sh → 110a-request-run.sh (PC_RUN_MODE=AUTO)
  │                        └─ enqueue-only (docker): touch run_all_requested.flag
  │                              └─ 050-watch-queue-flag.sh (host) → 110a-request-run.sh
  │
  └─[manual]─ pcc start / monitors' "Request run" → 110a / 110b
                    │
                    ▼
      src/20_pipeline/100-run-worker.sh   (flock-locked loop)
        STEP 0  000-update-before-run.sh
        STEP 1  015-import-index-snapshot.py (AUTO) / 010-collect-index.py
        STEP 2  020-notify-whatsapp.py --announce
        STEP 3  030-collect-details.py (+ inline detail messages)
        STEP 4  040-build-detail-views.py --apply + 020-record-templates.py
        STEP 5  020-notify-whatsapp.py --announce-details
        STEP 6  py_compile + 050-repair-missing-deadlines.py
        STEP 7  060-build-calendar.py
        SUMMARY src/30_notify/010-waha-client.py (purpose=summary)
        STEP 8  070-test-zone.py (opt-in via PC_TEST_ZONE_AUTORUN)
        │
        └─ writes run_all_progress.env / run_all_last_summary.env / logs
              ▲ read by ▼
      MONITORS: 001a-monitor-tk.py · 001b-monitor-web.py · 001c-monitor-terminal.sh · 002-next-run-timer.py
```

## Entry points

| Entry point | Role | Proof |
|---|---|---|
| `bin/pcc` | CLI hub — dispatches every command | case statement `bin/pcc:100-489` |
| `setup.sh` | Installer (apt, venv, Playwright browser, docker) | sources `lib/env.sh`; `PC_SETUP_SKIP_*` |
| `docker-compose.yml` | changedetection + sockpuppetbrowser + WAHA + enqueue-only webhook | services block |
| `systemd/user/panamacompra.service` | `pcc start` as user service | `ExecStart` line |
| `systemd/user/panamacompra-webhook.service` | `pcc webhook start --foreground` | `ExecStart` line |
| `scripts/tasks/0*.sh` | 5-line wrappers `exec`ing into `pcc`/root scripts (desktop launcher targets) | e.g. `scripts/tasks/020-start-collector.sh` |
| `update-local-copy.sh`, `review-system.sh` | Root maintenance scripts, also reachable via `pcc` (`pcc health` → `review-system.sh`) | `bin/pcc:460` |

## Configuration spine

`lib/env.sh` is sourced by nearly every script. It:

- Detects `APP_MODE` (installed / portable / development) and derives all
  `PC_*` paths (`PC_DATA_DIR`, `PC_RECORDS_DIR`, `PC_LOG_DIR`, `PC_QUEUE_DIR`,
  `PC_ARCHIVE_DB_PATH`, …).
- Loads config with precedence **environment variable > `config/defaults.env` >
  `.env` > `$PC_CONFIG_DIR/env`** (`lib/env.sh:75-80` snapshots and restores the
  caller's env so real env vars always win).
- Deliberately sets only `-u`/`pipefail`, never `-e` (documented in its header:
  `100-run-worker.sh` and `120b-stop-collectors.sh` opt out of `-e`).

Runtime settings written by the monitors and `pcc set` live in
`$PC_DATA_DIR/config/monitor_settings.env`; the worker sources it at startup
(`100-run-worker.sh:16-22`). Python code resolves the same paths through
`src/common.py` (`STATE_DIR`, `DATA_DIR`, …).

## The pipeline orchestrator

`src/20_pipeline/100-run-worker.sh` holds a `flock` on
`/tmp/panamacompra_run_all_worker.lock` and loops while
`run_all_requested.flag` exists in `PC_QUEUE_DIR`.

Step semantics (all verified in the file):

1. **Update** (`000-update-before-run.sh`): git fetch + ff-only pull, falling
   back to `git reset --hard origin/<branch>`; tracked local edits are
   auto-stashed. Failure does NOT abort the run (`:232-241`).
2. **Index**: AUTO runs with `PC_INDEX_FROM_SNAPSHOT!=0` first try
   `015-import-index-snapshot.py`, which parses the changedetection Brotli
   snapshot and feeds records through `common.insert_or_update_index`
   (`src/common.py:1366`) — the same dedup/transition path the crawler uses.
   Exit 3 = partial snapshot → hybrid: `010-collect-index.py` crawls only the
   unhealthy groups via `PC_INDEX_GROUPS` (`:298-328`).
3. **Messaging (index)**: `020-notify-whatsapp.py --announce` sends per-record
   "Nueva Oportunidad" alerts BEFORE the long download phase (`:392`).
4. **Details**: `030-collect-details.py` downloads detail pages; with
   `PC_NOTIFY_DETAILS_INLINE=1` (default) it sends each "Detalles Completos"
   follow-up right after that record's download by dynamically loading module
   020 (`load_script` at the top of 030).
5. **Views** (`040-build-detail-views.py --apply --since <run start>`):
   normalizes `summary`/`items`/`calendar` inside each `*.detail.json` and
   rewrites per-record `.calendar.ics`. Then `020-record-templates.py apply`
   copies operator-selected work templates into new record folders.
6. **Messaging (details)**: `020 --announce-details` is the idempotent catch-up
   (guarded by `detail_notified_at`); with details disabled it runs
   `--sync-snapshots` so items are absorbed silently.
7. **Verify + repair**: `py_compile` of the core scripts, then
   `050-repair-missing-deadlines.py --include-failed --apply`.
8. **Calendar**: `060-build-calendar.py` builds timestamped `.ics` packages
   under `data/calendar/<day>/`.
9. **Summary**: `src/30_notify/010-waha-client.py` sends the run summary to the
   `summary` purpose destination (`:623-630`), including per-stage durations.
10. **Test zone** (opt-in, `PC_TEST_ZONE_AUTORUN=1`): `070-test-zone.py` re-runs
    the last N records in the isolated `records_test/` sandbox when the run had
    no new records.

Failure handling: each failed step writes `FAILED` progress, sends a WAHA
`failed` event, and skips downstream steps (`:338-351`, `:561-581`). An abrupt
worker exit restores the request flag so the next start resumes
(`mark_abrupt_exit_for_resume`, `:169-186`), unless the stop-no-resume flag was
set by a deliberate stop.

## Trigger paths

- **Automatic**: changedetection (running the Browser Steps script from
  `config/changedetection-browser-steps.js`) fires its notification URL
  `json://<host>:8765/panamacompra/<token>`. The listener
  (`src/10_webhook/010-webhook-listener.py`) validates the token with
  `hmac.compare_digest` on the URL path, replies 202 immediately, and either
  launches `060-run-collector.sh` (host mode) or, in `PC_WEBHOOK_ENQUEUE_ONLY=1`
  container mode, just touches `run_all_requested.flag`;
  `050-watch-queue-flag.sh` on the host polls the flag and launches the run.
  Both AUTO paths set `PC_RUN_MODE=AUTO` and uncapped limits.
- **Manual**: `pcc start` → `110a-request-run.sh` (queue + spawn worker with
  nohup) or `110b-run-now.sh` (foreground worker). The monitors call the same
  scripts (`001a-monitor-tk.py:1268-1281`).

## Notification design

Two senders, deliberately split:

- `src/30_notify/010-waha-client.py` — dependency-free plain-text sender for
  system/summary events. Per-purpose destination routing
  (`default|index|details|status|system|summary`) with a documented fallback
  chain (`configured_chat_id`, `:89-102`), retry with exponential backoff, and
  an in-process circuit breaker (`send_text`, `:225-258`). Never blocks a run:
  missing configuration exits 0.
- `src/20_pipeline/020-notify-whatsapp.py` — rich record messages (index alerts,
  detail follow-ups, status changes), per-destination keyword filters
  (`load_filter_rules`/`evaluate_filter`, also used by `pcc keywords test`),
  customizable message formats (`src/50_tools/040-message-formats.py`), and
  monitor-visible per-message progress.

Both talk to WAHA's `POST /api/sendText` (container from `docker-compose.yml`,
optional `X-Api-Key`).

## Storage

- **SQLite** `data/panamacompra_archive.db`, table `opportunities`, NUMERO as
  the natural key; additive schema migrations via the `ALTER TABLE` map in
  `src/common.py:1166-1183` (detail status/timestamps, `notified_at`,
  `detail_notified_at`, status-change tracking, item hashes).
- **CSV** `data/panamacompra_index.csv` (index mirror).
- **Record folders** `records/<day>/(<finish>)-(<numero>)-(<desc>)/` with
  detail txt/json, tables, per-record `.calendar.ics`, and `templates/`.
- **Calendar packages** `data/calendar/<day>/*.ics`.
- **Logs & progress** in `data/logs/`: `run_all_worker.log`,
  `run_all_current.log`, `run_all_history.log`, and the monitors' data feed —
  `run_all_progress.env` (live phase/percent/ETA) and
  `run_all_last_summary.env` (per-stage seconds, index source).

## Modes

`APP_MODE` (lib/env.sh:37-49): `installed` (XDG dirs), `portable`
(`var/.portable`), `development` (git checkout, state under `var/`).

## Known caveats (from the audit)

- The changedetection **watch configuration** lives in the container datastore
  (`var/integrations/changedetection/`), not in the repo; it must match
  `config/changedetection-browser-steps.js` manually.
- `systemd/user/panamacompra.service` uses `Type=simple` but `pcc start` exits
  after queueing — the unit goes inactive after each trigger. Works, but is
  misdeclared (planned fix: Phase 5 of the improvement plan).
- `000-update-before-run.sh` can `git reset --hard` a diverged branch —
  appliance behavior that is dangerous on a development machine.
- Worker detection is by process-name grep (`pgrep -f "[r]un-worker.sh"`) in
  several scripts; renaming `100-run-worker.sh` would silently break them.
- `config/defaults.env` and `docker-compose.yml` ship a fixed default WAHA
  dashboard password; setup should generate a random one instead (Phase 5).
