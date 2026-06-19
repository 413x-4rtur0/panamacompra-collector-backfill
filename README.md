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
records/YY-MM-DD/[finish]-[NUMERO]-[desc]/    NUMERO.json, NUMERO.detail.{json,html,txt}, NUMERO.calendar.ics, tables/*.json
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


### Updating an existing local copy

On the workstation, update the existing checkout safely with:

```bash
cd ~/Apps/panamacompra-collector
./update_local_copy.sh
```

The update script stops active collector workers, refuses to continue if local
uncommitted changes would be overwritten, fast-forwards the current branch, refreshes
the Python virtual environment dependencies, fixes executable bits, and runs the
system review. It intentionally uses `git pull --ff-only`, so it will not create a
merge commit or leave a half-resolved conflict during unattended updates. To request
a small smoke run after the update, use:

```bash
cd ~/Apps/panamacompra-collector
PC_UPDATE_TEST_DETAIL_LIMIT=5 ./update_local_copy.sh
```

#### Recovering from a blocked merge or PR checkout

If Git reports `Merging is not possible because you have unmerged files`, finish or
abandon the in-progress merge before trying another branch. The safest recovery path
on the workstation is:

```bash
cd ~/Apps/panamacompra-collector
git status --short --branch
git merge --abort
git fetch --all --prune
git checkout main
git pull --ff-only origin main
```

Then switch to the branch you want to test and update it from the refreshed `main`.
Run the merge only if the checkout command succeeds:

```bash
gh pr checkout 3
git merge main
# If conflicts are reported, edit the listed files, then:
git status --short
git add <resolved-files>
git commit
```

If `gh pr checkout 3` says the branch has diverged or cannot fast-forward, the PR
branch was likely force-pushed after your local checkout was created. If you do not
need to preserve local commits on that PR branch, reset the local branch to the
remote PR branch and try again:

```bash
git merge --abort 2>/dev/null || true
git checkout main
git branch -D codex/fix-progress-bar-bugs-in-monitor-hein5x
git fetch origin codex/fix-progress-bar-bugs-in-monitor-hein5x
git checkout -B codex/fix-progress-bar-bugs-in-monitor-hein5x \
  origin/codex/fix-progress-bar-bugs-in-monitor-hein5x
git merge main
```

After any failed checkout or merge, stop and re-run `git status --short --branch`
before continuing. Do not run the next merge command if the checkout command failed,
because it may merge into whichever branch is currently checked out.

A message such as `not something we can merge` usually means the branch name is not
available locally. Fetch it first, or merge the remote-tracking name directly, for
example `git fetch origin codex/review-project-for-enhancements-e8vmmy` followed by
`git merge origin/codex/review-project-for-enhancements-e8vmmy`.

### Setup

```bash
cd ~/Apps/panamacompra-collector

# System packages: browser + complete Python venv support + optional Tk monitor
sudo apt update && sudo apt install -y chromium python3-venv python3-full python3-tk

# Python environment
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Optional: only needed if you do not want to use the system Chromium package
python -m playwright install chromium
```

`requirements.txt` intentionally lists only pip-installable Python modules. The
collector's non-stdlib runtime module is `playwright`; `tkinter` and the Python
stdlib extension `_posixsubprocess` come from the operating-system Python
packages above. If an existing `.venv` fails with `ModuleNotFoundError:
_posixsubprocess`, remove and recreate `.venv` after installing `python3-venv` /
`python3-full`.

---

## Usage

All commands assume you are in the project directory.

```bash
# Run a small full sequence (index + 5 detail pages) and open the native monitor
./pc_request_run_all.sh 5
./pc_open_monitor.sh   # native Tk window; no Firefox/web browser

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
| `pc_monitor_tk.py` | Preferred lightweight native Tk monitor window; no Firefox/browser or web server required. |
| `pc_monitor_server.py` | Optional local browser monitor at `http://127.0.0.1:8766/`; loads once, polls lightweight JSON, and auto-closes after completion. |
| `pc_monitor_window.sh` | Optional live terminal progress monitor; auto-closes when idle. |
| `pc_open_monitor.sh` | Opens/starts the native Tk monitor by default. Set `PC_MONITOR_MODE=web` for browser monitor or `PC_MONITOR_MODE=terminal` for terminal monitor. |
| `pc_run_all_status.sh` | One-shot status snapshot. |
| `pc_stop_run_all.sh` | Emergency stop for stuck index/detail/worker processes. |
| `pc_follow_run_all.sh` | `tail -f` of the worker and current-run logs. |
| `migrate_previous_records.py` / `.sh` | Migrate old flat `records/NUMERO/` folders into dated archive folders; detail download then renames them to `[finish]-[NUMERO]-[desc]`. |
| `pc_rename_record_folders.py` | Rename record folders to `[finish]-[numero]-[desc]` from already-saved data. Dry-run by default; `--apply` to act. |
| `pc_build_detail_views.py` | Backfill the `summary` / numbered `items` / `calendar` views into existing `detail.json` files from saved text/tables (browser-free). Dry-run by default; `--apply` to act. |
| `pc_update_day_folder.py` | Manually **re-download** every record in a `YY-MM-DD` day folder from the live portal (overwriting saved HTML/text/detail JSON/calendar ICS/tables) to refresh records pulled under an earlier portal version. Prompts for the day (default today) or takes `--date`; lists and asks before downloading, or `--apply` to skip the prompt. |
| `review_panamacompra_system.sh` | Health check: required scripts, compile/syntax checks, process and status review. |
| `update_local_copy.sh` | Safe in-place updater for an existing checkout: stop workers, fast-forward Git, refresh dependencies, run health checks. |

---

## Configuration

Behavior is controlled with environment variables (all optional):

| Variable | Default | Used by | Meaning |
|----------|---------|---------|---------|
| `PC_MAX_PAGES_PER_GROUP` | `20` | index collector | Max pages crawled per status group. |
| `PC_DETAIL_LIMIT` | `10` | detail downloader | Max detail pages per run. |
| `PC_MAX_DETAIL_ATTEMPTS` | `5` | detail downloader | A record that fails this many times is no longer retried. |
| `PC_DESC_SLUG_MAX` | `40` | folder naming | Max length of the `[description]` token in the record-folder name. |
| `PC_RENAME_AFTER_DETAIL` | `1` | detail downloader | Auto-rename each folder to `[finish]-[numero]-[desc]` after a successful detail save. Set `0` to keep `<numero>`. |
| `PC_CALENDAR_TZ` | `America/Panama` | detail views | Timezone recorded in each record's `calendar` event. |
| `PC_CALENDAR_ATTENDEES` | `alex.gutierrez@craw-ds.com,razelgutierrez@gmail.com` | detail views | Comma-separated attendee emails for the `calendar` event. |
| `PC_WEBHOOK_DETAIL_LIMIT` | `999999` | `run_collector.sh` | Detail limit applied to webhook-triggered runs. |
| `PC_WEBHOOK_HOST` | `0.0.0.0` | webhook listener | Bind address. Keep `0.0.0.0` for Docker; use `127.0.0.1` to restrict to localhost. |
| `PC_WEBHOOK_PORT` | `8765` | webhook listener | Listen port. |
| `PC_MONITOR_MODE` | `tk` | monitor opener | `tk` opens the native Tk monitor; `web` starts the browser monitor; `terminal` tries the old graphical-terminal monitor. |
| `PC_MONITOR_TK_REFRESH_SECONDS` | `3` | native monitor | Native Tk monitor refresh interval while a run is active. Minimum is 2 seconds. |
| `PC_MONITOR_TK_IDLE_REFRESH_SECONDS` | `15` | native monitor | Slower native Tk refresh interval after the system is idle/done. |
| `PC_MONITOR_TK_AUTO_CLOSE_SECONDS` | `20` | native monitor | Seconds to wait after completion before closing the native monitor window. Use `0` to disable. |
| `PC_MONITOR_TK_GEOMETRY` | `980x760` | native monitor | Initial native monitor window size. |
| `PC_MONITOR_HOST` | `127.0.0.1` | web monitor | Bind address for the local web monitor. |
| `PC_MONITOR_PORT` | `8766` | web monitor | Port for the local web monitor. |
| `PC_MONITOR_WEB_REFRESH_SECONDS` | `3` | web monitor | Lightweight JSON polling interval while a run is active. Minimum is 3 seconds. |
| `PC_MONITOR_WEB_IDLE_REFRESH_SECONDS` | `30` | web monitor | Slower JSON polling interval after the system is idle/done. |
| `PC_MONITOR_WEB_AUTO_CLOSE_SECONDS` | `20` | web monitor | Seconds to wait after completion before the web monitor tries to close its tab/window. Use `0` to disable. |
| `PC_MONITOR_REFRESH_SECONDS` | `5` | terminal monitor | Poll interval for process/log changes. The screen only redraws when state changes or the force-redraw interval elapses. |
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
            ├── NUMERO.calendar.ics             # importable calendar event
            └── tables/
                └── NUMERO.table_001.json
```

> `panamacompra_index.csv` is written once per `NUMERO` at first insert and is **not**
> updated afterwards, so it is a first-seen log, not a mirror of current state. Query
> the SQLite database for the live picture.

### Record folder naming

Folders are created as `NUMERO` during the index scan, then renamed to encode the
key facts once detail data is available:

```text
records/YY-MM-DD/[<finish>]-[<numero>]-[<desc>]/
              e.g. [2022-10-11_12:00]-[2022-0-12-214-12-CL-008498]-[FRS-126--CMPRS-D-CJ-PLSTC]
```

- **`<finish>`** = `YYYY-MM-DD_HH:MM` when proposals stop being accepted: the **end**
  time of the *"Fecha y hora presentación de cotizaciones"* window (24-hour). For older
  records without that field, the delivery (*entrega*) date at `12:00` is used. Empty `[]`
  if no date can be found.
- **`<numero>`** = the PanamaCompra `NUMERO`, unchanged.
- **`<desc>`** = the request description, accent-stripped, uppercased, with **vowels
  removed**, non-alphanumerics turned into `-`, truncated to `PC_DESC_SLUG_MAX` chars.

New records are renamed automatically by the detail downloader after each successful
save (disable with `PC_RENAME_AFTER_DETAIL=0`). To rename folders that already exist on
disk, run the tool below (reads only saved files, no network):

```bash
./pc_rename_record_folders.py            # dry-run: preview every planned rename
./pc_rename_record_folders.py --apply    # rename folders and update the database
```

It is idempotent (already-named folders are skipped) and never overwrites an existing
target. Inner files keep their `NUMERO.*` names.

#### Keeping new and previous records in the same format

There are two supported paths, and both converge on the same
`[finish]-[numero]-[desc]` leaf format:

1. **New records** — run the normal collector. The index scan first creates a
   temporary `records/YY-MM-DD/NUMERO/` folder; once detail data is downloaded,
   `pc_detail_downloader.py` computes the finish stamp and description from the
   detail text/tables and automatically renames the folder.
2. **Previous records already on disk** — run `pc_rename_record_folders.py`. It
   reads the saved `NUMERO.detail.txt` and `tables/*.json`, previews the same
   target name in dry-run mode, and applies the rename only with `--apply`.
   If a previous folder only has the index `NUMERO.json`, run
   `pc_update_day_folder.py --date YY-MM-DD --apply` to fetch its detail page
   first; the day updater syncs those index-only folders into SQLite before it
   lists records to download.

Recommended review/test commands before and after applying updates:

```bash
# 1) Preview old-folder renames without changing files
./pc_rename_record_folders.py --limit 20

# 2) Apply old-folder renames after the preview looks right
./pc_rename_record_folders.py --apply

# 3) Preview detail view/calendar backfill for previous records
./pc_build_detail_views.py

# 4) Apply detail view/calendar backfill for previous records
./pc_build_detail_views.py --apply

# 5) Test new records with a small live run, then check the resulting folder name
./pc_request_run_all.sh 5
./pc_run_all_status.sh
```

Use `pc_update_day_folder.py --date YY-MM-DD --apply` when previous records
need to be **fetched/re-fetched from the live portal** instead of just renamed
or rebuilt from saved detail files. This includes index-only folders that have
`NUMERO.json` but do not yet have `NUMERO.detail.json`, `NUMERO.detail.html`,
or `NUMERO.detail.txt`.

### Detail tables and links

- Each two-column detail table also gets a **`key_values`** object
  (`{"Fecha y hora presentación de cotizaciones": "19-06-2026 - 08:00 AM a 12:00 PM", …}`)
  alongside the existing row arrays.
- Saved links are filtered to the **useful** ones only — document attachments
  (`pdf`/`doc`/`xls`/`zip`/…) and real PanamaCompra opportunity/portal URLs — dropping
  navigation, in-page `#/` router links, `mailto:`/`javascript:`, and asset noise.
  Already-saved archives are re-cleaned automatically from their stored HTML on the next
  run (no re-download).

### Detail summary, items, and calendar

Each `detail.json` also carries three structured views, parsed from the saved detail
text (and the on-page items grid). They hold the same facts the old `SUMMARY.csv` /
`items.csv` / `.ics` artifacts did, but in one JSON document:

- **`summary`** — curated key facts: `numero`, `descripcion`, `objeto_de_la_contratacion`,
  `entidad`, `dependencia`, `unidad_de_compra`, `direccion`, `provincia_de_entrega`,
  `contacto` (`nombre`/`cargo`/`telefono`/`correo_electronico`), `forma_de_entrega`,
  `dias_de_entrega`, `forma_de_pago`, `dia_y_hora_de_entrega`, `precio_estimado`,
  `enlace_publico`, `enlace_interno`.
- **`items`** (+ **`items_count`**) — the **numbered** item list: each entry has `r`,
  `codigo`, `clasificacion`, `cantidad`, `unidad_de_medida`, `descripcion`, `ses`.
  Códigos and clasificación come from the on-page items grid when present; otherwise
  quantity/unit/description are read from the detail text (códigos left blank).
- **`calendar`** — an ICS `VEVENT` expressed as JSON: `uid`, `summary`, `dtstart`/`dtend`
  (the *Día y Hora de Entrega* window in 24-hour, on the delivery date), `timezone`,
  `location`, `organizer` (the record's contact), `attendees`, `url_publico`,
  `url_interno`, `precio_estimado`, and a human-readable `description`. A sibling
  `NUMERO.calendar.ics` file is also written so the event can be imported into a
  calendar app later.
- **`fields_detected`** — the raw `Label: value` pairs parsed from the detail text.

New records get these automatically. Existing archives are backfilled from their saved
HTML on the next detail run (the detail schema version was bumped). To backfill without a
browser or network, run:

```bash
./pc_build_detail_views.py            # dry-run: preview every record
./pc_build_detail_views.py --apply    # write the views into detail.json
```

Calendar timezone and attendees are configurable with `PC_CALENDAR_TZ` and
`PC_CALENDAR_ATTENDEES`. The JSON calendar view is the source of truth; the
`.calendar.ics` file is a portable review/import copy generated during detail
downloads, day-folder refreshes, and `pc_build_detail_views.py --apply`. The
ICS export follows the legacy review format as closely as possible: configured
attendees are emitted as top-level `ATTENDEE:MAILTO:...` lines, `DTSTART` /
`DTEND` use `TZID=<timezone>;VALUE=DATE-TIME`, `DTSTAMP` is emitted with a
trailing `Z`, organizer lines include quoted `CN` and `ROLE` parameters when
available, and the description includes the public/internal links, price,
record number, request description, entity/dependency/contact/delivery/payment
fields, and item rows.

> **Portal versions.** The collector reads the current
> `…/Inicio/#/solicitud-de-cotizacion/{numero}/{token}` pages (both *abierta* and
> *programada* states). Links to the previous-version preview
> (`…/Inicio/v2/#!/vistaPreviaCP?NumLc=…`) are recognized (classified `vista-previa`)
> and kept.

### Re-downloading a day folder

`pc_build_detail_views.py` and the automatic schema-version refresh only re-parse
**saved** HTML — they never go back to the portal. To actually re-fetch records from
the live site (for example, a day's records first captured under an earlier portal
version that you now want pulled as the current one), use:

```bash
./pc_update_day_folder.py                  # prompt for the day (default: today), then confirm
./pc_update_day_folder.py --date 26-06-18  # a specific day folder (YY-MM-DD or YYYY-MM-DD)
./pc_update_day_folder.py --date yesterday --apply
./pc_update_day_folder.py --apply          # today's folder, no confirmation
```

It selects every record whose `date_folder` matches (the day it was first seen),
lists them, and — after a confirmation, or immediately with `--apply` — force
re-downloads each one from its stored detail link, **overwriting** its saved HTML,
text, detail JSON, calendar `.ics`, and table JSONs, re-deriving the
summary/items/calendar views and re-naming the folder if the finish date, `NUMERO`,
or description changed. Without `--date` it prompts (defaulting to today)
and shows the day folders present in the database.

> Re-fetching uses each record's stored `link`. If the listing URLs may have changed,
> run an index scan first so links and `last_seen` are refreshed. The index scan
> also checks for an existing `NUMERO.json` anywhere under `records/YY-MM-DD/`,
> including renamed `[finish]-[numero]-[desc]` folders, before creating a new
> plain `NUMERO` folder. This prevents duplicate archives when the original
> `NUMERO/` leaf was already renamed after detail download.

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

The default monitor is now the native Tk window (`pc_monitor_tk.py`). Run
`./pc_open_monitor.sh` to open a lightweight desktop window without starting Firefox,
a browser engine, or a web server. It shows the real progress bar, current step/item,
diagnostics counters, process status, and recent log tails. When the run is done, the
window slows its refresh and auto-closes after the configured delay.

The browser monitor remains available for hosts where Tk is not installed or where a
remote browser dashboard is preferred: `PC_MONITOR_MODE=web ./pc_open_monitor.sh`,
then open `http://127.0.0.1:8766/`. The terminal UI is still available with
`PC_MONITOR_MODE=terminal ./pc_open_monitor.sh`. If no GUI can be opened, use
`./pc_run_all_status.sh` or `./pc_follow_run_all.sh` from any terminal.

Key logs under `data/logs/`:

| Log | Contents |
|-----|----------|
| `run_all_current.log` | The run currently in progress. |
| `run_all_history.log` | Appended history of completed runs. |
| `run_all_worker.log` | Worker lifecycle events. |
| `run_all_requests.log` | Run-all requests. |
| `collector_triggered.log` | Webhook → collector triggers. |
| `webhook_listener.log` | Webhook listener activity. |
| `monitor_open.log` | Attempts to start/open the web monitor, terminal monitor, or fallback. |
| `monitor_tk.log` | Background native Tk monitor output/errors. |
| `monitor_server.log` | Background web monitor server output. |
| `run_all_follow.log` | Background log-follow fallback when no GUI monitor can be opened. |

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


**Webhook triggers the collector but no monitor window opens** — terminal windows can fail when launched by changedetection.io, cron, or a background service. Use the web monitor URL first, then check the monitor opener log and text fallback if needed:

```bash
tail -80 data/logs/monitor_open.log
python3 pc_monitor_tk.py --snapshot
PC_MONITOR_MODE=web ./pc_open_monitor.sh  # optional browser monitor
./pc_run_all_status.sh
./pc_follow_run_all.sh
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
- Records are stored under `records/YY-MM-DD/[finish]-[NUMERO]-[desc]/` and existing files are skipped, not overwritten
