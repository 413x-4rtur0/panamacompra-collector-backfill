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
   - [Run the stack with Docker Compose](#run-the-stack-with-docker-compose)
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
changedetection.io          detects a change in the PanamaCompra table (Docker)
        │
        ▼
webhook_listener.py         receives the webhook
        │                     • host/systemd mode → runs run_collector.sh directly
        │                     • docker enqueue mode (PC_WEBHOOK_ENQUEUE_ONLY=1) →
        │                       only writes data/queue/run_all_requested.flag, then
        │                       pc_run_all_flag_watcher.sh (host) picks it up
        ▼
run_collector.sh            requests the full collector sequence
        │
        ▼
pc_request_run_all.sh       creates data/queue/run_all_requested.flag
        │
        ▼
pc_run_all_worker.sh        single locked worker
        │
        ├─ STEP 0  pc_update_before_run.sh fast-forwards local checkout before each run
        ├─ STEP 1  pc_index_collector.py   scans Programadas + Abiertas + pagination
        ├─ STEP 2  pc_detail_downloader.py downloads pending detail pages
        ├─ STEP 3  pc_build_calendar.py     writes timestamped .ics packages for new events
        ├─ STEP 4  pc_notify_new_records.py --announce  sends the WhatsApp messages one by one (visible MESSAGING step)
        └─ STEP 5  pc_test_zone.py          OPTIONAL, off by default: set PC_TEST_ZONE_AUTORUN=1 to re-run the last 5 in a sandbox when no new records
        │
        ▼
records/YY-MM-DD/[finish]-[NUMERO]-[desc]/    NUMERO.json, NUMERO.detail.{json,html,txt}, NUMERO.calendar.ics, tables/*.json
records_test/…                                isolated testing sandbox (shown as MODE=TEST in the monitor)
data/calendar/YY-MM-DD/YY-MM-DD_HH-MM_panamacompra_calendar_001.ics  normal-run import packages
```

### Simple user flow graphic

```text
Example: changedetection sees a new PanamaCompra row OC-2026-000123

[1 Detect] changedetection.io notices the table changed
      │
      ▼
[2 Queue] webhook_listener.py accepts /panamacompra/<token> and queues one run
      │
      ▼
[3 Index] pc_index_collector.py records the index row in SQLite
      │
      ▼
[4 Download] pc_detail_downloader.py downloads ALL detail fields + items first
      │
      ▼
[5 Compare] pc_notify_new_records.py compares against the last notified snapshot
      │
      ├─ No change ───────────────► no WhatsApp message
      │
      └─ New/status/items changed ─► one complete WhatsApp message for OC-2026-000123
                                      + data/calendar_exports/YYYY/MM/OC-2026-000123.ics
      │
      ▼
[6 Summary] worker sends one final run summary after messaging finishes
```

Key point: the webhook only starts/queues the run. WhatsApp is not sent from the
webhook and is not sent while detail rows are still downloading; messages are sent
after the collector has a complete record and can compare it safely.

### Process diagram and test visibility

```mermaid
flowchart TD
    A[changedetection.io or manual request] --> B[pc_request_run_all.sh]
    B --> C[data/queue/run_all_requested.flag]
    C --> D[pc_run_all_worker.sh with flock lock]
    D --> U[STEP 0: pc_update_before_run.sh]
    U --> E[STEP 1: pc_index_collector.py]
    E --> F[SQLite + records/YY-MM-DD/NUMERO index JSON]
    F --> G[STEP 2: pc_detail_downloader.py]
    G --> H[detail JSON, HTML, text, tables, per-record ICS]
    H --> I[STEP 3: pc_build_calendar.py]
    I --> J[data/calendar/YY-MM-DD timestamped ICS packages]
    J --> P[STEP 4: pc_notify_new_records.py --announce]
    P --> Q[WhatsApp messages sent one by one via WAHA, PHASE=MESSAGING]
    D --> K{No pending new details AND PC_TEST_ZONE_AUTORUN=1?}
    K -- yes --> L[STEP 5: pc_test_zone.py]
    L --> M[records_test/latest_5 + records_test/calendar/YY-MM-DD, MODE=TEST]
    D --> N[data/logs/run_all_progress.env]
    N --> O[pc_monitor_tk.py / pc_monitor_server.py]
```

The monitor now shows separate `normal_run` and `test_run` flags, plus worker/index/detail/calendar flags. Normal live runs show `MODE=LIVE`; the isolated test zone shows `MODE=TEST`, so it is visible when the worker is exercising code paths without touching the real archive.

The workflow has two phases run back-to-back by the worker:

| Phase | Script | Work |
|-------|--------|------|
| **Pre-run update** | `pc_update_before_run.sh` | Before each worker iteration, auto-stash any local edits to tracked files, fast-forward the local Git checkout (reset to remote if diverged), refresh installed Python requirements when `.venv` exists, and fix executable bits. Untracked runtime files never block it. If the update fails (e.g. no network), the worker logs a warning and **still runs** the collection with the current code instead of skipping. Set `PC_RUN_UPDATE_BEFORE_RUN=0` to skip the update entirely. |
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
  runs one more full sequence after it finishes — no duplicate Firefox sessions.
  If the worker exits before a clean shutdown, it restores the request flag so the
  next `pc_request_run_all.sh` start resumes pending database work instead of
  losing the interrupted task.
- **Immutable archive.** Existing JSON / HTML / text files are never overwritten;
  completed folders are skipped.

---

## Installation

The scripts resolve their own location, so the project can live in **any directory**
(`~/Apps/panamacompra-collector` is just the example used below).

### Requirements

- Linux (Debian / Ubuntu / Linux Mint recommended)
- Python 3.10+ (3.12 used in development)
- Playwright Firefox browser — install it in the active virtualenv with
  `python -m playwright install firefox`
- Shell tools: `bash`, `flock`, `timeout`, `pgrep`, `pkill`, `tail`, `sed`, `grep`, `find`, `date`, `tee`, and a desktop opener such as `xdg-open` for opening the test sandbox folder after monitor-launched tests
- Optional: `sqlite3` CLI for manual inspection


### Updating an existing local copy

On the workstation, update the existing checkout safely with:

```bash
cd ~/Apps/panamacompra-collector
./update_local_copy.sh
```

The update script always brings the checkout up to date. It stops active collector
workers, **auto-stashes** any local edits to *tracked* files (kept in the stash for
recovery, never lost), ignores untracked runtime files (`data/`, `records/`,
`.webhook_token`, `.venv.broken.*`, …), auto-selects the branch to track (the most
recent unmerged remote branch, otherwise `main`; pin one with `PC_UPDATE_BRANCH`),
fast-forwards it, refreshes
the Python virtual environment dependencies, fixes executable bits, and runs the
system review. If the webhook listener was running before the update, or if the
`panamacompra-webhook.service` user service is enabled, the updater restores it at
the end so changedetection.io does not keep seeing `Connection refused` after a
manual **Update + Monitor** launch. It also tries to restore the listener on failed
updates before exiting. It uses `git pull --ff-only`; if the local branch has diverged
and a fast-forward is impossible, it resets the branch to the remote (diverging
commits stay reachable via `git reflog`), so an unattended update never stops
half-way. To request a small smoke run after the update, use:

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
sudo apt update && sudo apt install -y python3-venv python3-full python3-tk

# Python environment
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Browser engine used by the collectors
python -m playwright install firefox
```

`requirements.txt` intentionally lists only pip-installable Python modules. The
collector's non-stdlib runtime module is `playwright`; `tkinter` and the Python
stdlib extension `_posixsubprocess` come from the operating-system Python
packages above. If an existing `.venv` fails with `ModuleNotFoundError:
_posixsubprocess`, install `python3-venv` / `python3-full` and rerun
`./update_local_copy.sh`; the updater detects an incomplete `.venv`, moves it to
`.venv.broken.YYYYMMDD_HHMMSS`, and recreates a clean one.

Operational shell wrappers use the repository `.venv` when it exists and fall
back to `python3` when it does not. The status/stop/monitor scripts recognize
collector processes launched by either `python` or `python3`. The local updater
verifies that Playwright Firefox can launch and installs it automatically unless
`PC_UPDATE_SKIP_BROWSER_INSTALL=1` is set.

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
| `pc_run_all_worker.sh` | Locked sequential worker: pre-run update, index, detail, calendar packaging, optional test zone; repeats if re-requested. A failed pre-run update only logs a warning — the worker still collects with the current code. |
| `pc_update_before_run.sh` | Lightweight pre-run updater called by the worker before every iteration; auto-stashes local tracked edits, fast-forwards Git (reset to remote if diverged) and refreshes requirements without stopping the active worker. Untracked runtime files never block it. |
| `pc_waha_notify.py` | Optional dependency-free WAHA notifier for short operational WhatsApp alerts (start/done/failed/…). Enabled only when WAHA environment variables are configured. |
| `pc_notify_new_records.py` | WhatsApp (WAHA) notifier helpers and entry point. The worker calls `--announce` in the visible MESSAGING step to send one “🟢 NUEVA OPORTUNIDAD DETECTADA” message per new record with per-message monitor progress; `--idle` sends “⚪ Sin nuevas entradas”; `--flush` retries failed sends. Supports an optional keyword filter and a first-use baseline so the existing archive is never re-announced. |
| `pc_run_all_now.sh` | Runs the worker in the foreground for interactive use. |
| `run_collector.sh` | Bridge called by the webhook listener; requests a full run. |
| `webhook_listener.py` | Local HTTP listener for changedetection.io notifications. Runs `run_collector.sh` directly, or (with `PC_WEBHOOK_ENQUEUE_ONLY=1`, as in the Docker stack) only writes the run request flag for the host runner. |
| `pc_run_all_flag_watcher.sh` | Host runner for the dockerized webhook: watches `data/queue/run_all_requested.flag` and launches the host collector (`pc_request_run_all.sh`) when a request is enqueued. Install as the `panamacompra-runner.service` user unit. |
| `docker-compose.yml` / `docker/Dockerfile.webhook` | Reproducible stack: changedetection.io + sockpuppetbrowser + WAHA + the enqueue-only webhook listener. |
| `pc_webhook_diagnostic.sh` | Diagnostic/fix helper for changedetection.io webhook reachability; starts the listener on `PC_WEBHOOK_HOST:PC_WEBHOOK_PORT`, tests local curl, and tests from the changedetection container when Docker is available. |
| `pc_monitor_tk.py` | Preferred lightweight native Tk monitor window with a vertical scrollbar; no Firefox/browser or web server required. Its manual buttons are grouped into Collector Runners, Updater & Migration, Data Tools, Testing & Validation, and Folder Management zones, with stop buttons and test-sandbox folder opening after test-zone completion. |
| `pc_next_run_timer.py` | Small **fixed-size** always-on-top dashboard centered near the top of the desktop (about 30 px down) counting down to the next live run. The countdown is anchored to the **last live run's start time** (from `run_all_progress.env`) plus the interval, so it tracks the real cadence and rolls forward if a run is overdue (falling back to clock boundaries when no previous run is recorded), and turns amber in the final minute. It also shows the **current git branch**, the **latest collected records** (newest NUMERO + short description, read from `data/panamacompra_archive.db`), and a **last-run summary** (New/Saved counts + total archive size). Withdraws while a live run is active and reappears when finished. Size/position and the number of records shown are configurable via `PC_NEXT_RUN_TIMER_WIDTH/HEIGHT/TOP` and `PC_NEXT_RUN_TIMER_RECORDS`. |
| `pc_monitor_server.py` | Optional local browser monitor at `http://127.0.0.1:8766/`; loads once, polls lightweight JSON, offers the same live/test run request and stop controls plus its own grouped action buttons, and auto-closes only after completed live runs. |
| `pc_monitor_window.sh` | Optional live terminal progress monitor; auto-closes when idle. |
| `pc_open_monitor.sh` | Opens/starts the native Tk monitor and the tiny next-run timer by default. Set `PC_MONITOR_MODE=web` for browser monitor or `PC_MONITOR_MODE=terminal` for terminal monitor. |
| `pc_run_all_status.sh` | One-shot status snapshot. |
| `pc_stop_run_all.sh` | Emergency stop for stuck index/detail/worker processes. |
| `pc_follow_run_all.sh` | `tail -f` of the worker and current-run logs. |
| `migrate_previous_records.py` / `.sh` | Migrate old flat `records/NUMERO/` folders into dated archive folders; detail download then renames them to `[finish]-[NUMERO]-[desc]`. |
| `pc_rename_record_folders.py` | Rename record folders to `[finish]-[numero]-[desc]` from already-saved data. Dry-run by default; `--apply` to act. |
| `pc_build_detail_views.py` | Backfill the `summary` / numbered `items` / `calendar` views into existing `detail.json` files from saved text/tables (browser-free). Dry-run by default; `--apply` to act. |
| `pc_update_day_folder.py` | Manually **re-download** every record in a `YY-MM-DD` day folder from the live portal (overwriting saved HTML/text/detail JSON/calendar ICS/tables) to refresh records pulled under an earlier portal version. Prompts for the day (default today) or takes `--date`; lists and asks before downloading, or `--apply` to skip the prompt. |
| `pc_build_calendar.py` | Build timestamped `.ics` import packages from new/changed detail calendars under `data/calendar/YY-MM-DD/`, defaulting to 10 events per file. Runs automatically as STEP 3 after each detail step; use `--all` to package every saved record, `--flat` for the old parent-only layout, or `--legacy-combined` to also write the old single combined file. |
| `pc_test_zone.py` | **Testing zone.** Re-run the last N records (default 5) through the full pipeline in an isolated sandbox (`records_test/latest_5/`, throwaway DB, test calendar packages under `records_test/calendar/YY-MM-DD/`), leaving the real archive untouched. It does **not** run automatically anymore; opt in with `PC_TEST_ZONE_AUTORUN=1` to have STEP 5 run it when a run finds no new records, or launch it from the monitor's manual actions. The monitor shows `MODE=TEST` and the `test_run` flag. |
| `review_panamacompra_system.sh` | Health check: required scripts, compile/syntax checks, process and status review. |
| `update_local_copy.sh` | In-place updater for an existing checkout that always brings it up to date: stop **only the collector pipeline + webhook trigger** (never the updater/loader/monitor themselves, which previously caused the update to freeze or close on itself), auto-stash local tracked edits (kept for recovery), **auto-select the branch** (track `main` when the most recently updated remote branch is already merged into `main`, otherwise switch to that latest branch), reset to the remote, refresh dependencies, run health checks, and install the Update + Monitor desktop shortcut. Runtime data (`data/`, `records/`, `.venv`) is protected by `.gitignore` so the reset/`git clean` can never delete the archive or database. |
| `pc_update_loader.py` | Separate centered Tk updater loader window for desktop/manual updates. Appears first with a step-based progress bar, tails the update output, and opens the monitor only **after** the update finishes so the monitor reflects the already-updated code. |

---

## Configuration

Behavior is controlled with environment variables (all optional):

| Variable | Default | Used by | Meaning |
|----------|---------|---------|---------|
| `PC_MAX_PAGES_PER_GROUP` | `20` | index collector | Max pages crawled per status group. |
| `PC_DETAIL_LIMIT` | `10` | detail downloader | Max detail pages per run. |
| `PC_MAX_DETAIL_ATTEMPTS` | `5` | detail downloader | A record that fails this many times is no longer retried. |
| `PC_DESC_SLUG_MAX` | `24` | folder naming | Max length of the `[description]` token in the record-folder name. |
| `PC_RENAME_AFTER_DETAIL` | `1` | detail downloader | Auto-rename each folder to `[finish]-[numero]-[desc]` after a successful detail save. Set `0` to keep `<numero>`. |
| `PC_CALENDAR_TZ` | `America/Panama` | detail views | Timezone recorded in each record's `calendar` event. |
| `PC_CALENDAR_ATTENDEES` | `a2gutierrezmora@gmail.com,razelgutierrez@gmail.com` | detail views | Comma-separated attendee emails for the `calendar` event. |
| `PC_CALENDAR_PACKAGE_SIZE` | `10` | calendar builder | Maximum events per timestamped import package. Smaller packages reduce calendar-import reminder/edit overload. |
| `PC_CALENDAR_AUTO_IMPORT` | unset | calendar builder | Set to `1` to automatically open each generated `.ics` package with the desktop opener (`xdg-open`, `gio open`, or macOS `open`) after it is written. |
| `PC_CALENDAR_AUTO_IMPORT_CMD` | unset | calendar builder | Optional command run once per written `.ics` package, with the package path appended, for local auto-import/open workflows. Overrides the default opener used by `PC_CALENDAR_AUTO_IMPORT=1`. |
| `PC_WEBHOOK_DETAIL_LIMIT` | `99` | `run_collector.sh` | Detail limit per webhook-triggered run (also the default for the run-all worker / `pc_request_run_all.sh`). |
| `PC_TEST_ZONE_AUTORUN` | `0` | run-all worker | When `1`, the worker runs the idle testing zone (STEP 5) automatically when a run finds no new records. Default `0` keeps the autostart from launching it; the test zone stays available as a manual monitor action. |
| `PC_TEST_ZONE_LIMIT` | `5` | run-all worker | How many recent records the idle testing zone (STEP 5) re-runs in the sandbox when `PC_TEST_ZONE_AUTORUN=1`. `0` disables it. |
| `PC_RUN_UPDATE_BEFORE_RUN` | `1` | run-all worker | Run `pc_update_before_run.sh` before every worker iteration. Set `0` to skip automatic pre-run updates. |
| `PC_UPDATE_REMOTE` | `origin` | update scripts | Git remote used by `update_local_copy.sh` and `pc_update_before_run.sh`. |
| `PC_UPDATE_BRANCH` | auto-detect | update scripts | Optional **hard override** that pins the branch to track. When empty (default), `update_local_copy.sh` auto-selects: it stays on `main` if the most recently updated remote branch is already merged into `main`, otherwise it switches to that latest branch. `pc_update_before_run.sh` uses it (or the current branch) for its lightweight refresh. |
| `PC_UPDATE_TEST_DETAIL_LIMIT` | `0` | `update_local_copy.sh` | Optional smoke-run detail limit to request after a successful local update. |
| `PC_UPDATE_SKIP_BROWSER_INSTALL` | `0` | `update_local_copy.sh` | Set to `1` to skip automatic Playwright Firefox install during local updates. |
| `PC_UPDATE_INSTALL_MONITOR_SHORTCUT` | `1` | `update_local_copy.sh` | Installs/refreshes the **PanamaCompra Update + Monitor** desktop/application-menu shortcut. The shortcut opens the separate updater loader first, then starts the native monitor. Set to `0` to skip. |
| `PC_UPDATE_RESTART_WEBHOOK` | `auto` | `update_local_copy.sh` | Controls whether the updater restores `webhook_listener.py` after stopping it for a safe code update. `auto` restarts it when it was already running or when the `panamacompra-webhook.service` user service is enabled; `1` always starts it after update; `0` leaves it stopped. |
| `PC_WEBHOOK_HOST` | `0.0.0.0` | webhook listener | Bind address. Keep `0.0.0.0` for Docker; use `127.0.0.1` to restrict to localhost. |
| `PC_WEBHOOK_PORT` | `8765` | webhook listener | Listen port. |
| `PC_WEBHOOK_ENQUEUE_ONLY` | `0` | webhook listener | When `1` (set by the Docker `webhook` service), the listener only writes `data/queue/run_all_requested.flag` instead of running `run_collector.sh`, so a host runner performs the actual collection. |
| `PC_RUNNER_POLL_SECONDS` | `5` | `pc_run_all_flag_watcher.sh` | How often the host runner polls for an enqueued run request. |
| `PC_MONITOR_MODE` | `tk` | monitor opener | `tk` opens the native Tk monitor; `web` starts the browser monitor; `terminal` tries the old graphical-terminal monitor. |
| `PC_MONITOR_TK_REFRESH_SECONDS` | `3` | native monitor | Native Tk monitor refresh interval while a run is active. Minimum is 2 seconds. |
| `PC_MONITOR_TK_IDLE_REFRESH_SECONDS` | `15` | native monitor | Slower native Tk refresh interval after the system is idle/done. |
| `PC_MONITOR_TK_AUTO_CLOSE_SECONDS` | `20` | native monitor | Seconds to count down (centered on screen) after a LIVE run finishes before the native monitor closes itself. The countdown only starts once the monitor has actually watched a run go active→done, never when opening straight into an idle state, and never for test-zone runs. Set `0` to keep the window open until you close it manually. |
| `PC_MONITOR_TK_GEOMETRY` | `980x760` | native monitor | Initial native monitor window size; the window is centered automatically. |
| `PC_MONITOR_TK_ALPHA` | `0.85` | native monitor | Native monitor whole-window opacity (text shares it; Tk has no per-widget transparency). `0.85` is lightly translucent but readable; lower toward `0.30` for a more see-through window (clamped to 0.30–1.00). Editable live from the monitor's Settings panel; re-applied after the window is visible so it works on X11 WMs. |
| settings file | `data/config/monitor_settings.env` | native monitor / WAHA notifier | `KEY=VALUE` file written by the monitor's Settings panel (transparency, auto-close/refresh seconds, WAHA source label). Read at startup and by the notifier. Precedence: environment variable > this file > built-in default. |
| `PC_NEXT_RUN_TIMER` | `1` | monitor opener | Starts the tiny next-run timer together with the Tk monitor. Set to `0` to disable. |
| `PC_NEXT_RUN_INTERVAL_MINUTES` | `30` | next-run timer | Countdown interval for scheduled live runs. |
| `PC_NEXT_RUN_TIMER_TOP` | `30` | next-run timer | Pixels from the top edge of the screen for the timer window. |
| `PC_NEXT_RUN_TIMER_WIDTH` / `PC_NEXT_RUN_TIMER_HEIGHT` | `340` / `258` | next-run timer | Fixed timer window size (the window is not resizable). |
| `PC_NEXT_RUN_TIMER_RECORDS` | `3` | next-run timer | How many of the latest collected records to list in the timer. |
| `PC_MONITOR_HOST` | `127.0.0.1` | web monitor | Bind address for the local web monitor. |
| `PC_MONITOR_PORT` | `8766` | web monitor | Port for the local web monitor. |
| `PC_MONITOR_WEB_REFRESH_SECONDS` | `3` | web monitor | Lightweight JSON polling interval while a run is active. Minimum is 3 seconds. |
| `PC_MONITOR_WEB_IDLE_REFRESH_SECONDS` | `30` | web monitor | Slower JSON polling interval after the system is idle/done. |
| `PC_MONITOR_WEB_AUTO_CLOSE_SECONDS` | `20` | web monitor | Seconds to wait after completion before the web monitor tries to close its tab/window. Use `0` to disable. |
| `PC_MONITOR_REFRESH_SECONDS` | `5` | terminal monitor | Poll interval for process/log changes. The screen only redraws when state changes or the force-redraw interval elapses. |
| `PC_MONITOR_FORCE_REDRAW_SECONDS` | `30` | monitor | Maximum seconds between redraws while the monitor is open, even if no state changed. |
| `PC_MONITOR_IDLE_CLOSE_SECONDS` | `8` | monitor | Delay before auto-closing once idle. |
| `PC_MONITOR_STABLE_DONE_CYCLES` | `3` | monitor | Idle cycles required before closing. |
| `PC_WAHA_ENABLED` | unset | WAHA notifier | Set `1` to enable private WhatsApp group/channel notifications. If `PC_WAHA_CHAT_ID` is not configured, notifications are skipped safely. |
| `PC_WAHA_BASE_URL` | `http://127.0.0.1:3000` | WAHA notifier | Base URL for the self-hosted WAHA HTTP API. |
| `PC_WAHA_SESSION` | `default` | WAHA notifier | WAHA session name to use when sending messages. |
| `PC_WAHA_CHAT_ID` | unset | WAHA notifier | Destination WhatsApp group/channel chat id for automated “what is new” notifications. This environment variable is the only destination source; group ids usually end in `@g.us`. |
| `PC_WAHA_API_KEY` | unset | WAHA notifier | Optional WAHA `X-Api-Key` value when the WAHA server requires it. |
| `PC_WAHA_NOTIFY_EVENTS` | `info,start,done,failed,timeout,resume,update,new,none` | WAHA notifier | Comma-separated event names to send. `new` = rich “nueva oportunidad” messages, `none` = “sin nuevas entradas” status. Use `all` to send every supported event. |
| `PC_WAHA_STRICT` | `0` | WAHA notifier | Set `1` only if notification failures should fail the notifier command. Worker calls still ignore notifier failures. |
| `PC_WAHA_SOURCE` | `Panamá Compra` | new-record notifier | Source label shown as `📌 Fuente:` in the rich opportunity / “sin nuevas entradas” messages. |
| keyword filter | `data/config/waha_keywords.txt` | new-record notifier | Optional, one keyword per line. When present only matching new records are announced; matched keywords appear in `🔎 Coincidencia`. |
| notify baseline | `data/config/waha_notify_initialized` | new-record notifier | Marker written on first run so the existing archive is not announced as “new”. Delete it to re-baseline. |
| saved WAHA message | `data/config/waha_message.txt` | WAHA notifier | Optional reusable message body saved by `pc_waha_notify.py --save-message`; used on later notifications when no one-off message is passed. |

The detail limit can also be passed positionally: `./pc_request_run_all.sh 5`. The native and web monitors include buttons to request a run immediately and to save the WhatsApp group/channel destination that receives automated “what is new” messages for current and future runs.

#### Native monitor layout

The native Tk monitor is organized top-to-bottom into clear sections:

1. **Run controls** — a wide `live`/`test` mode selector comes first, followed by the detail/sandbox limit entry and the **Request selected run** button, all on one row.
2. **Live diagnostics** — phase/status/record counters laid out as two label/value column pairs, grouped left-to-right and top-to-bottom (lifecycle → progress → timing → record counters). The label columns stay narrow while the value columns expand, so large counters and long values stay readable; the free-text **Extra** note gets its own full-width row. Placed directly under Run controls so the live run status is visible without scrolling. While the WhatsApp MESSAGING step runs, a `messaging` process pill lights up and the Phase/Step/Item fields track each message being sent.
3. **Settings (editable)** — entry fields pre-filled with the current values; change what you need and leave the rest, then click **Apply & save settings**:
   - Window transparency (`0.30`–`1.00`, default `0.85`; lower it for a more see-through window) — applied live.
   - Auto-close seconds, active refresh seconds, idle refresh seconds — applied live.
   - WhatsApp source label, destination chat id, and keyword filter.
   - Values persist to `data/config/monitor_settings.env` (and the WhatsApp chat id/keywords to their own files), so they survive restarts and are picked up by the notifier.
4. **Record index** — a **type-to-filter box plus a dedicated, self-scrolling list** of every collected record as `NUMERO — description` (newest first), read straight from `data/panamacompra_archive.db`. This replaces the old dropdown, whose popup scroll fought the whole-page scroll and made the long entries impossible to separate; the list now scrolls on its own (its own scrollbar/wheel) and the filter narrows it instantly. The full number/description of the current selection are echoed on a wide line; **Open record folder** (or double-click a row) opens the archived `records/…` folder and **Open in portal** opens the PanamaCompra page. Use **Refresh list** after a new collection. Empty until the collector has run at least once.
5. **Manual script buttons** — grouped by zone (Collector Runners → Updater & Migration → Data Tools → Testing & Validation → Folder Management) in a compact grid. **Hover any button** to see a tooltip explaining exactly what it does before clicking.
6. **Recent worker / current action logs**.

Transparency, refresh cadence and the auto-close countdown can all be changed from the Settings panel without restarting the monitor. The web monitor (`pc_monitor_server.py`) exposes the same record-index selector, backed by the `/api/record-index` endpoint.

### Optional WAHA private WhatsApp group alerts

This project can send short text alerts to a private WhatsApp group through a
self-hosted WAHA server. WAHA exposes `POST /api/sendText` with a JSON body that
includes `session`, `chatId`, and `text`; group chat ids normally end in `@g.us`.
The notifier is off by default and uses only Python's standard library.

Example local configuration:

```bash
export PC_WAHA_ENABLED=1
export PC_WAHA_BASE_URL="http://127.0.0.1:3000"
export PC_WAHA_SESSION="default"
export PC_WAHA_CHAT_ID="120363000000000000@g.us"
# Optional, if your WAHA server is protected:
export PC_WAHA_API_KEY="your-waha-api-key"
```

Test the notifier without running the collector:

```bash
./pc_waha_notify.py --event info --status TEST --message "PanamaCompra WAHA test"
```

Keep this group private and low-volume. WAHA is a WhatsApp Web style automation
bridge, not the official WhatsApp Business Cloud API, so the safest use is a
private alert group controlled by you.

#### Rich “what is new” opportunity messages

In addition to the short operational alerts above (`start`/`done`/`failed`/…),
new opportunities are announced one message per record. By default the run-all
worker sends them in a **dedicated, monitor-visible MESSAGING step** (STEP 4):
after the detail and calendar steps it runs `pc_notify_new_records.py --announce`,
which sends one WhatsApp message per new index entry **one at a time** and
publishes per-message progress to the monitor — `PHASE=MESSAGING`, `Step 4/5`,
`Item i/N`, and a one-line preview of the message in the **Extra** field — so you
can watch each opportunity go out. The detail downloader does not send WhatsApp
messages mid-download; notifications are emitted only after detail and calendar
processing complete for the run. New-record messages use the full 9-field record
template (status, number, description, location, date range, up to 10 items,
link, created time, and downloaded time):

```text
🔔 *Nueva Oportunidad - Panama Compra*

📊 *Estado:* {estado}
🔢 *Número:* {numero}
📝 *Descripción:* {descripcion}
📍 *Ubicación:* {provincia/lugar}
📅 *Rango Fechas:* {fecha_publicacion} al {fecha_cierre}

📦 *Items ({items_count}):*
• Item 1: {descripcion_item} - Qty: {cantidad} - Unit: {unidad}
... [+ {remaining} más]

🔎 *Coincidencia:* {matched_keywords}

🔗 *Enlace:* {link}
🕒 *Creado:* {first_seen}
⬇️ *Descargado:* {detail_saved_at}
```

When a run completes with no new records, the worker sends a single status
message instead (`pc_notify_new_records.py --idle`):

```text
⚪ Sin nuevas entradas

📌 Fuente: Panamá Compra
🕒 Revisión: {checked_at}
📊 Registros revisados: {total_records}
✅ Monitor activo
```

The MESSAGING step also announces **status changes**, **cancellations**, and
**item-only changes**. Status changes are flagged by the index step
(`pending_status_change`); item changes are detected by comparing the current
detail item hash to the last successfully notified snapshot. After any successful
record notification, the script writes a per-record `.ics` export under
`data/calendar_exports/YYYY/MM/{numero}.ics`.

```text
⚠️ *Cambio de Estado - Panama Compra*
📊 *Estado:* [ANTERIOR: {estado_anterior}] ➡️ [ACTUAL: {estado_actual}]
...

🔄 *Actualización de Items - Panama Compra*
📊 *Estado:* {estado} (Sin cambios)
...
```

Behavior notes:

- **One message per changed record, one at a time.** After the detail step, the
  MESSAGING step sends each new opportunity, status/cancellation change, and
  item-only change individually, publishing per-message monitor progress; if
  there is nothing to send it posts the single `⚪ Sin nuevas entradas` status.
- **No backlog flood.** On first use (before any new detail is downloaded)
  `ensure_baseline` marks every existing saved record as already-announced via
  the `notified_at` column and writes `data/config/waha_notify_initialized`, so
  only records saved afterwards are announced.
- **Change snapshot.** New-record announcing is guarded by `notified_at`; after a
  successful send the notifier stores `last_notified_status`,
  `last_notified_items_hash`, and `last_notified_signature` so later status or
  item changes can be detected without re-sending unchanged records.
- **Optional keyword filter.** Put one keyword per line in
  `data/config/waha_keywords.txt`. When present, only records whose
  title/description/entity match a keyword are announced and the matched
  keywords are listed in `🔎 Coincidencia`. When the file is missing or empty,
  every new record is announced and the line reads
  `Sin filtro (todas las entradas)`.
- **Calendar and summary outputs.** After a successful record notification, a
  per-record `.ics` file is exported below `data/calendar_exports/YYYY/MM/`.
  After the MESSAGING step, the worker sends one final run summary with start/end
  time and aggregate archive counts.
- **Never blocks a run.** Any notifier or network failure is caught and logged;
  records whose send failed keep `notified_at` empty and are retried by the
  end-of-run flush (`pc_notify_new_records.py --flush`) or the next run.
- **Config.** Requires `PC_WAHA_ENABLED=1`, a WAHA server (default
  `http://127.0.0.1:3000`) and a destination chat id in `PC_WAHA_CHAT_ID`
  (for example, a group id ending in `@g.us`).

---

## Data and storage

### Folder layout

```text
panamacompra-collector/
├── *.py, *.sh, requirements.txt, README.md   # code (committed)
├── data/                                      # runtime data (gitignored)
│   ├── panamacompra_archive.db                # SQLite database
│   ├── panamacompra_index.csv                 # append-only "first seen" log
│   ├── config/                                # monitor_settings.env, waha_chat_id.txt, waha_keywords.txt
│   ├── index/                                 # lightweight per-day index JSON
│   ├── calendar/                              # YY-MM-DD timestamped .ics import packages
│   ├── logs/                                  # worker / monitor / webhook logs + run_all_progress.env
│   └── queue/                                 # run_all_requested.flag
└── records/                                   # archive (gitignored)
    └── YY-MM-DD/
        └── [<finish>]-[<numero>]-[<desc>]/     # created as NUMERO, then renamed (see Record folder naming)
            ├── NUMERO.json                     # index record
            ├── NUMERO.detail.json              # detail metadata
            ├── NUMERO.detail.html              # full page HTML
            ├── NUMERO.detail.txt               # visible text
            ├── NUMERO.calendar.ics             # importable calendar event
            └── tables/                         # one detail-page section -> three files:
                ├── NUMERO.table.<section>.001.json         # clean: headers/rows/key_values/links
                ├── NUMERO.table.<section>.001.raw.json     # raw rows
                └── NUMERO.table.<section>.001.raw_wL.json  # raw rows with links
```

`<section>` is a short identifier derived from the detail-page section heading
(e.g. `informacion-general`, `contacto-unidad-compra`, `items-cotizacion`). The
per-table index — section, identifier and the three filenames — is also listed in
`detail.json` under `tables`. Timestamped files under `data/calendar/YY-MM-DD/` hold small import packages for calendar apps. The normal worker exports only events from records written in that run, so you can import each package once without re-importing the entire archive. To auto-open/import generated packages on a desktop machine, set `PC_CALENDAR_AUTO_IMPORT=1` or provide a custom `PC_CALENDAR_AUTO_IMPORT_CMD`.

> `panamacompra_index.csv` is written once per `NUMERO` at first insert and is **not**
> updated afterwards, so it is a first-seen log, not a mirror of current state. Query
> the SQLite database for the live picture.

### Record folder naming

Folders are created as `NUMERO` during the index scan, then renamed to encode the
key facts once detail data is available:

```text
records/YY-MM-DD/[<finish>]-[<numero>]-[<desc>]/
              e.g. [2022-10-11_12:00]-[2022-0-12-214-12-CL-008498]-[FRS-126-CMPRS-D-CJ-PLSTC]
```

- **`<finish>`** = `YYYY-MM-DD_HH:MM` when proposals stop being accepted: the **end**
  time of the *"Fecha y hora presentación de cotizaciones"* window (24-hour). For older
  records without that field, the delivery (*entrega*) date at `12:00` is used. Empty `[]`
  if no date can be found.
- **`<numero>`** = the PanamaCompra `NUMERO`, unchanged.
- **`<desc>`** = the request description, accent-stripped, uppercased, with **vowels
  removed**, each run of non-alphanumerics collapsed to one `-`, truncated to `PC_DESC_SLUG_MAX` chars.

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
  (the **presentación de cotizaciones** / cierre / límite window in 24-hour — start and
  end on its date; older records fall back to the *Día y Hora de Entrega* window),
  `timezone`, `location` (`(Provincia) - (Dirección de la unidad de compra)`),
  `organizer` (the record's contact), `attendees`, `url_publico` (the record's link),
  `url_interno`, `precio_estimado`, and a blank-line-separated `description`
  (`LINK :`, `DESCR:`, then an `ITEMS:` list). A sibling `NUMERO.calendar.ics` file
  is also written so the event can be imported into a calendar app later.
- **`fields_detected`** — the `Label → value` pairs parsed from the detail text. Both the
  current **V3** portal (tab-separated `Label⇥Value`) and older `Label: value` /
  label-on-its-own-line layouts are supported.

New records get these automatically when first downloaded. A normal run only processes
**new** records — it never re-pulls or rewrites records that were already downloaded,
even after the parsing rules change. Refresh previously-downloaded records **manually**:

```bash
./pc_build_detail_views.py            # dry-run: preview every record
./pc_build_detail_views.py --apply    # rebuild views + .ics from saved files (no browser)
./pc_update_day_folder.py --date <YY-MM-DD> --apply   # re-download a day from the portal
```

Calendar timezone and attendees are configurable with `PC_CALENDAR_TZ` and
`PC_CALENDAR_ATTENDEES`. The JSON calendar view is the source of truth; the
`.calendar.ics` file is a portable review/import copy generated during detail
downloads, day-folder refreshes, and `pc_build_detail_views.py --apply`. In the
ICS export, each event's `ATTENDEE:MAILTO:...` lines sit inside its `VEVENT`,
`DTSTART` / `DTEND` use `TZID=<timezone>;VALUE=DATE-TIME` (e.g.
`DTSTART;TZID=America/Panama;VALUE=DATE-TIME:20260619T100000`), `DTSTAMP` ends
with a trailing `Z`, organizer lines include quoted `CN` and `ROLE` parameters
when available, `LOCATION` is `(Provincia) - (Dirección de la unidad de compra)`,
and `DESCRIPTION` is a `LINK :` line, a `DESCR:` line, then an `ITEMS:` list
(blank-line separated). The record link is also set as the event `URL`, which
Thunderbird renders as a clickable link. (Commas in ICS text are written `\,` per
the spec and display unescaped in calendar apps.)

**Calendar import packages.** After each run, `pc_build_calendar.py` (STEP 3)
exports only the new/changed record calendars from that run into timestamped files:

```text
data/calendar/YY-MM-DD/YY-MM-DD_HH-MM_panamacompra_calendar_001.ics
data/calendar/YY-MM-DD/YY-MM-DD_HH-MM_panamacompra_calendar_002.ics
```

The default package size is **10 events per file** (`PC_CALENDAR_PACKAGE_SIZE=10`).
This is intentionally smaller than the old all-in-one calendar because Thunderbird
and other clients can become noisy when a single import contains too many reminders
or editable events. Import the packages produced by each run, then leave old
packages alone. If an imported ICS calendar shows reminders for a calendar you do
not want to edit, right-click that calendar in Thunderbird's calendar/task list,
open **Properties**, and mark it **read-only**.

Manual examples:

```bash
./pc_build_calendar.py                         # package only this run's new/changed records when PC_RUN_STARTED_AT exists
./pc_build_calendar.py --all                   # package every saved record under data/calendar/YY-MM-DD/
PC_CALENDAR_PACKAGE_SIZE=5 ./pc_build_calendar.py --all
./pc_build_calendar.py --all --flat            # write packages directly in data/calendar/
./pc_build_calendar.py --all --legacy-combined # also write data/calendar/panamacompra.ics
```

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

With `--apply`, Playwright is required. If the script was launched with a Python
environment that does not have Playwright, it first checks this checkout's
`.venv/bin/python`; when that interpreter has Playwright, the updater
automatically re-runs itself with the project virtualenv. If neither Python can
import Playwright, the error message prints the current interpreter, the checked
`.venv` path, and repair commands (`./update_local_copy.sh` or `source
.venv/bin/activate && python -m pip install -r requirements.txt`).

> Re-fetching uses each record's stored `link`. If the listing URLs may have changed,
> run an index scan first so links and `last_seen` are refreshed. The index scan
> also checks for an existing `NUMERO.json` anywhere under `records/YY-MM-DD/`,
> including renamed `[finish]-[numero]-[desc]` folders, before creating a new
> plain `NUMERO` folder. This prevents duplicate archives when the original
> `NUMERO/` leaf was already renamed after detail download.

### Testing zone

When a run finds **no new opportunities**, the pipeline normally does nothing, so a
code change cannot be observed. The testing zone fills that gap: it re-runs the most
recent **N records (default 5)** through the full pipeline in an **isolated sandbox**,
leaving the real archive and DB untouched, so you can see how the current code renders
them.

- Writes only to `records_test/latest_5/`, a throwaway in-memory DB, and test ICS packages under `records_test/calendar/YY-MM-DD/` — diff these against the real outputs.
- The run is published to the monitor as **`MODE=TEST`** (the monitor's *Mode* field
  shows `LIVE` vs `TEST`), with a *Test records* count, so it is clearly distinct from
  new (live) records.
- The run-all worker does **not** launch it automatically by default. Set
  `PC_TEST_ZONE_AUTORUN=1` to have it run as **STEP 5** when a run had no new records
  to process; `PC_TEST_ZONE_LIMIT` then controls how many records are re-run (`0`
  disables it). With the default `PC_TEST_ZONE_AUTORUN=0` the autostart never runs it.

Run it manually any time:

```bash
./pc_test_zone.py                 # list the last 5, then ask
./pc_test_zone.py --limit 5 --apply
```

Each run starts from a clean sandbox (the previous `records_test/` is cleared), and
the full pipeline runs — live re-download, section-table split, views, and timestamped test calendar packages — so browser extraction and parsing changes are both exercised.

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
| `notified_at` | Timestamp of the first WAHA “nueva oportunidad” WhatsApp message for this record (empty = not yet announced). |
| `last_notified_status`, `last_notified_items_hash`, `last_notified_signature` | Last successfully notified record snapshot, used to suppress unchanged records and detect status/item updates. |
| `last_calendar_export_path` | Per-record `.ics` path written after a successful WhatsApp notification. |

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

changedetection.io is used **only** to detect changes and fire the webhook — it
must not download detail pages itself. You can run it (and the WhatsApp/WAHA
server and the webhook listener) from the committed [Docker Compose stack](#run-the-stack-with-docker-compose),
or wire up your own changedetection.io instance and run the listener on the host.

### Run the stack with Docker Compose

The repo ships a `docker-compose.yml` that brings the container-friendly pieces
into one reproducible stack:

| Service | Image | Purpose |
|---------|-------|---------|
| `changedetection` | `dgtlmoon/changedetection.io` | Watches the PanamaCompra table and fires the webhook. UI on `http://localhost:5000`. |
| `sockpuppetbrowser` | `dgtlmoon/sockpuppetbrowser` | Headless Chromium that renders the JavaScript watch page for changedetection. |
| `waha` | `devlikeapro/waha` | Self-hosted WhatsApp HTTP API for the alerts. API on `http://localhost:3000` (scan the QR once to log in). |
| `webhook` | built from `docker/Dockerfile.webhook` | `webhook_listener.py` in **enqueue-only** mode on port `8765`. |

```bash
cp .env.example .env            # set CHANGEDETECTION_BASE_URL, ports, WAHA_API_KEY
printf 'YOUR_SECRET_TOKEN' > .webhook_token   # shared webhook path token (gitignored)
docker compose up -d            # changedetection + browser + waha + webhook
```

**Why the webhook container only “enqueues”.** The real collector (Playwright
Firefox writing to the host `./records` and `./data`) runs on the **host**, not in
a container. So the `webhook` container runs with `PC_WEBHOOK_ENQUEUE_ONLY=1`: on a
valid request it only writes `data/queue/run_all_requested.flag` into the
bind-mounted checkout. A tiny **host** runner then performs the actual collection:

```bash
# On the host checkout, run the watcher (or install it as a user service below):
./pc_run_all_flag_watcher.sh
```

In the changedetection.io UI, set the watch **notification URL** to reach the
webhook container on the compose network (no `host.docker.internal` needed):

```text
json://webhook:8765/panamacompra/YOUR_SECRET_TOKEN?method=POST
```

Run the host runner as a user service so requests are always picked up:

```bash
cat > ~/.config/systemd/user/panamacompra-runner.service <<'EOF'
[Unit]
Description=PanamaCompra run-all flag watcher (host collector launcher)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/Apps/panamacompra-collector
ExecStart=%h/Apps/panamacompra-collector/pc_run_all_flag_watcher.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now panamacompra-runner.service
```

> Prefer the all-host setup instead? Skip the `webhook` compose service and run
> `webhook_listener.py` on the host (see [Persistent webhook listener with
> systemd](#persistent-webhook-listener-with-systemd)). In that mode the listener
> runs `run_collector.sh` itself and no flag watcher is needed.

### Watch and webhook configuration

**Watch URL**

```text
https://www.panamacompra.gob.pa/Inicio/#/cotizaciones-en-linea/cotizaciones-en-linea
```

**Watch configuration**

- Fetch method: Playwright / Firefox (JavaScript mode)
- JS actions: close popup → click *Programadas* → set 50 rows/page → crawl pages →
  click *Abiertas* → set 50 rows/page → crawl pages → output stable text keyed by `NUMERO`
- CSS filter: `#pc-monitor-output`
- Do **not** include visual row number, page number, or generated timestamps (they cause false alerts)

**Webhook**

changedetection notifies the local listener. Create the token first:

```bash
printf 'YOUR_SECRET_TOKEN' > .webhook_token
source .venv/bin/activate
python webhook_listener.py
```

The listener accepts requests at `/panamacompra/<TOKEN>`:

```text
Local:        http://127.0.0.1:8765/panamacompra/YOUR_TOKEN
From Docker:  http://host.docker.internal:8765/panamacompra/YOUR_TOKEN
```

On a valid request it runs `run_collector.sh`, which requests the full sequence. It
does not start a browser session directly.

### Webhook reachability diagnostic

If changedetection.io logs `Connection refused to host.docker.internal:8765`, the
webhook URL format and token have not been tested yet — the Linux host was not
accepting the TCP connection. A bad token would reach the listener and return
`403 Forbidden`; `Connection refused` normally means the listener is stopped, bound
to the wrong interface, or unreachable from Docker.

Run the bundled diagnostic from the checkout:

```bash
./pc_webhook_diagnostic.sh
```

The script ensures `.webhook_token` exists with mode `600`, restarts
`webhook_listener.py` on `PC_WEBHOOK_HOST` / `PC_WEBHOOK_PORT` (defaults
`0.0.0.0:8765`), confirms the port is listening, tests
`http://127.0.0.1:8765/panamacompra/<TOKEN>`, and then attempts the same request
from the detected changedetection.io Docker container using
`host.docker.internal`. Token values are masked in diagnostic log output. A healthy
local and Docker test returns `HTTP 202` with `Collector triggered`.

If the local test works but the Docker test cannot resolve or reach
`host.docker.internal`, add this to the changedetection.io service in its
`docker-compose.yml`, then restart that stack:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

As an alternative on the local LAN, point changedetection.io at the workstation's
LAN address instead of Docker's host alias, for example:

```text
json://192.168.10.20:8765/panamacompra/YOUR_TOKEN?method=POST&format=html&overflow=upstream
```

Before using the LAN URL, test it from inside the changedetection.io container.

### Persistent webhook listener with systemd

For regular use, run the listener as a user service so it survives terminal
closures and restarts automatically:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/panamacompra-webhook.service <<'EOF'
[Unit]
Description=PanamaCompra webhook listener
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/Apps/panamacompra-collector
Environment=PC_WEBHOOK_HOST=0.0.0.0
Environment=PC_WEBHOOK_PORT=8765
ExecStart=%h/Apps/panamacompra-collector/.venv/bin/python %h/Apps/panamacompra-collector/webhook_listener.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now panamacompra-webhook.service
systemctl --user status panamacompra-webhook.service --no-pager
```

Allow the service to continue after logout when needed:

```bash
sudo loginctl enable-linger "$USER"
```

Check or restart the service with:

```bash
journalctl --user -u panamacompra-webhook.service -n 80 --no-pager
systemctl --user restart panamacompra-webhook.service
```

---

## Monitoring and logs

The default monitor is now the native Tk window (`pc_monitor_tk.py`). Run
`./pc_open_monitor.sh` or launch the **PanamaCompra Update + Monitor** desktop/application-menu shortcut installed by `./update_local_copy.sh`. The shortcut opens a separate updater loader (`pc_update_loader.py`) first: that window appears on top with a step-based progress bar (steps 1–11 of `update_local_copy.sh`) and streams the update output, and only **after** the update attempt finishes does the normal monitor/timer open. If the update fails, the loader keeps the error visible and still starts the monitor so you can inspect logs and controls.
The monitor opens a lightweight desktop window without starting Firefox, a browser engine, or a web server. It shows the real progress bar, current step/item,
diagnostics counters, process status, recent log tails, run-mode/limit selectors for the live collector or test-zone script, and manual controls grouped into **Runners**, **Tests**, **Updater / Migration**, and **Settings** zones. The runner zone includes stop controls for active collector processes. The test-zone button opens the `records_test/` parent folder after the test command finishes, so the generated sandbox output is immediately visible. The monitor body is scrollable with the scrollbar **and the mouse wheel** (Linux/X11 wheel events are handled, not only Windows/macOS), so smaller Linux Mint screens can reach the logs and manual actions. Each manual button has an adjacent comment explaining what it does before the user clicks it, and command output is appended to `data/logs/manual_actions.log`. The manually-opened monitor **stays open** for manual work and does not auto-close by default (`PC_MONITOR_TK_AUTO_CLOSE_SECONDS=0`); if a positive auto-close value is configured, it is honored only for completed live runs, not for test-zone or manual desktop actions.

For a tiny always-on-top countdown timer showing when the next live run is due, run:
```bash
python pc_next_run_timer.py
```
This mini-monitor counts down to the next run, anchored to the last live run's start time (read from `data/logs/run_all_progress.env`) plus `PC_NEXT_RUN_INTERVAL_MINUTES`, so it tracks the real cadence and rolls forward when a run is overdue; before any run is recorded it falls back to clock boundaries (:00 and :30 past each hour by default). `pc_open_monitor.sh` starts it automatically with the Tk monitor unless `PC_NEXT_RUN_TIMER=0` is set. When a live run starts, the timer window withdraws/closes from view; when the live run finishes, it reappears and starts counting down again. Its default position is centered horizontally and about 30 px below the top of the screen.

The browser monitor remains available for hosts where Tk is not installed or where a
remote browser dashboard is preferred: `PC_MONITOR_MODE=web ./pc_open_monitor.sh`,
then open `http://127.0.0.1:8766/`; the web monitor mirrors the Tk monitor zones, stop button, test-sandbox folder opening, and live-run-only auto-close behavior. The terminal UI is still available with
`PC_MONITOR_MODE=terminal ./pc_open_monitor.sh`. If no GUI can be opened, use
`./pc_run_all_status.sh` or `./pc_follow_run_all.sh` from any terminal.

Key logs under `data/logs/`:

| Log | Contents |
|-----|----------|
| `run_all_current.log` | The run currently in progress. |
| `run_all_history.log` | Appended history of completed runs. |
| `run_all_worker.log` | Worker lifecycle events. |
| `run_all_requests.log` | Run-all requests. |
| `update_before_run_*.log` | Pre-run Git/dependency update output for each worker iteration. |
| `collector_triggered.log` | Webhook → collector triggers. |
| `webhook_listener.log` | Webhook listener activity. |
| `monitor_open.log` | Attempts to start/open the web monitor, terminal monitor, or fallback. |
| `monitor_tk.log` | Background native Tk monitor output/errors. |
| `next_run_timer.log` | Background tiny next-run timer output/errors. |
| `update_loader_*.log` | Separate updater loader output for desktop/manual update sessions. |
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
source .venv/bin/activate
python pc_monitor_tk.py --snapshot
PC_MONITOR_MODE=web ./pc_open_monitor.sh  # optional browser monitor
./pc_run_all_status.sh
./pc_follow_run_all.sh
```

**Webhook does not trigger the collector** — first distinguish reachability from token
validation. `Connection refused to host.docker.internal:8765` means changedetection.io
could not connect to the listener at all; a wrong token reaches the listener and returns
`403 Forbidden`. Run the diagnostic/fix helper, then check logs:

```bash
./pc_webhook_diagnostic.sh
tail -80 data/logs/webhook_listener.log
tail -80 data/logs/collector_triggered.log
tail -80 data/logs/run_all_requests.log
```

If local curl returns `202` but the Docker test fails, add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the changedetection.io
compose service or use the workstation LAN IP in the notification URL. If the error
started right after the manual **Update + Monitor** launcher, run
`./update_local_copy.sh` again after this version is installed; it now restores the
webhook listener after stopping it for the update.

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
PYTHON_BIN="${PYTHON_BIN:-python3}"
[ -f .venv/bin/activate ] && source .venv/bin/activate && PYTHON_BIN=python
"$PYTHON_BIN" -m py_compile pc_common.py pc_index_collector.py pc_detail_downloader.py \
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
