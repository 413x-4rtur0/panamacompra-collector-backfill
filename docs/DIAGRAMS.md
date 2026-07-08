# Diagrams

Mermaid diagrams for the PanamaCompra Collector, verified against the code in
July 2026. Render on GitHub or any Mermaid-capable viewer. See
ARCHITECTURE.md for the prose version with file/line citations.

## 1. System architecture

```mermaid
flowchart LR
  subgraph Docker["docker-compose.yml"]
    CD[changedetection.io<br/>:5000] --> SP[sockpuppetbrowser<br/>headless Chromium]
    WAHA[WAHA WhatsApp API<br/>:3000]
    WHC[webhook container<br/>PC_WEBHOOK_ENQUEUE_ONLY=1]
  end

  PC[panamacompra.gob.pa] -->|Browser Steps JS<br/>config/changedetection-browser-steps.js| CD
  CD -->|"json://host:8765/panamacompra/&lt;token&gt;"| WHL[host listener<br/>src/webhook/010-webhook-listener.py]
  CD -->|same URL, compose network| WHC
  WHC -->|touch run_all_requested.flag| Q[(data/queue)]
  Q --> WQ[050-watch-queue-flag.sh<br/>host poller]
  WHL --> RC[060-run-collector.sh]
  RC & WQ --> REQ[110a-request-run.sh<br/>PC_RUN_MODE=AUTO]
  CLI[bin/pcc · monitors] --> REQ
  REQ --> W[100-run-worker.sh]
  W --> ST[(archive.db · records/ · calendar/ · logs/)]
  W -->|POST /api/sendText| WAHA
  ST --> MON[monitors<br/>001a tk · 001b web · 001c terminal · 002 timer]
```

## 2. Pipeline execution flow (one worker iteration)

```mermaid
flowchart TD
  S0["STEP 0 · 000-update-before-run.sh<br/>git ff/reset + DB maintenance<br/>(failure does not abort)"] --> S1{AUTO run and<br/>PC_INDEX_FROM_SNAPSHOT≠0?}
  S1 -->|yes| SNAP["STEP 1 · 015-import-index-snapshot.py<br/>parse changedetection snapshot"]
  S1 -->|no| IDX["STEP 1 · 010-collect-index.py<br/>Playwright Firefox crawl"]
  SNAP -->|exit 0 healthy| S2
  SNAP -->|"exit 3 partial → crawl only<br/>unhealthy groups (PC_INDEX_GROUPS)"| IDX
  SNAP -->|other exit| IDX
  IDX -->|exit ≠ 0| FAILIDX["FAILED · detail skipped<br/>notify_waha failed"]
  IDX --> S2["STEP 2 · 020-notify-whatsapp.py --announce<br/>index alerts BEFORE downloads"]
  S2 --> S3["STEP 3 · 030-collect-details.py<br/>+ inline detail messages (PC_NOTIFY_DETAILS_INLINE)"]
  S3 -->|exit ≠ 0 / 124 timeout| FAILDET[FAILED/TIMEOUT · later steps skipped]
  S3 --> S4["STEP 4 · 040-build-detail-views.py --apply<br/>+ 020-record-templates.py apply"]
  S4 --> S5["STEP 5 · 020 --announce-details<br/>(or --sync-snapshots when disabled)"]
  S5 --> S6["STEP 6 · py_compile core scripts<br/>+ 050-repair-missing-deadlines.py"]
  S6 --> S7["STEP 7 · 060-build-calendar.py<br/>timestamped .ics packages"]
  S7 --> SUM["run summary → 010-waha-client.py<br/>purpose=summary, stage durations"]
  SUM -.->|"idle run + PC_TEST_ZONE_AUTORUN=1"| S8["STEP 8 · 070-test-zone.py<br/>records_test/ sandbox"]
  SUM --> LOOP{run_all_requested.flag<br/>present again?}
  LOOP -->|yes| S0
  LOOP -->|no| END[worker exits · IDLE]
```

## 3. Notification flow & purpose routing

```mermaid
flowchart TD
  subgraph senders
    NR["020-notify-whatsapp.py<br/>rich record messages"]
    WC["010-waha-client.py<br/>system/summary messages"]
  end
  NR -->|"index alert (purpose=index)"| ROUTE
  NR -->|"detail follow-up (purpose=details)"| ROUTE
  NR -->|"status change (purpose=status)"| ROUTE
  WC -->|"health (purpose=system)"| ROUTE
  WC -->|"run summary (purpose=summary)"| ROUTE

  ROUTE{destination for purpose}
  ROUTE -->|1| P["PC_WAHA_CHAT_ID_&lt;PURPOSE&gt; env<br/>or waha_chat_id_&lt;purpose&gt;.txt"]
  ROUTE -->|2 fallback| D["PC_WAHA_CHAT_ID env<br/>or waha_chat_id.txt (default)"]
  ROUTE -->|3 fallback| ONE["exactly-one-purpose-filled rule<br/>(single group heuristic)"]
  P & D & ONE --> SEND["send_text → WAHA POST /api/sendText<br/>retries + circuit breaker"]
  SEND -->|filters first| KW["keyword rules per destination<br/>OR lines · AND '+' · NOT '-'<br/>(load_filter_rules / evaluate_filter)"]
```

## 4. Monitor information flow

```mermaid
flowchart LR
  W[100-run-worker.sh] -->|write_progress| PROG[run_all_progress.env]
  W -->|on success| LSUM[run_all_last_summary.env<br/>stage seconds · INDEX_SOURCE]
  W --> LOGS[worker/current/history logs]
  PIPE[pipeline steps] -->|common.write_run_progress| PROG
  PIPE --> DB[(archive.db)]

  PROG & LSUM & LOGS & DB --> TK[001a-monitor-tk.py]
  PROG & LSUM & LOGS & DB --> WEB[001b-monitor-web.py<br/>/api/status · /api/db-stats · …]
  PROG & LOGS --> TERM[001c-monitor-terminal.sh]
  PROG & LSUM & DB --> TIMER[002-next-run-timer.py]

  TK & WEB -->|write| SET[monitor_settings.env<br/>waha_chat_id*.txt · waha_keywords*.txt<br/>waha_format_*.txt]
  SET -->|sourced at startup| W
  SET -->|load_monitor_settings| PCC[bin/pcc]
```

## 5. Dashboard information architecture (current → proposed)

```mermaid
flowchart LR
  subgraph current["Current tabs (web)"]
    O1[Operations] --- S1[Collector Settings] --- I1[Integrations] --- W1[WhatsApp] --- K1[KPIs] --- R1[Records & Database]
  end
  subgraph proposed["Proposed order"]
    OV2[Overview *new*<br/>health · last run · freshness] --> O2[Operations] --> R2[Opportunities<br/>renamed Records & DB] --> K2[KPIs & Statistics] --> W2[Alerts / WhatsApp] --> I2[Integrations] --> S2[Settings]
  end
  current -.regroup, not rewrite.-> proposed
```

## 6. Error-handling / resume flow

```mermaid
flowchart TD
  RUN[worker running] -->|SIGINT/TERM/HUP| TRAP[terminate_worker → exit 128]
  RUN -->|normal loop end| DONE[RUN_COMPLETED=1]
  TRAP & CRASH[unexpected exit] --> EXITTRAP[mark_abrupt_exit_for_resume]
  EXITTRAP -->|stop_no_resume flag set| CLEAN["clear flags · STOPPED<br/>(deliberate stop, no restart)"]
  EXITTRAP -->|RUN_COMPLETED=0| RESUME["restore run_all_requested.flag<br/>notify_waha resume FAILED<br/>next start resumes pending work"]
  DONE --> IDLE[clear in-progress flag · IDLE]

  STEPFAIL[any step exit ≠ 0] --> PF[write_progress FAILED]
  PF --> NW[notify_waha failed/timeout]
  NW --> SKIP[downstream steps skipped<br/>iteration logged to history]
```

## 7. Entity / status data model (storage view)

```mermaid
erDiagram
  OPPORTUNITIES {
    text numero PK "natural key from portal"
    text grupo "Programadas | Abiertas"
    text entidad "contracting entity"
    text detail_status "pending | saved | failed"
    text first_seen
    text detail_saved_at
    text detail_json_path
    text finish_date_guess "DTEND"
    text start_date_guess "DTSTART"
    text notified_at "index alert sent"
    text detail_notified_at "follow-up sent"
    text pending_status_change
    text last_notified_status
    text last_notified_items_hash
  }
  OPPORTUNITIES ||--o| RECORD_FOLDER : "records/<day>/(finish)-(numero)-(desc)/"
  RECORD_FOLDER ||--o{ DETAIL_FILES : "detail.txt · detail.json · tables/*.json"
  RECORD_FOLDER ||--o| RECORD_ICS : ".calendar.ics"
  RECORD_ICS ||--o{ CALENDAR_PACKAGE : "data/calendar/<day>/*.ics"
```

Schema columns verified against the additive migration map in
`src/common.py:1166-1183`; folder convention from
`common.build_record_folder_leaf`.
