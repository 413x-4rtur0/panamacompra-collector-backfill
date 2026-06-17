# PanamaCompra Collector

A local monitoring and archival system for **PanamaCompra** opportunities.

It watches the PanamaCompra *Cotizaciones en Línea* table, detects new or changed
opportunities, stores each one under its stable `NUMERO`, and optionally downloads
the full detail page into a local archive.

The project targets a **low-resource Linux workstation**: it never runs parallel
browser sessions and performs the index scan and detail download sequentially.

---

## Table of contents

1. [What it does](#what-it-does)
2. [How it works](#how-it-works)
3. [Design decisions](#design-decisions)
4. [Installation](#installation)
5. [Usage](#usage)
6. [Scripts reference](#scripts-reference)
7. [Configuration](#configuration)
8. [Data and storage](#data-and-storage)
9. [changedetection.io and the webhook](#changedetectionio-and-the-webhook)
10. [Monitoring and logs](#monitoring-and-logs)
11. [Troubleshooting](#troubleshooting)
12. [Security notes](#security-notes)
13. [Development](#development)

---

## What it does

The system collects and archives public opportunities from PanamaCompra. For each
opportunity it stores:

- `NUMERO` (the stable unique id), status group (`Programadas` / `Abiertas`), status
- Description, entity, dependency, table date, modality
- Detail URL, full detail HTML and visible text
- JSON metadata, plus one JSON file per table found in the detail page
- An estimated closing/finish date when one can be detected from the detail text

The unique identifier is always the PanamaCompra `NUMERO`, e.g. `2026-0-07-02-02-CL-058693`.
Visual row number and page number are intentionally **ignored** — they change whenever
new opportunities are inserted at the top of the table.

---

## How it works

```text
changedetection.io          detects a change in the PanamaCompra table
        │
        ▼
webhook_listener.py         receives the webhook
        │
        ▼
run_collector.sh            requests the full collector sequence
        │
        ▼
pc_request_run_all.sh       creates data/queue/run_all_requested.flag
        │
        ▼
pc_run_all_worker.sh        single locked worker
        │
        ├─ STEP 1  pc_index_collector.py   scans Programadas + Abiertas + pagination
        └─ STEP 2  pc_detail_downloader.py downloads pending detail pages
        │
        ▼
records/YY-MM-DD/NUMERO/    NUMERO.json, NUMERO.detail.{json,html,txt}, tables/*.json
```

The workflow has two phases run back-to-back by the worker:

| Phase | Script | Work |
|-------|--------|------|
| **Index scan** | `pc_index_collector.py` | Open the table, select *Programadas*, set 50 rows/page, crawl all pages, repeat for *Abiertas*. Save lightweight index JSON + DB records. |
| **Detail download** | `pc_detail_downloader.py` | Read pending records from SQLite, visit each detail URL, save HTML / text / metadata / table JSON, mark as saved. |

> The current architecture is **run-all only**. The older queue-based system is
> deprecated and is not part of this repository.

---

## Design decisions

- **`NUMERO` is the primary key.** Row number, page number, changedetection history
  position and visual order are all unstable and are never used as identifiers.
- **Index and detail are split.** Scanning the table is light; downloading detail
  pages is heavy. Splitting them keeps browser work small at any one moment.
- **One browser process at a time.** The worker takes a `flock` lock. If a new
  trigger arrives while it is running, a request flag is left behind and the worker
  runs one more full sequence after it finishes — no duplicate Chromium sessions.
- **Immutable archive.** Existing JSON / HTML / text files are never overwritten;
  completed folders are skipped.

---

## Installation

The scripts resolve their own location, so the project can live in **any directory**
(`~/Apps/panamacompra-collector` is just the example used below).

### Requirements

- Linux (Debian / Ubuntu / Linux Mint recommended)
- Python 3.10+ (3.12 used in development)
- Chromium or Google Chrome — the collectors use the system browser if present at
  `/usr/bin/chromium`, `/usr/bin/google-chrome`, or `/usr/bin/chromium-browser`
- Shell tools: `bash`, `flock`, `timeout`, `pgrep`, `pkill`, `tail`, `sed`, `grep`, `find`, `date`, `tee`
- Optional: `sqlite3` CLI for manual inspection

### Setup

```bash
cd ~/Apps/panamacompra-collector

# Browser (system Chromium)
sudo apt update && sudo apt install -y chromium

# Python environment
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

---

## Usage

All commands assume you are in the project directory.

```bash
# Run a small full sequence (index + 5 detail pages) and open the monitor
./pc_request_run_all.sh 5
./pc_open_monitor.sh

# Run the full sequence (index + all pending detail pages)
./pc_request_run_all.sh

# Run the worker in the foreground (this terminal)
./pc_run_all_now.sh 5

# Check status
./pc_run_all_status.sh

# Follow the logs live
./pc_follow_run_all.sh

# Emergency stop (use only if a browser step is frozen)
./pc_stop_run_all.sh
```

You can also run a single phase manually:

```bash
source .venv/bin/activate
./pc_index_collector.py                 # index scan only
PC_DETAIL_LIMIT=5 ./pc_detail_downloader.py   # download up to 5 pending details
```

---

## Scripts reference

| Script | Role |
|--------|------|
| `pc_common.py` | Shared module: paths, DB schema, JSON helpers, URL/date detection. **Not run directly.** |
| `pc_index_collector.py` | Index scan. Crawls Programadas + Abiertas, writes index JSON and DB records. |
| `pc_detail_downloader.py` | Detail download. Saves HTML/text/metadata/tables for pending records. |
| `pc_request_run_all.sh` | **Main entry point.** Requests a full run and starts the worker if idle. |
| `pc_run_all_worker.sh` | Locked sequential worker: index then detail; repeats if re-requested. |
| `pc_run_all_now.sh` | Runs the worker in the foreground for interactive use. |
| `run_collector.sh` | Bridge called by the webhook listener; requests a full run. |
| `webhook_listener.py` | Local HTTP listener for changedetection.io notifications. |
| `pc_monitor_window.sh` | Live terminal progress monitor; auto-closes when idle. |
| `pc_open_monitor.sh` | Opens the monitor in a graphical terminal (degrades gracefully with no display). |
| `pc_run_all_status.sh` | One-shot status snapshot. |
| `pc_stop_run_all.sh` | Emergency stop for stuck index/detail/worker processes. |
| `pc_follow_run_all.sh` | `tail -f` of the worker and current-run logs. |
| `migrate_previous_records.py` / `.sh` | Migrate old flat `records/NUMERO/` folders into `records/YY-MM-DD/NUMERO/`. |
| `review_panamacompra_system.sh` | Health check: required scripts, compile/syntax checks, process and status review. |

---

## Configuration

Behavior is controlled with environment variables (all optional):

| Variable | Default | Used by | Meaning |
|----------|---------|---------|---------|
| `PC_MAX_PAGES_PER_GROUP` | `20` | index collector | Max pages crawled per status group. |
| `PC_DETAIL_LIMIT` | `10` | detail downloader | Max detail pages per run. |
| `PC_MAX_DETAIL_ATTEMPTS` | `5` | detail downloader | A record that fails this many times is no longer retried. |
| `PC_WEBHOOK_DETAIL_LIMIT` | `999999` | `run_collector.sh` | Detail limit applied to webhook-triggered runs. |
| `PC_WEBHOOK_HOST` | `0.0.0.0` | webhook listener | Bind address. Keep `0.0.0.0` for Docker; use `127.0.0.1` to restrict to localhost. |
| `PC_WEBHOOK_PORT` | `8765` | webhook listener | Listen port. |
| `PC_MONITOR_REFRESH_SECONDS` | `5` | monitor | Poll interval for process/log changes. The screen only redraws when state changes or the force-redraw interval elapses. |
| `PC_MONITOR_FORCE_REDRAW_SECONDS` | `30` | monitor | Maximum seconds between redraws while the monitor is open, even if no state changed. |
| `PC_MONITOR_IDLE_CLOSE_SECONDS` | `8` | monitor | Delay before auto-closing once idle. |
| `PC_MONITOR_STABLE_DONE_CYCLES` | `3` | monitor | Idle cycles required before closing. |

The detail limit can also be passed positionally: `./pc_request_run_all.sh 5`.

---

## Data and storage

### Folder layout

```text
panamacompra-collector/
├── *.py, *.sh, requirements.txt, README.md   # code (committed)
├── data/                                      # runtime data (gitignored)
│   ├── panamacompra_archive.db                # SQLite database
│   ├── panamacompra_index.csv                 # append-only "first seen" log
│   ├── logs/
│   └── queue/
└── records/                                   # archive (gitignored)
    └── YY-MM-DD/
        └── NUMERO/
            ├── NUMERO.json                     # index record
            ├── NUMERO.detail.json              # detail metadata
            ├── NUMERO.detail.html              # full page HTML
            ├── NUMERO.detail.txt               # visible text
            └── tables/
                └── NUMERO.table_001.json
```

> `panamacompra_index.csv` is written once per `NUMERO` at first insert and is **not**
> updated afterwards, so it is a first-seen log, not a mirror of current state. Query
> the SQLite database for the live picture.

### Database

SQLite database at `data/panamacompra_archive.db`, table `opportunities`
(primary key `numero`):

| Field | Notes |
|-------|-------|
| `numero` | Primary key. |
| `grupo`, `tipo_url`, `estado` | Status group, URL type, status. |
| `descripcion`, `short_description` | Full and shortened description. |
| `entidad`, `dependencia`, `fecha`, `modalidad` | Index table fields. |
| `link` | Detail URL. |
| `first_seen`, `last_seen` | Timestamps. |
| `date_folder`, `record_folder`, `index_json_path` | Archive locations. |
| `detail_status` | `pending`, `saved`, or `failed`. |
| `detail_attempts`, `detail_saved_at`, `detail_json_path` | Detail tracking. |
| `finish_date_guess` | Closing date guessed from detail text. |

Useful queries:

```bash
# Total records
sqlite3 data/panamacompra_archive.db "SELECT COUNT(*) FROM opportunities;"

# Pending details
sqlite3 data/panamacompra_archive.db \
  "SELECT COUNT(*) FROM opportunities WHERE detail_status != 'saved';"

# Records that have repeatedly failed
sqlite3 data/panamacompra_archive.db \
  "SELECT numero, detail_attempts FROM opportunities WHERE detail_status = 'failed';"

# Latest activity
sqlite3 data/panamacompra_archive.db \
  "SELECT numero, grupo, estado, detail_status, finish_date_guess
   FROM opportunities ORDER BY last_seen DESC LIMIT 20;"
```

---

## changedetection.io and the webhook

changedetection.io is run separately (typically via Docker, with `sockpuppetbrowser`).
It is used **only** to detect changes and fire the webhook — it must not download
detail pages itself.

**Watch URL**

```text
https://www.panamacompra.gob.pa/Inicio/#/cotizaciones-en-linea/cotizaciones-en-linea
```

**Watch configuration**

- Fetch method: Playwright / Chromium (JavaScript mode)
- JS actions: close popup → click *Programadas* → set 50 rows/page → crawl pages →
  click *Abiertas* → set 50 rows/page → crawl pages → output stable text keyed by `NUMERO`
- CSS filter: `#pc-monitor-output`
- Do **not** include visual row number, page number, or generated timestamps (they cause false alerts)

**Webhook**

changedetection notifies the local listener. Create the token first:

```bash
printf 'YOUR_SECRET_TOKEN' > .webhook_token
python3 webhook_listener.py
```

The listener accepts requests at `/panamacompra/<TOKEN>`:

```text
Local:        http://127.0.0.1:8765/panamacompra/YOUR_TOKEN
From Docker:  http://host.docker.internal:8765/panamacompra/YOUR_TOKEN
```

On a valid request it runs `run_collector.sh`, which requests the full sequence. It
does not start a browser session directly.

---

## Monitoring and logs

The progress monitor (`pc_monitor_window.sh`) shows phase, status, a progress bar,
elapsed time, the detail limit, the current action, live process status, and recent
log tails. The progress percentage is estimated while a browser step runs; the real
completion state is determined by process status and worker exit. The monitor closes
automatically once the worker, index collector, and detail downloader are all idle and
no request flag remains.

Key logs under `data/logs/`:

| Log | Contents |
|-----|----------|
| `run_all_current.log` | The run currently in progress. |
| `run_all_history.log` | Appended history of completed runs. |
| `run_all_worker.log` | Worker lifecycle events. |
| `run_all_requests.log` | Run-all requests. |
| `collector_triggered.log` | Webhook → collector triggers. |
| `webhook_listener.log` | Webhook listener activity. |

```bash
tail -120 data/logs/run_all_current.log
```

---

## Troubleshooting

**Monitor stays open** — a real process is probably still running:

```bash
pgrep -af "pc_run_all_worker|pc_index_collector|pc_detail_downloader|timeout .*pc_" || true
```

**Index collector runs too long** — inspect, then stop if frozen:

```bash
tail -120 data/logs/run_all_current.log
./pc_stop_run_all.sh
```

**"No pending detail rows."** — every record has `detail_status = saved`:

```bash
sqlite3 data/panamacompra_archive.db \
  "SELECT COUNT(*) FROM opportunities WHERE detail_status != 'saved';"
```

**Webhook does not trigger the collector** — check the logs and confirm `.webhook_token`
exists and matches the URL:

```bash
tail -80 data/logs/webhook_listener.log
tail -80 data/logs/collector_triggered.log
```

**Data files show up in git** — verify `.gitignore` is working:

```bash
git status --short   # records/, data/, .venv/, .webhook_token must not appear
```

---

## Security notes

- Never commit `.webhook_token`, the database, downloaded records, or logs — all are
  covered by `.gitignore`.
- The webhook token travels in the URL path, so keep the listener on a trusted network.
  Set `PC_WEBHOOK_HOST=127.0.0.1` if changedetection runs on the same host (outside Docker).
- Use a private repository unless the project has been reviewed for public release.

---

## Development

```bash
# Health check (compile + syntax + process/status review)
./review_panamacompra_system.sh

# Verify everything compiles / parses
python -m py_compile pc_common.py pc_index_collector.py pc_detail_downloader.py \
  webhook_listener.py migrate_previous_records.py
for f in *.sh; do bash -n "$f"; done
```

The repository contains **code only**. Runtime data (`data/`, `records/`, `.venv/`,
`.webhook_token`, databases, logs, downloaded HTML/text) is excluded by `.gitignore`.

### System is healthy when

- All Python files compile and all shell scripts pass `bash -n`
- `pc_run_all_status.sh` shows no stuck process
- The webhook creates a run-all request
- The index scan reports zero duplicate `NUMERO`
- The detail downloader reports no pending rows after completion
- Records are stored under `records/YY-MM-DD/NUMERO/` and existing files are skipped, not overwritten
