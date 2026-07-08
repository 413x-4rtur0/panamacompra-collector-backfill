# Dashboard & KPI Improvement Plan

Result of the July 2026 dashboard/KPI review. Describes the current state
(verified against the code), the gaps, the target design, and the staged
implementation plan. **No code has been changed yet** — this document is the
approved-scope reference for that work.

## 1. Current state (verified)

### Tabs

- **Web monitor** (`src/monitor/001b-monitor-web.py:964`), 6 tabs:
  Operations · Collector Settings · Integrations · WhatsApp · KPIs ·
  Records & Database.
- **Tk monitor** (`src/monitor/001a-monitor-tk.py:1241-1248`), 5 tabs:
  Operations · Settings · WhatsApp · KPIs · Records & Database
  (Integrations content folded into other panels).
- **Terminal** (`001c-monitor-terminal.sh`, `pcc watch`) and the countdown
  widget (`002-next-run-timer.py`) are read-only companions.

### KPI tab today (`001b:979`, cards rendered at `:1467`)

Cards: Index archive total · Details saved · Details pending · Details failed ·
Item lines parsed · Avg items/record · Deadline repairs · Detail closure %.

Charts: Detail status mix · Index groups · Daily intake (14d) · Monthly intake
trend · Top contracting entities · Locations/buying units · Most frequent
items · Latest parsed items · Detail queue pressure · Items analysis · Item
keyword cloud · decision-recommendations pane.

Filters: time window (All/7/30/90/365 days) · Group (grupo) · Entity — with
datalist autocomplete. The same slicing exists in the CLI: `pcc kpi --days N
--grupo X --entidad Y` (`bin/pcc:177-341`).

### Records & Database tab today

Record selector with deadline status, detail status, order, and six date-range
filters; actions: open folder, open portal, notify WhatsApp, import calendars,
copy templates (`001b:977`). Plus DB summary/review panels and the opportunity
calendar (day/week/month/year, by end/start/downloaded).

## 2. Gap analysis

| Candidate KPI | Status today | Evidence / note |
|---|---|---|
| Total opportunities | ✅ card | "Index archive total" |
| By entity / group / status / keyword / location | ✅ charts | KPI tab diagrams |
| New opportunities **today** | ❌ | daily-intake chart exists, no "today" card |
| Open opportunities (Abiertas) / Programadas | ⚠️ | inside "Index groups" chart only, not first-class cards |
| Closing soon | ⚠️ | "queue pressure" chart + Records filter (`soon`), no count card |
| Alerts sent | ⚠️ | `pcc kpi` prints "WAHA index alerts sent" (`notified_at IS NOT NULL`); web shows only "Notify backlog" |
| Failed alerts | ❌ | send failures are printed, never persisted — no DB column |
| Duplicate opportunities | ❌ | dedup is silent (NUMERO primary key); no duplicate-rate metric |
| Last successful / failed run | ⚠️ | in Operations progress panel, absent from KPI tab |
| Monitor freshness | ❌ | no "data age" indicator |
| Data completeness | ⚠️ | "Detail JSON coverage" exists in `pcc kpi` only |
| Processing latency | ⚠️ | per-stage seconds already recorded in `run_all_last_summary.env` (`100-run-worker.sh:608-621`) but never charted |
| WhatsApp delivery status | ❌ | no per-message delivery record |
| By source (snapshot vs crawler) | ⚠️ | `INDEX_SOURCE` recorded per run in the summary env, not per record |

Structural issues:

- Every KPI change must be made **three times**: web monitor, tk monitor, and
  the Python heredoc in `bin/pcc:190-341`. At least 11 helper functions are
  near-duplicated between the two monitors (see MONITOR_RELATIONSHIPS.md).
- Charts are not linked to the Records tab — no drilldown.
- No export of the filtered KPI slice.

## 3. Target design

### Recommended tab order (both monitors)

1. **Overview** *(new)* — health strip: last run (time, mode, result, index
   source), next scheduled run, per-stage duration bars, WAHA reachability,
   changedetection snapshot age, webhook listener status, data freshness.
2. **Operations** — run controls, live progress, queue, logs (as today).
3. **Opportunities** — rename of "Records & Database": selector, filters,
   calendar, record actions.
4. **KPIs & Statistics** — cards + charts + filters (extended per below).
5. **Alerts (WhatsApp)** — destinations, toggles, filters, formats, and the new
   delivery-status panel.
6. **Integrations** — webhook access, changedetection script, docker stack.
7. **Settings** — collector/timer/storage/templates/reset.

Rationale: an operator's first question is "is the system healthy and current?"
(Overview), then "what did it collect?" (Opportunities), then analysis (KPIs).
Settings and Integrations are visited rarely and belong last. This maps 1:1
onto the existing card `data-tab` attributes in the web monitor, so it is a
regrouping, not a rewrite.

### New KPI cards (Phase 2)

- **New today** — `first_seen >= date('now')`.
- **Closing in ≤N days** — reuse `PC_MONITOR_DEADLINE_SOON_DAYS`.
- **Abiertas now** / **Programadas now** — group counts as cards.
- **Alerts sent (window)** — `notified_at` within the current filter window.

All computable from the existing `opportunities` columns; no schema change.

### Overview/health strip (Phase 2)

Data already on disk, presentation only:

- `run_all_progress.env` → current phase, staleness.
- `run_all_last_summary.env` → started/finished, TOTAL/stage seconds,
  `INDEX_SOURCE` → horizontal stage-duration bar.
- WAHA reachability: existing test endpoint / last send result.
- Snapshot age: mtime of the newest changedetection snapshot (the same check
  `015-import-index-snapshot.py` performs with
  `PC_INDEX_SNAPSHOT_MAX_AGE_SECONDS`).

### Delivery tracking (Phase 3 — schema change)

Add via the existing additive migration map (`src/common.py:1166`):

- `notify_attempts INTEGER DEFAULT 0`
- `notify_error TEXT` (last failure reason, cleared on success)

`020-notify-whatsapp.py` and `010-waha-client.py` callers record outcomes.
Unlocks: **Failed alerts** card, per-record delivery status in the Opportunities
tab, and a delivery panel in the Alerts tab.

### Filters & drilldowns (Phase 3)

- Chart click → pre-fills Opportunities-tab filters (both are already
  parameterized; wire chart bar → filter values).
- **Source filter** (snapshot/crawler) once persisted per record.
- **Export CSV** button for the current filtered slice — `/api/db-stats`
  already computes it; add a `format=csv` variant.

### Single KPI engine (Phase 4)

Extract the duplicated monitor helpers into `src/monitor/monitor_common.py`
(name TBD) consumed by 001a, 001b, and a `--json` CLI mode that replaces the
`bin/pcc` heredoc. Acceptance: the three surfaces produce identical numbers for
the same filters.

## 4. Staged implementation plan

| Phase | Scope | Files | Risk | Test | Rollback |
|---|---|---|---|---|---|
| 1 ✅ done | Docs & cleanup (this doc set; pruned `docs/reports/`; legacy migration tools marked; pipeline map in `scripts/README.md` corrected) | `docs/*`, `scripts/README.md` | None | `bash -n` touched scripts; `pcc help` | `git revert` |
| 2 ✅ done | Web: new **Overview** tab (health cards, last-run stage bars, services line) is now the first/default tab; tabs reordered Overview → Operations → Opportunities → KPIs → WhatsApp → Integrations → Settings. Both monitors: `new_today`, `closing_soon` (window = `PC_MONITOR_DEADLINE_SOON_DAYS`), `abiertas`, `programadas` counters + Alerts-sent card; tk KPIs tab gained the last-run/stage-durations line (`read_last_summary` in both monitors) | `src/monitor/001b-monitor-web.py`, `src/monitor/001a-monitor-tk.py` | Low | `python -m py_compile src/monitor/001*.py`; `/api/db-stats` + `/api/status` smoke-tested; web↔tk number parity verified | revert 2 files |
| 3 | Delivery columns, Failed-alerts card, drilldown, CSV export | `src/common.py`, `src/pipeline/020-notify-whatsapp.py`, monitors | Medium (additive schema) | migration on a DB copy; `pcc kpi`; `070-test-zone.py --limit 2 --apply` | revert code; new columns stay inert |
| 4 | Shared monitor library; retire `pcc` KPI heredoc | new `src/monitor/monitor_common.py`, `001a`, `001b`, `bin/pcc` | Medium | `py_compile`; number-parity check web vs tk vs `pcc kpi` | revert; no data touched |
| 5 | systemd unit type fix, dev-mode guard for `git reset --hard`, random WAHA dashboard password, freshness alarm | `systemd/user/*.service`, `src/pipeline/000-update-before-run.sh`, `setup.sh`, `src/tools/010-docker-stack.sh` | Medium | `systemctl --user daemon-reload && start`; `pcc webhook diag`; `review-system.sh` | keep `.bak` units; revert commit |

Constraint carried from the audit: KPI numbers shown by the web monitor, tk
monitor, and `pcc kpi` must stay in agreement at every phase.
