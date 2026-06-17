# PanamaCompra Collector

Local monitoring and archival system for PanamaCompra opportunities.

This project watches the PanamaCompra **Cotizaciones en Línea** section, detects new or changed opportunity rows, stores each opportunity by its stable `NUMERO`, and optionally downloads the full detail page into a local archive.

The project is designed for a low-resource Linux workstation, so the workflow avoids parallel browser sessions and runs the index scan and detail download sequentially.

---

## 1. Project goal

The main goal is to monitor public opportunities from PanamaCompra and keep a structured local archive.

The system collects:

- Opportunity number / `NUMERO`
- Status group, such as `Programadas` or `Abiertas`
- Status
- Description
- Entity
- Dependency
- Date shown in the table
- Modality
- Detail URL
- Full detail page HTML
- Full detail page visible text
- JSON metadata
- JSON files for tables found inside detail pages
- Estimated finish or closing date when detectable from the detail page text

The stable unique identifier is the PanamaCompra `NUMERO`, for example:

```text
2026-0-07-02-02-CL-058693

The system intentionally ignores visual row number and page number because those change whenever PanamaCompra inserts new opportunities at the top of the table.

2. Final architecture
changedetection.io
  detects change in PanamaCompra table
        ↓
webhook_listener.py
  receives webhook from changedetection.io
        ↓
run_collector.sh
  requests full collector sequence
        ↓
pc_request_run_all.sh
  creates run_all_requested.flag
        ↓
pc_run_all_worker.sh
  single locked worker
        ↓
STEP 1: pc_index_collector.py
  scans Programadas + Abiertas + pagination
        ↓
STEP 2: pc_detail_downloader.py
  downloads pending detail pages
        ↓
records/YY-MM-DD/NUMERO/
  NUMERO.json
  NUMERO.detail.json
  NUMERO.detail.html
  NUMERO.detail.txt
  tables/*.json

The current active architecture is run-all only.

The older queue system is deprecated and should not be used.

3. Important design decisions
3.1 Use NUMERO as the unique key

The system treats NUMERO as the primary key.

Do not use:

row number
page number
changedetection history position
visual order

Those values are unstable.

3.2 Split index and detail work

The system separates the workflow into two internal phases:

Index scan
Opens the PanamaCompra table.
Selects Programadas.
Sets rows per page to 50.
Crawls all Programadas pages.
Selects Abiertas.
Sets rows per page to 50.
Crawls all Abiertas pages.
Saves lightweight index JSON and database records.
Detail download
Reads pending records from SQLite.
Visits each detail URL.
Saves HTML, text, metadata JSON, and table JSON files.
Marks the detail as saved.

This split protects low-resource hardware from doing too much browser work at once.

3.3 One browser process at a time

The run-all worker uses a lock so that only one active sequence runs at a time.

If changedetection triggers again while the worker is running:

new request flag is created
current process continues
after finishing, worker repeats one more full sequence

This prevents duplicate Playwright/Chromium sessions.

3.4 Immutable local archive

Existing JSON/detail files are not overwritten.

If a folder is already complete, the system skips unnecessary reprocessing.

4. Main URLs

Base table URL:

https://www.panamacompra.gob.pa/Inicio/#/cotizaciones-en-linea/cotizaciones-en-linea

Example detail URL types:

https://www.panamacompra.gob.pa/Inicio/#/solicitud-de-cotizacion/...
https://www.panamacompra.gob.pa/Inicio/#/pliego-de-cargos/...

The system supports both:

solicitud-de-cotizacion
pliego-de-cargos
5. Active scripts
pc_common.py

Shared Python module.

Contains:

Project paths
Database path
Records path
CSV path
JSON helpers
Filename-safe helpers
Date folder naming
SQLite schema
URL type detection
Finish date guessing
Shared insert/update helpers

Do not run directly.

pc_index_collector.py

Light-to-medium process.

Purpose:

Scan PanamaCompra table/index only.

Actions:

Opens the base PanamaCompra table URL.
Closes the popup.
Clicks Programadas.
Sets table rows to 50.
Crawls all Programadas pagination.
Clicks Abiertas.
Sets table rows to 50.
Crawls all Abiertas pagination.
Extracts table rows.
Saves or updates SQLite records.
Writes one immutable index JSON file per NUMERO.

Output example:

records/26-06-17/2026-1-10-01-09-CL-044633/
└── 2026-1-10-01-09-CL-044633.json

Manual run:

cd ~/Apps/panamacompra-collector
source .venv/bin/activate
./pc_index_collector.py
pc_detail_downloader.py

Heavy process.

Purpose:

Download pending detail pages.

Actions:

Reads pending records from SQLite.
Opens each detail URL.
Saves full HTML.
Saves visible text.
Extracts page tables into JSON files.
Creates detail JSON.
Tries to detect finish or closing date from text.
Updates SQLite detail status.

Output example:

records/26-06-17/2026-1-10-01-09-CL-044633/
├── 2026-1-10-01-09-CL-044633.detail.json
├── 2026-1-10-01-09-CL-044633.detail.html
├── 2026-1-10-01-09-CL-044633.detail.txt
└── tables/
    ├── 2026-1-10-01-09-CL-044633.table_001.json
    └── 2026-1-10-01-09-CL-044633.table_002.json

Manual small run:

cd ~/Apps/panamacompra-collector
source .venv/bin/activate
PC_DETAIL_LIMIT=5 ./pc_detail_downloader.py
pc_request_run_all.sh

Safe entry point for the full workflow.

Purpose:

Request a full sequential run.

It creates:

data/queue/run_all_requested.flag

Then starts pc_run_all_worker.sh if no worker is already active.

Manual background run with only 5 detail pages:

./pc_request_run_all.sh 5

Manual background run with all pending detail pages:

./pc_request_run_all.sh
pc_run_all_worker.sh

Main sequential worker.

Purpose:

Run index first, then detail.

Actions:

Checks lock.
Reads request flag.
Runs pc_index_collector.py.
If index succeeds, runs pc_detail_downloader.py.
Writes current log and history log.
Updates progress file for the monitor.
Repeats if another request arrived while it was running.

Important files:

data/logs/run_all_current.log
data/logs/run_all_history.log
data/logs/run_all_worker.log
data/logs/run_all_progress.env
run_collector.sh

Webhook entry point.

Purpose:

Called by changedetection.io through webhook_listener.py.

Actions:

Receives changedetection trigger.
Requests the full sequence using pc_request_run_all.sh.
Does not run the collector directly.
Does not start parallel browser sessions.

This file is the bridge between changedetection and the local worker.

webhook_listener.py

Local HTTP listener.

Purpose:

Receive webhook calls from changedetection.io.

Typical local endpoint:

http://127.0.0.1:8765/panamacompra/YOUR_TOKEN

From inside Docker, changedetection usually calls:

http://host.docker.internal:8765/panamacompra/YOUR_TOKEN

The listener validates the token and then runs:

run_collector.sh
pc_monitor_window.sh

Terminal progress monitor.

Purpose:

Show live progress while the run-all process is active.

Shows:

Phase
Status
Progress bar
Elapsed time
Detail limit
Current action
Active process status
Recent worker log
Current run log

It closes automatically when:

pc_run_all_worker.sh is not running
pc_index_collector.py is not running
pc_detail_downloader.py is not running
run_all_requested.flag does not exist
pc_open_monitor.sh

Terminal launcher.

Purpose:

Open a graphical terminal running pc_monitor_window.sh.

It tries common Linux terminal emulators:

gnome-terminal
mate-terminal
xfce4-terminal
x-terminal-emulator
xterm

If no graphical display is available, it logs the issue and exits safely.

pc_run_all_status.sh

Manual status command.

Purpose:

Show current process state.

Run:

./pc_run_all_status.sh

It reports:

Worker status
Index collector status
Detail downloader status
Pending request flag
Related process list
Recent request log
Recent worker log
Current run log
pc_stop_run_all.sh

Emergency stop script.

Purpose:

Stop active run-all/index/detail process if stuck.

Use only if the browser process is frozen or the log has not moved for a long time.

Run:

./pc_stop_run_all.sh
migrate_previous_records.py

Migration script.

Purpose:

Move previous saved records from old flat structure into the new dated structure.

Old structure:

records/NUMERO/
├── row.json
├── detail.html
├── detail.txt
└── metadata.json

New structure:

records/YY-MM-DD/NUMERO/
├── NUMERO.json
├── NUMERO.detail.json
├── NUMERO.detail.html
└── NUMERO.detail.txt

The migration script copies files and does not delete old records.

migrate_previous_records.sh

Simple launcher for the migration script.

Run:

./migrate_previous_records.sh
6. Deprecated scripts

These were part of an older queue-based design and should not be used in the final workflow:

pc_enqueue.sh
pc_queue_worker.sh
pc_queue_status.sh
pc_requeue_running.sh
pc_run_index.sh
pc_run_detail.sh
pc_run_sequence.sh
panamacompra_collector.py

They may exist in a backup folder, but they are not part of the active architecture.

7. Storage structure

Main local project folder:

~/Apps/panamacompra-collector/

Typical structure:

panamacompra-collector/
├── README.md
├── pc_common.py
├── pc_index_collector.py
├── pc_detail_downloader.py
├── pc_request_run_all.sh
├── pc_run_all_worker.sh
├── pc_run_all_status.sh
├── pc_stop_run_all.sh
├── pc_monitor_window.sh
├── pc_open_monitor.sh
├── run_collector.sh
├── webhook_listener.py
├── migrate_previous_records.py
├── migrate_previous_records.sh
├── data/
│   ├── panamacompra_archive.db
│   ├── panamacompra_index.csv
│   ├── logs/
│   └── queue/
└── records/
    └── YY-MM-DD/
        └── NUMERO/
            ├── NUMERO.json
            ├── NUMERO.detail.json
            ├── NUMERO.detail.html
            ├── NUMERO.detail.txt
            └── tables/
                └── NUMERO.table_001.json
8. Database

SQLite database:

data/panamacompra_archive.db

Main table:

opportunities

Important fields:

numero
grupo
tipo_url
estado
descripcion
short_description
entidad
dependencia
fecha
modalidad
link
first_seen
last_seen
date_folder
record_folder
index_json_path
detail_status
detail_attempts
detail_saved_at
detail_json_path
finish_date_guess

numero is the primary key.

Check total records:

sqlite3 data/panamacompra_archive.db \
"SELECT COUNT(*) FROM opportunities;"

Check pending details:

sqlite3 data/panamacompra_archive.db \
"SELECT COUNT(*) FROM opportunities WHERE detail_status != 'saved';"

Show latest records:

sqlite3 data/panamacompra_archive.db \
"SELECT numero, grupo, estado, detail_status, finish_date_guess FROM opportunities ORDER BY last_seen DESC LIMIT 20;"
9. Dependencies
9.1 Operating system

Recommended:

Linux Mint / Ubuntu / Debian-based Linux

The project was designed around a low-resource Linux Mint machine.

9.2 Python

Recommended:

Python 3.10+

Current environment used:

Python 3.12 virtual environment

Python standard library modules used:

csv
json
os
re
sqlite3
subprocess
datetime
pathlib
http.server
9.3 Python packages

Required external Python package:

playwright

Install inside the virtual environment:

cd ~/Apps/panamacompra-collector
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install playwright

This project uses the system Chromium executable when available:

/usr/bin/chromium
/usr/bin/google-chrome
/usr/bin/chromium-browser
9.4 Browser

Recommended:

Chromium

Install:

sudo apt update
sudo apt install -y chromium

The collectors launch Chromium in headless mode.

9.5 Shell tools

Used by shell scripts:

bash
flock
timeout
pgrep
pkill
tail
sed
grep
find
date
tee

Install common tools:

sudo apt install -y util-linux coreutils procps findutils grep sed
9.6 SQLite command-line tool

Optional but recommended:

sudo apt install -y sqlite3

Python itself uses the built-in sqlite3 module, but the command-line sqlite3 tool is useful for manual inspection.

9.7 Docker / changedetection.io

The project assumes changedetection.io is running separately, typically using Docker.

Expected services:

changedetection
sockpuppetbrowser

changedetection.io is used only to detect changes and send the webhook.

It should not download all detail pages itself.

9.8 Git and GitHub CLI

Recommended for GitHub repository management:

sudo apt install -y git

GitHub CLI:

gh

Used for:

gh auth login
gh repo create
gh repo view
git push
10. changedetection.io configuration
Watch URL

Use:

https://www.panamacompra.gob.pa/Inicio/#/cotizaciones-en-linea/cotizaciones-en-linea
Fetch method

Use Playwright/Chromium JavaScript mode.

JavaScript actions

The changedetection watch should:

Close popup.
Click Programadas.
Set rows per page to 50.
Crawl pagination until no Next button.
Click Abiertas.
Set rows per page to 50.
Crawl pagination until no Next button.
Output stable text keyed by NUMERO.
CSS filter

Recommended:

#pc-monitor-output
Avoid unstable fields

Do not include:

visual row number
page number
generated timestamp

Those create false alerts.

11. Webhook setup

changedetection.io sends a notification to the local webhook listener.

The listener runs:

run_collector.sh

The active flow is:

changedetection notification
→ webhook_listener.py
→ run_collector.sh
→ pc_request_run_all.sh
→ pc_run_all_worker.sh
12. Manual operation
Run a small full sequence
cd ~/Apps/panamacompra-collector
./pc_request_run_all.sh 5
./pc_open_monitor.sh

This runs:

index scan
then 5 pending detail downloads
Run all pending details
cd ~/Apps/panamacompra-collector
./pc_request_run_all.sh
./pc_open_monitor.sh
Check status
cd ~/Apps/panamacompra-collector
./pc_run_all_status.sh
Follow logs in terminal
cd ~/Apps/panamacompra-collector
./pc_follow_run_all.sh
Emergency stop
cd ~/Apps/panamacompra-collector
./pc_stop_run_all.sh
13. Monitor behavior

The progress monitor displays:

Phase
Status
Progress bar
Elapsed time
Detail limit
Current action
Recent worker log
Current run log

The monitor closes automatically when no active process remains.

The progress percentage is estimated while a browser step is running, because the Python collectors print final summaries at the end of each stage.

The real completion state is determined by process status and worker exit.

14. Logs

Important logs:

data/logs/run_all_current.log
data/logs/run_all_history.log
data/logs/run_all_worker.log
data/logs/run_all_requests.log
data/logs/collector_triggered.log
data/logs/webhook_listener.log
data/logs/monitor_open.log

Inspect current run:

tail -120 data/logs/run_all_current.log

Inspect worker history:

tail -120 data/logs/run_all_worker.log

Inspect webhook triggers:

tail -120 data/logs/collector_triggered.log
tail -120 data/logs/webhook_listener.log
15. GitHub repository policy

This repository should contain code only.

Do not commit:

records/
data/
.venv/
.webhook_token
SQLite databases
logs
downloaded HTML
downloaded TXT

These are excluded in .gitignore.

Recommended .gitignore:

.venv/
__pycache__/
*.pyc

data/
records/
logs/
*.log
*.db
*.sqlite
*.sqlite3
*.db-wal
*.db-shm

.webhook_token
*.token
*.secret
.env

*.html
*.txt

*.bak*
deprecated_queue_scripts/
16. Common troubleshooting
Monitor stays open

Check real processes:

pgrep -af "pc_run_all_worker|pc_index_collector|pc_detail_downloader|timeout .*pc_" || true

If one exists, the monitor is correctly staying open.

If no process exists, check:

./pc_run_all_status.sh
Index collector runs too long

Check current log:

tail -120 data/logs/run_all_current.log

If frozen for too long:

./pc_stop_run_all.sh
Detail downloader has nothing to do

If the log says:

No pending detail rows.

That means all database records have detail_status = saved.

Check:

sqlite3 data/panamacompra_archive.db \
"SELECT COUNT(*) FROM opportunities WHERE detail_status != 'saved';"
changedetection trigger does not run collector

Check:

tail -80 data/logs/webhook_listener.log
tail -80 data/logs/collector_triggered.log
tail -80 data/logs/run_all_requests.log
Git accidentally shows data files

Run:

git status --short

If records/, data/, .venv/, or .webhook_token appear, fix .gitignore before committing.

17. Security notes

Do not publish:

.webhook_token
local database
downloaded records
logs with local paths
private operational notes

Use a private GitHub repository unless the project has been cleaned for public release.

18. Git workflow

Initial setup:

git init
git branch -M main
git add .
git commit -m "Initial PanamaCompra collector scripts"

Push to GitHub:

gh repo create panamacompra-collector --private --source=. --remote=origin --push

Update after changes:

git status --short
git add .
git commit -m "Update collector scripts"
git push

Clone on another machine:

gh auth login
gh repo clone panamacompra-collector
19. Recommended operating mode

For stable low-resource operation:

changedetection.io runs frequently
changedetection triggers webhook only when content changes
webhook requests full run
run-all worker runs one sequence at a time
index scan runs first
detail downloader runs second
monitor shows progress
monitor closes automatically

Manual safe command:

./pc_request_run_all.sh 5
./pc_open_monitor.sh

Full command:

./pc_request_run_all.sh
./pc_open_monitor.sh
20. Current stable status checklist

The system is healthy when:

pc_common.py compiles
pc_index_collector.py compiles
pc_detail_downloader.py compiles
webhook_listener.py compiles
pc_run_all_status.sh shows no stuck process
changedetection webhook creates run-all request
index scan reports zero duplicate NUMERO
detail downloader reports no pending detail rows after completion
records are stored under records/YY-MM-DD/NUMERO/
existing JSON files are skipped, not overwritten

