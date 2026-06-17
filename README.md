# PanamaCompra Collector

Local PanamaCompra monitoring and archival tools.

## Workflow

changedetection.io detects changes and calls a webhook. The webhook requests a sequential run:

1. `pc_index_collector.py` scans Programadas and Abiertas tables.
2. `pc_detail_downloader.py` downloads pending detail pages.
3. Records are stored locally under `records/YY-MM-DD/NUMERO/`.

## Main scripts

- `pc_common.py` shared database and helper functions.
- `pc_index_collector.py` table/index collector.
- `pc_detail_downloader.py` detail-page downloader.
- `pc_request_run_all.sh` requests the full sequence.
- `pc_run_all_worker.sh` runs index then detail with a lock.
- `pc_monitor_window.sh` terminal progress monitor.
- `webhook_listener.py` receives changedetection webhook.
- `run_collector.sh` entry point called by changedetection.

## Local data not committed

The repository intentionally excludes:

- `records/`
- `data/`
- `.venv/`
- `.webhook_token`
- SQLite databases
- logs
- downloaded HTML/text files
