# CODEBASE_AUDIT_PLAN

Full codebase audit of the PanamaCompra Collector — July 2026. Read-only
review; every relationship below was verified by reading the cited file. Items
that could not be verified from the repository are explicitly marked
**uncertain**.

Companion documents produced by this audit:

- [ARCHITECTURE.md](ARCHITECTURE.md) — verified system architecture with citations.
- [MONITOR_RELATIONSHIPS.md](MONITOR_RELATIONSHIPS.md) — file-level call graph, duplication map, fragile couplings.
- [DASHBOARD_KPI_PLAN.md](DASHBOARD_KPI_PLAN.md) — dashboard/KPI gap analysis and target design.
- [DIAGRAMS.md](DIAGRAMS.md) — Mermaid diagrams (architecture, pipeline, notification, error handling, data model).

---

## 1. Codebase relationship map

### Entry points

| Entry point | Role | Proof |
|---|---|---|
| `bin/pcc` | CLI hub — dispatches every command to the scripts below | case statement `bin/pcc:100-489` |
| `setup.sh` | Installer (apt, venv, Playwright browser, docker) | sources `lib/env.sh`; `PC_SETUP_SKIP_*` flags |
| `docker-compose.yml` | Container stack: changedetection + sockpuppetbrowser + WAHA + enqueue-only webhook | services block, lines 26-97 |
| `systemd/user/panamacompra.service` | Runs `pcc start` as a user service | `ExecStart=%h/.local/share/panamacompra/bin/pcc start` |
| `systemd/user/panamacompra-webhook.service` | Runs `pcc webhook start --foreground` | `ExecStart` line |
| `scripts/tasks/0*.sh` | 5-line wrappers that `exec` into `pcc`/root scripts (desktop-launcher targets) | e.g. `scripts/tasks/020-start-collector.sh` → `exec $ROOT/bin/pcc start` |
| `update-local-copy.sh`, `review-system.sh` | Root maintenance scripts, also reachable via `pcc` (`pcc health` → `review-system.sh`) | `bin/pcc:460` |

### Monitors (`src/40_monitor/`)

- `000-open-monitor.sh` — chooser; default `PC_MONITOR_MODE=tk` (`:22`), starts
  `001a` (`:92`) or `001b` (`:114`).
- `001a-monitor-tk.py` (2,957 lines) — native Tk dashboard, 5 tabs (`:1241-1248`).
- `001b-monitor-web.py` (1,937 lines) — HTTP dashboard + JSON API (~25
  endpoints, `:1625-1888`), 6 tabs (`:964`).
- `001c-monitor-terminal.sh` — terminal watch mode (`pcc watch`, `bin/pcc:143`).
- `002-next-run-timer.py` — countdown widget (next run, latest records, last summary).
- `003-update-loader.py` — Tk loader running `update-local-copy.sh` then opening the monitor.

### Pipeline (`src/20_pipeline/`) — numbered by execution order

- `000-update-before-run.sh` — git fetch/ff-or-hard-reset + DB maintenance + pip install.
- `010-collect-index.py` — Playwright Firefox crawl of Programadas + Abiertas index tables.
- `015-import-index-snapshot.py` — parses the changedetection Brotli snapshot
  instead of re-crawling; feeds `common.insert_or_update_index` (`src/common.py:1366`).
- `020-notify-whatsapp.py` (1,572 lines) — rich per-record WhatsApp messages:
  `--announce`, `--announce-details`, `--sync-snapshots`, keyword filters
  (`load_filter_rules`/`evaluate_filter`; also used by `pcc keywords test`,
  `bin/pcc:436-448`).
- `030-collect-details.py` — detail-page downloader; dynamically loads 020 for
  inline detail messages (`load_script("src/20_pipeline/020-notify-whatsapp.py")`).
- `040-build-detail-views.py` — normalizer: rebuilds `summary`/`items`/`calendar`
  views inside each `*.detail.json`.
- `050-repair-missing-deadlines.py` — re-fetches records missing DTEND.
- `060-build-calendar.py` — timestamped `.ics` packages under `data/calendar/`.
- `070-test-zone.py` — sandbox re-run into `records_test/`.
- `100-run-worker.sh` (692 lines) — **the orchestrator** (flock-locked loop, steps 1-8).
- `110a-request-run.sh` / `110b-run-now.sh` — queue flag + spawn worker / foreground worker.
- `120a-stop-everything.sh` / `120b-stop-collectors.sh` — stop scripts.
- `130a/b/c` — queue status / run status / follow log.

### Notification — two senders, deliberately split

- `src/30_notify/010-waha-client.py` — plain system/summary messages; per-purpose
  chat routing (`CHAT_PURPOSES`, `configured_chat_id` fallback chain `:89-102`),
  retry with backoff and an in-process circuit breaker (`send_text`, `:225-258`).
- `src/20_pipeline/020-notify-whatsapp.py` — rich record messages (index alerts,
  detail follow-ups, status changes).

### Webhook (`src/10_webhook/`)

`010-webhook-listener.py` (token-gated `hmac.compare_digest` `:100`;
`ENQUEUE_ONLY` mode `:30`), `020-start-listener.sh`, `030-install-service.sh`,
`040-diagnose-webhook.sh`, `050-watch-queue-flag.sh` (host-side poller for the
dockerized listener), `060-run-collector.sh` (sets `PC_RUN_MODE=AUTO` → `110a`).

### Tools (`src/50_tools/`)

`010-docker-stack.sh` (manages the compose stack; **generates `.webhook_token`**,
`:101-106`), `020-record-templates.py`, `030-opportunity-calendar.py`,
`040-message-formats.py`, `050-maintain-database.py`,
`060-import-selected-calendars.py`, `070-rename-record-folders.py`,
`080-update-day-folder.py`, `090a/090b-migrate-previous-records`,
`100-migrate-apps-layout.sh`, `110-reset.py`, `120-setup-git-credentials.sh`,
`130-open-web-app.sh`, `140-full-report.py`, `150-upload-github.sh`.

### Config & storage

- `lib/env.sh` — the config spine, sourced by nearly every script; precedence
  env var > `config/defaults.env` > `.env` > `$PC_CONFIG_DIR/env` (`:75-80`);
  resolves all `PC_*` paths; three modes (installed/portable/development).
- `config/changedetection-browser-steps.js` — the crawl script pasted into
  changedetection's Browser Steps.
- Runtime settings: `$PC_DATA_DIR/config/monitor_settings.env` — written by
  both monitors and `pcc set`, read by the worker (`100-run-worker.sh:16-22`).
- Storage: SQLite `data/panamacompra_archive.db` (table `opportunities`, schema
  migrations `src/common.py:1166-1183`), `panamacompra_index.csv`,
  `records/<day>/(<finish>)-(<numero>)-(<desc>)/` folders,
  `data/calendar/*.ics`, logs + progress env files in `data/logs/`
  (`run_all_progress.env`, `run_all_last_summary.env` — the monitors' data feed).

---

## 2. Verified architecture graph

```
SOURCE                 panamacompra.gob.pa (BASE_URL, src/common.py:76)
  │
  ├─[watch]─ changedetection container (docker-compose.yml:37) + browser-steps JS
  │             └─ notification json://host:8765/panamacompra/<token>
  │                   └─ src/10_webhook/010-webhook-listener.py  (handle_trigger :97)
  │                        ├─ direct: 060-run-collector.sh → 110a-request-run.sh (PC_RUN_MODE=AUTO)
  │                        └─ enqueue-only (docker): touch run_all_requested.flag
  │                              └─ 050-watch-queue-flag.sh (host) → 110a-request-run.sh
  │
  └─[manual]─ pcc start / monitors' "Request run" → 110a / 110b
                    │
                    ▼
      src/20_pipeline/100-run-worker.sh   (flock loop, :197-686)
        STEP 0  000-update-before-run.sh                (:225-242)
        STEP 1  015-import-index-snapshot.py (AUTO)     (:286-310)
                └ exit 3 → hybrid: 010-collect-index.py crawls unhealthy groups (:298-328)
                010-collect-index.py (manual/fallback)
                    └─ common.insert_or_update_index → SQLite + CSV + index JSON
        STEP 2  020-notify-whatsapp.py --announce       (:392)   ── WAHA /api/sendText
        STEP 3  030-collect-details.py                  (:409)   → records/ folders
                    └ inline detail messages via loaded 020 module
        STEP 4  040-build-detail-views.py --apply       (:438)   (normalizer)
                020-record-templates.py apply           (:460)
        STEP 5  020-notify-whatsapp.py --announce-details (:484)
        STEP 6  py_compile + 050-repair-missing-deadlines.py (:508-527)
        STEP 7  060-build-calendar.py                   (:550)   → data/calendar/*.ics
        SUMMARY notify_waha via src/30_notify/010-waha-client.py (purpose=summary) (:623-630)
        STEP 8  070-test-zone.py (opt-in, PC_TEST_ZONE_AUTORUN) (:661-669)
        │
        └─ writes run_all_progress.env / run_all_last_summary.env / logs
              ▲ read by ▼
      MONITORS: 001a-monitor-tk.py · 001b-monitor-web.py (status_payload :807) · 001c · 002-next-run-timer.py
```

### Uncertain items

- The actual changedetection **watch configuration** (URL, CSS filter
  `#pc-monitor-output`, notification URL) lives in the container datastore
  (`var/integrations/changedetection/`), not in the repo — configured by hand
  in the UI. Cannot verify it matches the repo's browser-steps script version.
- `systemd/user/panamacompra.service` runs `pcc start`, which **exits after
  queueing** (`110a-request-run.sh` spawns the worker with `nohup` and
  returns). With `Type=simple` + `Restart=on-failure`, the unit goes inactive
  after each successful trigger — works as a one-shot but is misdeclared.
  Needs a live systemd check to confirm behavior.
- `.github/workflows/ci.yml` contents not reviewed in depth.

---

## 3. Dashboard/KPI review

Summary here; full gap table and target design in
[DASHBOARD_KPI_PLAN.md](DASHBOARD_KPI_PLAN.md).

**Exists today:** 6 web tabs / 5 tk tabs; KPI tab with 8 cards (archive total,
saved/pending/failed, item lines, avg items, deadline repairs, closure %),
11 chart panels, and window/group/entity filters mirrored by `pcc kpi`.

**Main gaps:** no "New today" / "Closing soon" / Abiertas-Programadas cards; no
failed-alert tracking (send failures are never persisted); per-stage run
durations recorded in `run_all_last_summary.env` but never charted; no
freshness indicator; no chart→records drilldown; no CSV export; KPI logic
implemented three times (web, tk, `bin/pcc:190-341` heredoc).

**Recommended tab order:** Overview *(new)* → Operations → Opportunities
(renamed Records & Database) → KPIs & Statistics → Alerts (WhatsApp) →
Integrations → Settings. This is a regrouping of the existing `data-tab`
cards, not a rewrite.

---

## 4. Diagrams

Produced as Mermaid in [DIAGRAMS.md](DIAGRAMS.md): system architecture,
pipeline execution flow (with failure branches), notification purpose-routing
with fallback chain, monitor information flow, dashboard IA (current →
proposed), error-handling/resume flow, and the entity/storage data model.

---

## 5. Duplication, dead code, risks, secrets

### Duplicated logic

- **Tk ↔ Web monitor twins** — at least 11 near-identical functions
  (`finish_stamp_from_detail_json`, `finish_stamp_from_folder`, `is_done`,
  `load_detail_payload_for_kpi`, `load_record_index`, `parse_progress_file`,
  `queue_snapshot`/`queue_payload`, `status_snapshot`/`status_payload`,
  `summarize_items_for_kpi`, `tail`, `webhook_running`). #1 maintenance cost:
  every KPI change is made twice (see git history, e.g. commit 296043b
  "Expose … in both monitors").
- **KPI logic tripled**: ~150-line Python heredoc in `bin/pcc:190-341`
  re-implements the monitors' KPI queries.
- `guess_finish_date_from_text` duplicated between `src/common.py` and
  `src/50_tools/090b-migrate-previous-records.py`.

### Dead / legacy candidates

- `src/50_tools/090a+090b-migrate-previous-records`, `src/50_tools/100-migrate-apps-layout.sh`
  — one-time migrations, likely done; document as legacy, do not extend.
- `docs/reports/*.md` — three stale point-in-time review reports.
- Root `__pycache__/` directory on disk (gitignored) plus `.pyc` caches under
  `src/*/__pycache__`.
- `integrations` symlink at repo root is **owned by root** (created by a sudo
  run) — harmless today, a permission tripwire later.
- `README.md` at ~134 KB is a monolith serving as the only architecture doc
  (now supplemented by this doc set).

### Risks

- **`000-update-before-run.sh` performs `git reset --hard origin/<branch>`**
  when fast-forward fails, auto-stashing tracked changes. Correct for an
  appliance, dangerous on a development machine — discards local commits on
  diverged branches. Needs a guard for `APP_MODE=development` (Phase 5).
- Worker liveness detected via `pgrep -f "[r]un-worker.sh"` in 4+ scripts —
  brittle string coupling to the filename (same pattern for the tk monitor and
  updater scripts).
- The webhook trigger token travels in the **URL** (may be logged by proxies /
  changedetection). `hmac.compare_digest` on the path is good; acceptable on a
  LAN, worth noting.
- systemd `panamacompra.service` Type mismatch (see §2 uncertain).
- Queue protocol is bare flag files shared between host processes and the
  docker webhook container via a bind mount — any rename is a cross-container
  protocol change.

### Secrets (paths + variable names only; values intentionally not recorded)

- `.env` — contains `WAHA_API_KEY` (+ dashboard credentials). Gitignored ✅,
  file mode 600 ✅.
- `.webhook_token` — trigger token, generated by
  `src/50_tools/010-docker-stack.sh:101-106`. Gitignored ✅, mode 600 ✅.
- ⚠️ `config/defaults.env:17` and `docker-compose.yml:71` **commit the fixed
  default WAHA dashboard password** (`WAHA_DASHBOARD_PASSWORD`), documented in
  UI strings as a deliberate "always reachable" default. Consequence: every
  install ships a known dashboard login on port 3000. Recommendation: generate
  a random default at setup (as `.webhook_token` already does) — Phase 5.

---

## 6. Staged implementation plan

| Phase | Scope | Files | Risk | Test | Rollback |
|---|---|---|---|---|---|
| **1** ✅ done | No-risk cleanup & docs: this doc set; pruned `docs/reports/`; legacy migration tools marked + pipeline map corrected in `scripts/README.md`; stray root `__pycache__` deleted. (`integrations` symlink ownership needs root — left as-is, see §5.) | `docs/*`, `scripts/README.md` | None | `bash -n` touched scripts; `pcc help` | `git revert` |
| **2** ✅ done | Dashboard: web Overview tab (health cards, last-run stage bars from `run_all_last_summary.env`, services line) as the new default tab with reordered nav; `new_today`/`closing_soon`/`abiertas`/`programadas` counters and Alerts-sent card in both monitors; tk KPIs tab last-run line | `src/40_monitor/001b-monitor-web.py`, `src/40_monitor/001a-monitor-tk.py` | Low | `py_compile` both; `/api/db-stats` + `/api/status` smoke-tested; web↔tk number parity verified | revert 2 files |
| **3** ✅ done | Delivery tracking: additive `notify_attempts`/`notify_error` columns via the migration map; `020-notify-whatsapp.py` persists per-record send outcomes; Failed-alerts KPI in both monitors; `/api/kpi-export` CSV endpoint + button | `src/common.py`, `src/20_pipeline/020-notify-whatsapp.py`, monitors | Medium (additive schema) | migration verified on a DB copy; smoke-tested | revert code; new columns stay inert |
| **4** ✅ done | Shared KPI engine `src/40_monitor/monitor_common.py` consumed by both monitors and `pcc kpi` (heredoc removed); number parity verified across the three surfaces. Remaining monitor-local duplicates (progress/queue/status helpers) deferred to a follow-up | `src/40_monitor/monitor_common.py` (new), `001a`, `001b`, `bin/pcc` | Medium | `py_compile`; parity check pcc == web == tk | revert; no data touched |
| **5** ✅ done | `panamacompra.service` → `Type=oneshot`; development-mode guard for `git reset --hard` (`PC_UPDATE_FORCE_RESET=1` opt-out); random WAHA dashboard password at setup (fixed default removed from shipped config/UI); data-freshness check in `review-system.sh` (`PC_FRESHNESS_MAX_HOURS`) | `systemd/user/panamacompra.service`, `src/20_pipeline/000-update-before-run.sh`, `src/50_tools/010-docker-stack.sh`, `review-system.sh`, `config/defaults.env`, `.env.example`, `docker-compose.yml` | Medium | `bash -n`; `systemd-analyze verify`; freshness smoke test | revert commit |

Standing constraint: KPI numbers shown by the web monitor, tk monitor, and
`pcc kpi` must stay in agreement at every phase.
