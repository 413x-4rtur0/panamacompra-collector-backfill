# Script organization and ordered task names

Engine scripts live under `src/`, grouped by concern:

- `src/20_pipeline/` — the run-all worker and its sequential steps.
- `src/10_webhook/` — the changedetection.io webhook listener and its helpers.
- `src/40_monitor/` — the Tk/web/terminal monitor UIs and the updater loader.
- `src/30_notify/` — the low-level WAHA HTTP client.
- `src/50_tools/` — one-off maintenance/migration utilities.

`bin/pcc` is the stable command-line entrypoint; `scripts/tasks/` holds thin,
numbered wrappers around it for a visible install/operate lifecycle. Prefer
`bin/pcc <command>` or a numbered task wrapper over calling a `src/` script
directly, except for the manual single-phase runs documented in the main
README (e.g. running just the index or detail step by hand).

## Naming methodology

Use ordered names when the filename should communicate workflow hierarchy:

```text
NNN[-letter]-short-description.ext
```

Examples:

- `scripts/tasks/001a-setup-development.sh` — first bootstrap path for development/portable use.
- `scripts/tasks/001b-install-update-monitor-launcher.sh` — alternate/conditional first-time desktop step.
- `src/20_pipeline/010-collect-index.py` — STEP 1 of the run-all worker sequence.
- `src/40_monitor/001a-monitor-tk.py` / `001b-monitor-web.py` / `001c-monitor-terminal.sh` — mutually exclusive monitor UI choices. Manual launches use `PC_MONITOR_MODE` (default `tk`); changedetection-triggered runs pass `PC_CHANGEDETECTION_MONITOR_MODE` (default `terminal`) as a one-run override.
- `src/40_monitor/030-install-web-service.sh` — installs the web monitor as a persistent `systemd --user` service, so port 8766 remains supervised without a background terminal.

Rules:

1. `NNN` communicates lifecycle/sequence order. In `scripts/tasks/`, lower
   numbers happen earlier (install → update → run → status → stop →
   uninstall). In `src/20_pipeline/`, the numbers mirror the run-all worker's
   documented STEP 0-7 sequence in the main README.
2. Optional letters (`001a`, `001b`, `001c`, ...) mark mutually exclusive or
   conditional variants inside the same phase/step (e.g. the three monitor
   UIs, chosen manually by `PC_MONITOR_MODE` or automatically for
   changedetection by `PC_CHANGEDETECTION_MONITOR_MODE`).
3. The description is lowercase kebab-case and starts with a verb when
   possible. Scripts that are daemons/programs rather than actions (e.g.
   `listener.py`, `common.py`) may be a plain noun instead.
4. Numbered task files under `scripts/tasks/` should be thin wrappers;
   implementation belongs in `src/` or the `bin/pcc` command dispatcher.
5. **Exception — Python import constraints:** a handful of `src/20_pipeline/`
   scripts are imported as real Python modules by sibling steps (not just
   executed as standalone scripts), e.g. `070-test-zone.py` does
   `from build_calendar import write_packages` and `from collect_detail import
   process_detail`. Python's `import` statement cannot reference a filename
   that starts with a digit or contains a hyphen, so these specific files keep
   plain `snake_case` names with no numeric prefix instead:
   `src/20_pipeline/030-collect-details.py` (STEP 2), `src/20_pipeline/060-build-calendar.py`
   (STEP 5), `src/20_pipeline/020-notify-whatsapp.py` (STEP 6), and
   `src/50_tools/080-update-day-folder.py`. `src/common.py` and
   `src/30_notify/010-waha-client.py` are also imported by many other scripts and
   follow the same underscore-only rule. The STEP order for these is
   documented in the main README's Scripts reference table and pipeline
   diagram, not encoded in the filename.

## Current lifecycle map

| Ordered wrapper | Canonical command | Purpose |
| --- | --- | --- |
| `tasks/001a-setup-development.sh` | `./setup.sh` | Bootstrap dependencies for a checkout, install/update the monitor, changedetection, WAHA, low-resource Integration URLs, and Docker integration launchers unless `PC_SETUP_INSTALL_MONITOR_SHORTCUT=0`, and write the local integration access note with generated WAHA/webhook secrets. |
| `tasks/001b-install-update-monitor-launcher.sh` | `./bin/pcc launcher install` | Install the Update + Monitor, Monitor Only (native Tk without update), CLI Monitor (agent-style terminal TUI), Next Run Timer, changedetection, WAHA, low-resource Integration URLs, and Docker integration desktop launchers, each with its own generated icon and taskbar/window identity where applicable. |
| `tasks/010-update-local-copy.sh` | `./update-local-copy.sh` | Update code/dependencies before monitor use. |
| `tasks/011-upload-github.sh` | `./src/50_tools/150-upload-github.sh` | Commit local changes and push the current branch to GitHub/remote. |
| `tasks/020-start-collector.sh` | `./bin/pcc start` | Queue/start a collector run. |
| `tasks/021-000-open-monitor.sh` | `./bin/pcc monitor` | Open the monitor independently. |
| `tasks/030-status.sh` | `./bin/pcc status` | Inspect queues, progress, logs, and process state. |
| `tasks/040-stop-all.sh` | `./bin/pcc stop` | Stop host processes. |
| `tasks/090-uninstall-or-purge.sh` | `./bin/pcc uninstall` | Stop services/containers and optionally purge state. |

## Run-all pipeline map (`src/20_pipeline/`)

Worker execution order as driven by `100-run-worker.sh` (see
`docs/ARCHITECTURE.md` for the full flow with citations):

| Step | Script | Purpose |
| --- | --- | --- |
| 0 | `000-update-before-run.sh` | Pre-run git/dependency refresh. |
| 1 | `015-import-index-snapshot.py` | AUTO runs: import the index from the changedetection snapshot (no browser); partial snapshots fall through to the crawler for the unhealthy groups. |
| 1 | `010-collect-index.py` | Index crawl (Programadas + Abiertas) — manual runs and snapshot fallback. |
| 2 | `020-notify-whatsapp.py --announce` | WhatsApp index alerts, sent before the long download phase. |
| 3 | `030-collect-details.py` | Detail download (sends inline detail messages when enabled). |
| 4 | `040-build-detail-views.py` | Rebuild summary/items/calendar views + per-record `.ics`; then work templates are applied. |
| 5 | `020-notify-whatsapp.py --announce-details` | WhatsApp detail follow-ups (idempotent catch-up). |
| 6 | `050-repair-missing-deadlines.py` | Verify (py_compile) + repair failed/missing-deadline records. |
| 7 | `060-build-calendar.py` | Build timestamped `.ics` packages. |
| 8 | `070-test-zone.py` | Optional (opt-in) sandbox re-run when the run had no new records. |

Numbered runner/inspector helpers in the same folder (`100-run-worker.sh`, `110a-request-run.sh`,
`110b-run-now.sh`, `130a-queue-status.sh`, `130b-run-status.sh`, `130c-follow-run.sh`,
`120a-stop-everything.sh`, `120b-stop-collectors.sh`) orchestrate or inspect the sequence
above rather than being a step in it.

`125-run-priority.sh` is the shared exclusive dispatcher. Its runtime state is
under `data/queue/priority-run.state`, pending jobs are under
`data/queue/priority-pending/`, and the monitor displays both the active job and
the number waiting. Priority order is update/migration (100), repair (90), test
(80), manual collector (60), maintenance/Cron (50/40), and changedetection (20).
Requests are coalesced by operation where appropriate, and queued jobs resume
automatically after the current job exits.

## Legacy one-time tools

These completed their purpose and are kept only for reference/recovery; do not
extend them (see `docs/audits/CODEBASE_AUDIT_PLAN.md` §5):

- `src/50_tools/090a-migrate-previous-records.sh` + `090b-migrate-previous-records.py`
  — one-time migration of pre-layout record archives.
- `src/50_tools/100-migrate-apps-layout.sh` — one-time Apps-layout migration
  (still reachable via `pcc migrate`).
