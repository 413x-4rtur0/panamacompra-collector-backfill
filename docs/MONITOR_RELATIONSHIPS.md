# Monitor & Script Relationships

File-level dependency reference: who calls what, where data flows, and where
logic is duplicated. Verified against the code in July 2026 (citations are
file:line or function names).

## Monitors

| File | Kind | Started by | Reads | Writes |
|---|---|---|---|---|
| `src/40_monitor/000-open-monitor.sh` | chooser | `pcc monitor`, `110a-request-run.sh` | saved/explicit `PC_MONITOR_MODE` (manual default `tk`) | starts 001a, 001b, or 001c; Tk/terminal also start the small timer |
| `src/40_monitor/001a-monitor-tk.py` | native Tk dashboard, 5 tabs | 000-open-monitor.sh | progress/summary env files, archive DB, settings env | `monitor_settings.env`, chat/keyword/format files; spawns 070/110a/110b/120b (`:1273-1286`) |
| `src/40_monitor/001b-monitor-web.py` | web dashboard + JSON API (~25 endpoints, `:1625-1888`), 6 tabs | 000-open-monitor.sh, `src/50_tools/130-open-web-app.sh` | same as 001a | same as 001a via POST endpoints |
| `src/40_monitor/001c-monitor-terminal.sh` | terminal watcher | `pcc watch`, 000-open-monitor.sh when terminal mode is selected | progress/queue/logs | — |
| `src/40_monitor/002-next-run-timer.py` | countdown widget | 000-open-monitor.sh / manual | changedetection watch API + synced interval; progress + last-summary env and DB as fallback/context | — |
| `src/40_monitor/003-update-loader.py` | update-then-open-monitor loader | worker (`100-run-worker.sh:68`), `050-watch-queue-flag.sh:57` | — | runs `update-local-copy.sh`, then 000-open-monitor.sh |

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
 ├─ start        → src/20_pipeline/110a-request-run.sh ──┬─ spawns 100-run-worker.sh
 │                                                    └─ opens src/40_monitor/000-open-monitor.sh (manual Tk default)
 ├─ stop         → src/20_pipeline/120a-stop-everything.sh
 ├─ status       → src/20_pipeline/130a-queue-status.sh
 ├─ watch        → src/40_monitor/001c-monitor-terminal.sh
 ├─ monitor      → src/40_monitor/000-open-monitor.sh → 001a / 001b / 001c
 ├─ webhook start→ src/10_webhook/020-start-listener.sh → 010-webhook-listener.py
 ├─ webhook diag → src/10_webhook/040-diagnose-webhook.sh
 ├─ docker       → src/50_tools/010-docker-stack.sh (compose stack; syncs changedetection timer access/interval privately)
 ├─ kpi          → inline Python heredoc (bin/pcc:190-341) over common.init_db
 ├─ calendar     → src/50_tools/030-opportunity-calendar.py
 ├─ notify       → src/20_pipeline/020-notify-whatsapp.py --record N
 ├─ test-whatsapp→ src/30_notify/010-waha-client.py
 ├─ health       → review-system.sh (root)
 ├─ full-report  → src/50_tools/140-full-report.py
 ├─ templates    → src/50_tools/020-record-templates.py
 ├─ format       → src/50_tools/040-message-formats.py
 ├─ keywords test→ loads 020-notify-whatsapp.py as a module (bin/pcc:436-448)
 ├─ upload       → src/50_tools/150-upload-github.sh
 ├─ migrate      → src/50_tools/100-migrate-apps-layout.sh
 ├─ setup        → setup.sh · uninstall → scripts/uninstall.sh
 └─ launcher     → scripts/install-desktop-launcher.sh → scripts/tasks/*.sh

100-run-worker.sh (orchestrator)
 ├─ 000-update-before-run.sh → src/50_tools/050-maintain-database.py
 ├─ 015-import-index-snapshot.py → common.insert_or_update_index
 ├─ 010-collect-index.py         → common.insert_or_update_index
 ├─ 020-notify-whatsapp.py (--announce / --announce-details / --sync-snapshots)
 ├─ 030-collect-details.py ──(load_script)──▶ 020-notify-whatsapp.py (inline)
 ├─ 040-build-detail-views.py
 ├─ src/50_tools/020-record-templates.py apply
 ├─ 050-repair-missing-deadlines.py ──(load_script)──▶ src/50_tools/080-update-day-folder.py
 ├─ 060-build-calendar.py
 ├─ src/30_notify/010-waha-client.py (notify_waha, :45-55)
 ├─ 070-test-zone.py (opt-in)
 └─ src/40_monitor/003-update-loader.py (queued update, :60-72)

webhook trigger path
 changedetection ─▶ 010-webhook-listener.py
   ├─ host mode:      060-run-collector.sh → 110a-request-run.sh (AUTO + terminal monitor override)
   └─ enqueue mode:   run_all_requested.flag ◀─ polled by 050-watch-queue-flag.sh → 110a (AUTO + terminal monitor override)
```

`PC_MONITOR_MODE=tk` remains the manual/default dashboard choice.
Changedetection paths read `PC_CHANGEDETECTION_MONITOR_MODE=terminal` and pass
it as a command-scoped `PC_MONITOR_MODE`, so cron/manual launches are unchanged.

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
`src/40_monitor/monitor_common.py` — `db_review_stats`, `summarize_items_for_kpi`,
`load_detail_payload_for_kpi`/`load_detail_items_for_kpi`, `load_record_index`,
`finish_stamp_from_folder`/`finish_stamp_from_detail_json`,
`read_last_summary`, `stats_to_csv`, plus a `--json`/`--csv` CLI. Both monitors
import it, and `pcc kpi` executes it directly (the former `bin/pcc` heredoc is
gone). Number parity pcc == web == tk was verified against the live DB.

Still duplicated (lower value, deferred): `parse_progress_file`, `is_done`,
`progress_stale`, `queue_snapshot`/`queue_payload`,
`status_snapshot`/`status_payload`, `tail`, `webhook_running` — candidates for
a follow-up extraction. Also `guess_finish_date_from_text` in both
`src/common.py` and `src/50_tools/090b-migrate-previous-records.py` (legacy tool,
leave as-is).

## Legacy / one-time scripts

- `src/50_tools/090a-migrate-previous-records.sh` + `090b-...py` — one-time
  migration of pre-layout archives.
- `src/50_tools/100-migrate-apps-layout.sh` — one-time Apps-layout migration
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
