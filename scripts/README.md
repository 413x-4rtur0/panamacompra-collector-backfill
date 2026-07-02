# Script organization and ordered task names

Engine scripts live under `src/`, grouped by concern:

- `src/pipeline/` — the run-all worker and its sequential steps.
- `src/webhook/` — the changedetection.io webhook listener and its helpers.
- `src/monitor/` — the Tk/web/terminal monitor UIs and the updater loader.
- `src/notify/` — the low-level WAHA HTTP client.
- `src/tools/` — one-off maintenance/migration utilities.

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
- `src/pipeline/010-collect-index.py` — STEP 1 of the run-all worker sequence.
- `src/monitor/001a-monitor-tk.py` / `001b-monitor-web.py` / `001c-monitor-terminal.sh` — mutually exclusive monitor UI choices, selected by `PC_MONITOR_MODE`.

Rules:

1. `NNN` communicates lifecycle/sequence order. In `scripts/tasks/`, lower
   numbers happen earlier (install → update → run → status → stop →
   uninstall). In `src/pipeline/`, the numbers mirror the run-all worker's
   documented STEP 0-7 sequence in the main README.
2. Optional letters (`001a`, `001b`, `001c`, ...) mark mutually exclusive or
   conditional variants inside the same phase/step (e.g. the three monitor
   UIs, chosen by `PC_MONITOR_MODE`).
3. The description is lowercase kebab-case and starts with a verb when
   possible. Scripts that are daemons/programs rather than actions (e.g.
   `listener.py`, `common.py`) may be a plain noun instead.
4. Numbered task files under `scripts/tasks/` should be thin wrappers;
   implementation belongs in `src/` or the `bin/pcc` command dispatcher.
5. **Exception — Python import constraints:** a handful of `src/pipeline/`
   scripts are imported as real Python modules by sibling steps (not just
   executed as standalone scripts), e.g. `070-test-zone.py` does
   `from build_calendar import write_packages` and `from collect_detail import
   process_detail`. Python's `import` statement cannot reference a filename
   that starts with a digit or contains a hyphen, so these specific files keep
   plain `snake_case` names with no numeric prefix instead:
   `src/pipeline/030-collect-details.py` (STEP 2), `src/pipeline/060-build-calendar.py`
   (STEP 5), `src/pipeline/020-notify-whatsapp.py` (STEP 6), and
   `src/tools/080-update-day-folder.py`. `src/common.py` and
   `src/notify/010-waha-client.py` are also imported by many other scripts and
   follow the same underscore-only rule. The STEP order for these is
   documented in the main README's Scripts reference table and pipeline
   diagram, not encoded in the filename.

## Current lifecycle map

| Ordered wrapper | Canonical command | Purpose |
| --- | --- | --- |
| `tasks/001a-setup-development.sh` | `./setup.sh` | Bootstrap dependencies for a checkout. |
| `tasks/001b-install-update-monitor-launcher.sh` | `./bin/pcc launcher install` | Install the Update + Monitor desktop launcher. |
| `tasks/010-update-local-copy.sh` | `./update-local-copy.sh` | Update code/dependencies before monitor use. |
| `tasks/020-start-collector.sh` | `./bin/pcc start` | Queue/start a collector run. |
| `tasks/021-000-open-monitor.sh` | `./bin/pcc monitor` | Open the monitor independently. |
| `tasks/030-status.sh` | `./bin/pcc status` | Inspect queues, progress, logs, and process state. |
| `tasks/040-stop-all.sh` | `./bin/pcc stop` | Stop host processes. |
| `tasks/090-uninstall-or-purge.sh` | `./bin/pcc uninstall` | Stop services/containers and optionally purge state. |

## Run-all pipeline map (`src/pipeline/`)

| Step | Script | Purpose |
| --- | --- | --- |
| 0 | `000-update-before-run.sh` | Pre-run git/dependency refresh. |
| 1 | `010-collect-index.py` | Index scan (Programadas + Abiertas). |
| 2 | `030-collect-details.py` | Detail download (imported by STEP 4/7 tools; see naming exception above). |
| 3 | `040-build-detail-views.py` | Rebuild summary/items/calendar views. |
| 4 | `050-repair-missing-deadlines.py` | Verify/repair failed + missing-deadline records. |
| 5 | `060-build-calendar.py` | Build timestamped `.ics` packages (imported by STEP 7; naming exception). |
| 6 | `020-notify-whatsapp.py` | WhatsApp announcements (imported by `src/tools/060-import-selected-calendars.py`; naming exception). |
| 7 | `070-test-zone.py` | Optional sandbox re-run of the last N records. |

Numbered runner/inspector helpers in the same folder (`100-run-worker.sh`, `110a-request-run.sh`,
`110b-run-now.sh`, `130a-queue-status.sh`, `130b-run-status.sh`, `130c-follow-run.sh`,
`120a-stop-everything.sh`, `120b-stop-collectors.sh`) orchestrate or inspect the sequence
above rather than being a step in it.
