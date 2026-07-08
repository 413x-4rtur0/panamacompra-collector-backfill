# Monitor & Script Relationships

File-level dependency reference: who calls what, where data flows, and where
logic is duplicated. Verified against the code in July 2026 (citations are
file:line or function names).

## Monitors

| File | Kind | Started by | Reads | Writes |
|---|---|---|---|---|
| `src/monitor/000-open-monitor.sh` | chooser | `pcc monitor` (`bin/pcc:104`), `110a-request-run.sh:35` | `PC_MONITOR_MODE` (default `tk`, `:22`) | starts 001a (`:92`) or 001b (`:114`) |
| `src/monitor/001a-monitor-tk.py` | native Tk dashboard, 5 tabs | 000-open-monitor.sh | progress/summary env files, archive DB, settings env | `monitor_settings.env`, chat/keyword/format files; spawns 070/110a/110b/120b (`:1273-1286`) |
| `src/monitor/001b-monitor-web.py` | web dashboard + JSON API (~25 endpoints, `:1625-1888`), 6 tabs | 000-open-monitor.sh, `src/tools/130-open-web-app.sh` | same as 001a | same as 001a via POST endpoints |
| `src/monitor/001c-monitor-terminal.sh` | terminal watcher | `pcc watch` (`bin/pcc:143`) | progress/queue/logs | — |
| `src/monitor/002-next-run-timer.py` | countdown widget | desktop launcher / manual | progress + last-summary env, DB | — |
| `src/monitor/003-update-loader.py` | update-then-open-monitor loader | worker (`100-run-worker.sh:68`), `050-watch-queue-flag.sh:57` | — | runs `update-local-copy.sh`, then 000-open-monitor.sh |

Both GUI monitors are **peers**: they render the same settings files and the
same data feeds. Neither talks to the other.

### Data feeds consumed by all monitors

- `data/logs/run_all_progress.env` — written by `write_progress`
  (`100-run-worker.sh:116-166`) and `common.write_run_progress`
  (`src/common.py:85`).
- `data/logs/run_all_last_summary.env` — per-stage seconds + `INDEX_SOURCE`
  (`100-run-worker.sh:608-621`).
- `data/panamacompra_archive.db` — via `common.init_db`.
- `data/queue/*.flag` — run/update queue state.
- `data/config/monitor_settings.env` + `waha_*.txt` — settings both monitors
  read AND write; also read by the worker and `bin/pcc` (`load_monitor_settings`,
  `bin/pcc:81-88`).

## Call graph (scripts → scripts)

```
bin/pcc
 ├─ start        → src/pipeline/110a-request-run.sh ──┬─ spawns 100-run-worker.sh
 │                                                    └─ opens src/monitor/000-open-monitor.sh
 ├─ stop         → src/pipeline/120a-stop-everything.sh
 ├─ status       → src/pipeline/130a-queue-status.sh
 ├─ watch        → src/monitor/001c-monitor-terminal.sh
 ├─ monitor      → src/monitor/000-open-monitor.sh → 001a / 001b
 ├─ webhook start→ src/webhook/020-start-listener.sh → 010-webhook-listener.py
 ├─ webhook diag → src/webhook/040-diagnose-webhook.sh
 ├─ docker       → src/tools/010-docker-stack.sh (compose stack; writes .webhook_token)
 ├─ kpi          → inline Python heredoc (bin/pcc:190-341) over common.init_db
 ├─ calendar     → src/tools/030-opportunity-calendar.py
 ├─ notify       → src/pipeline/020-notify-whatsapp.py --record N
 ├─ test-whatsapp→ src/notify/010-waha-client.py
 ├─ health       → review-system.sh (root)
 ├─ full-report  → src/tools/140-full-report.py
 ├─ templates    → src/tools/020-record-templates.py
 ├─ format       → src/tools/040-message-formats.py
 ├─ keywords test→ loads 020-notify-whatsapp.py as a module (bin/pcc:436-448)
 ├─ upload       → src/tools/150-upload-github.sh
 ├─ migrate      → src/tools/100-migrate-apps-layout.sh
 ├─ setup        → setup.sh · uninstall → scripts/uninstall.sh
 └─ launcher     → scripts/install-desktop-launcher.sh → scripts/tasks/*.sh

100-run-worker.sh (orchestrator)
 ├─ 000-update-before-run.sh → src/tools/050-maintain-database.py
 ├─ 015-import-index-snapshot.py → common.insert_or_update_index
 ├─ 010-collect-index.py         → common.insert_or_update_index
 ├─ 020-notify-whatsapp.py (--announce / --announce-details / --sync-snapshots)
 ├─ 030-collect-details.py ──(load_script)──▶ 020-notify-whatsapp.py (inline)
 ├─ 040-build-detail-views.py
 ├─ src/tools/020-record-templates.py apply
 ├─ 050-repair-missing-deadlines.py ──(load_script)──▶ src/tools/080-update-day-folder.py
 ├─ 060-build-calendar.py
 ├─ src/notify/010-waha-client.py (notify_waha, :45-55)
 ├─ 070-test-zone.py (opt-in)
 └─ src/monitor/003-update-loader.py (queued update, :60-72)

webhook trigger path
 changedetection ─▶ 010-webhook-listener.py
   ├─ host mode:      060-run-collector.sh → 110a-request-run.sh (AUTO)
   └─ enqueue mode:   run_all_requested.flag ◀─ polled by 050-watch-queue-flag.sh → 110a (AUTO)
```

Python-module reuse: every `src/**/*.py` inserts `src/` into `sys.path` and
imports `common` (paths, DB, parsing, progress). Cross-file loading uses
`common.load_script` (numbered filenames are not importable module names).

## Duplicated logic (verified via similarity analysis + reads)

Near-identical function pairs between the Tk and web monitors — the primary
maintenance cost of the dashboard layer (every KPI change is made twice):

| Function | 001a (tk) | 001b (web) |
|---|---|---|
| `finish_stamp_from_detail_json` | ✔ | ✔ (also in `002-next-run-timer.py`) |
| `finish_stamp_from_folder` | ✔ | ✔ |
| `is_done` | ✔ | ✔ |
| `load_detail_payload_for_kpi` | ✔ | ✔ |
| `summarize_items_for_kpi` | ✔ | ✔ |
| `load_record_index` | ✔ | ✔ |
| `parse_progress_file` | ✔ | ✔ |
| `queue_snapshot` / `queue_payload` | ✔ | ✔ |
| `status_snapshot` / `status_payload` | ✔ | ✔ |
| `tail` | ✔ | ✔ |
| `webhook_running` | ✔ | ✔ |

**Phase 4 status (implemented):** the KPI/data engine now lives in
`src/monitor/monitor_common.py` — `db_review_stats`, `summarize_items_for_kpi`,
`load_detail_payload_for_kpi`/`load_detail_items_for_kpi`, `load_record_index`,
`finish_stamp_from_folder`/`finish_stamp_from_detail_json`,
`read_last_summary`, `stats_to_csv`, plus a `--json`/`--csv` CLI. Both monitors
import it, and `pcc kpi` executes it directly (the former `bin/pcc` heredoc is
gone). Number parity pcc == web == tk was verified against the live DB.

Still duplicated (lower value, deferred): `parse_progress_file`, `is_done`,
`progress_stale`, `queue_snapshot`/`queue_payload`,
`status_snapshot`/`status_payload`, `tail`, `webhook_running` — candidates for
a follow-up extraction. Also `guess_finish_date_from_text` in both
`src/common.py` and `src/tools/090b-migrate-previous-records.py` (legacy tool,
leave as-is).

## Legacy / one-time scripts

- `src/tools/090a-migrate-previous-records.sh` + `090b-...py` — one-time
  migration of pre-layout archives.
- `src/tools/100-migrate-apps-layout.sh` — one-time Apps-layout migration
  (still wired to `pcc migrate`).
- `docs/reports/*.md` — stale point-in-time review reports.

Keep until confirmed unused in the field, but do not extend them.

## Fragile couplings to be aware of

- Worker liveness is detected by `pgrep -f "[r]un-worker.sh"` in
  `110a-request-run.sh`, `050-watch-queue-flag.sh`, and the monitors —
  renaming `100-run-worker.sh` silently breaks all of them.
- Similar name-greps target `001a-monitor-tk.py`, `update-local-copy.sh`,
  `update-loader.py` (`000-open-monitor.sh:56`, `050-watch-queue-flag.sh:46-48`).
- The queue protocol is bare flag files in `PC_QUEUE_DIR`
  (`run_all_requested.flag`, `run_all_in_progress.flag`,
  `run_all_stop_no_resume.flag`, `update_monitor_requested.flag`) — shared
  between host processes AND the docker webhook container via the bind mount
  (`docker-compose.yml:91-95`). Any rename is a cross-container protocol change.
- `monitor_settings.env` is parsed by four different consumers (both monitors,
  the worker's `sed` extraction at `100-run-worker.sh:283`, and `bin/pcc`'s
  shlex parsing) — keep the KEY=VALUE format strictly simple.
