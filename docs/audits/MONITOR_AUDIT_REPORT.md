# Auditoría Completa: Sistema de Monitoreo — PanamaCompra Collector

> Generado: 2026-07-28
> Rama: `agent/priority-run-queue`
> Repo: `panamacompra-collector`

---

## 1. Inventario de Monitores (7 archivos)

| # | Archivo | Tipo | Líneas | Propósito |
|---|---|---|---|---|
| M1 | `src/40_monitor/001a-monitor-tk.py` | Tkinter GUI | 3,292 | Monitor principal con barra de progreso, chips, KPIs, calendario |
| M2 | `src/40_monitor/001b-monitor-web.py` | Web (Flask) | ~6,191 | Monitor web con dashboard completo, KPI diagramas, settings |
| M3 | `src/40_monitor/001c-monitor-terminal.sh` | Bash TUI | 543 | Menú interactivo en terminal con opciones de ejecución |
| M4 | `src/40_monitor/002-next-run-timer.py` | Tkinter overlay | 932 | Ventana countdown + última ejecución (overlay escritorio) |
| M5 | `src/40_monitor/002b-next-run-timer-cli.py` | CLI Python | 235 | Countdown en terminal + notificación desktop |
| M6 | `src/40_monitor/003-update-loader.py` | Tkinter loader | 150 | Ventana de progreso de actualización de código |
| M7 | `src/40_monitor/000-open-monitor.sh` | Bash launcher | 373 | Orquestador que abre monitores según flags y modo |

---

## 2. Inventario Completo de Flags + State Files

### 2A. Queue Flags (runtime, en `$PC_QUEUE_DIR` = `var/data/queue/`)

| Flag | Creado por | Consumido/leído por | Borrado por | Propósito |
|---|---|---|---|---|
| `run_all_requested.flag` | `010-webhook-listener.py:17`, botón "Run" en M1/M2/M4, `125-run-priority.sh`, `100-run-worker.sh` (auto-resume en `mark_abrupt_exit_for_resume`) | M1/M2/M3/M4, `100-run-worker.sh`, `130a-queue-status.sh`, `130b-run-status.sh`, `140-full-report.py` | `100-run-worker.sh:323` al iniciar iteración, `120a/b-stop-*.sh` al limpiar | Señal: alguien pidió una ejecución completa del pipeline |
| `run_all_in_progress.flag` | `100-run-worker.sh:308` al adquirir lock | `100-run-worker.sh` (loop check), `120a/b-stop-*.sh` (para detener) | `100-run-worker.sh:847` al salir limpio, `:274/279` en `mark_abrupt_exit`, `120a/b-stop-*.sh` al limpiar | Señal: el worker está ejecutando activamente |
| `run_all_stop_no_resume.flag` | `120a-stop-everything.sh:14`, `120b-stop-collectors.sh:27`, `110a-request-run.sh:14` (stop-and-replace) | `100-run-worker.sh:266` (si existe NO restaura `requested`), `125-run-priority.sh:56` (`stop_requested()`) | `100-run-worker.sh:267` en `mark_abrupt_exit`, `120a/b-stop-*.sh:94/73` al limpiar | Señal: detener sin re-autorequest |
| `update_monitor_requested.flag` | `010-webhook-listener.py`, botón "Update" en M1/M2/M4, `100-run-worker.sh` (post-run `launch_queued_update_monitor:80`) | M1/M2/M3/M4, `100-run-worker.sh`, `130a-queue-status.sh`, `140-full-report.py` | `100-run-worker.sh:86` en `launch_queued_update_monitor`, `003-update-loader.py` al terminar | Señal: actualización de código vía git pull |
| `update_monitor_in_progress.flag` | `100-run-worker.sh` (antes de lanzar update) | M1/M2/M3/M4, `130a-queue-status.sh`, `140-full-report.py` | Worker/updater al terminar | Señal: la actualización está ejecutándose |
| `dev_mode_active.flag` | `121-dev-mode.sh` (al activar pausa) | Solo `001b-monitor-web.py:1558` para badge | `121-dev-mode.sh` (al desactivar) | Marca: modo desarrollo activo |
| **⚡ `repair_requested.flag`** | **NUEVO** — botón "Repair" en monitores | **NUEVO** — `060-repair-failed.sh` | **NUEVO** — worker repair al empezar | Señal: repair de registros fallidos |
| **⚡ `repair_in_progress.flag`** | **NUEVO** — worker repair al empezar | **NUEVO** — monitores (badge) | **NUEVO** — worker repair al terminar | Señal: repair ejecutándose |
| **⚡ `health_requested.flag`** | **NUEVO** — botón "Health Check" | **NUEVO** — `150-health-check.sh` | **NUEVO** — health checker al empezar | Señal: health check solicitado |
| **⚡ `health_in_progress.flag`** | **NUEVO** — health checker al empezar | **NUEVO** — monitores (badge) | **NUEVO** — health checker al terminar | Señal: health check ejecutándose |

### 2B. State Files (`.env` persistente en `var/data/logs/`)

| Archivo | Path | Escrito por | Leído por | Propósito |
|---|---|---|---|---|
| `run_all_progress.env` | `var/data/logs/run_all_progress.env` | `100-run-worker.sh` (vía `write_progress()`), `common.py` (vía `write_run_progress()`) | M1, M2, M4 | Estado EN VIVO: fase, %, mensaje, ETA, contadores |
| `run_all_last_summary.env` | `var/data/logs/run_all_last_summary.env` | `100-run-worker.sh:752-765` al completar | M1, M2, M4, `monitor_common.py` | Resumen final: timestamps, segundos/fase, source índice |
| `detail_timing.env` | `var/data/logs/detail_timing.env` | Pasos del pipeline (timing detalles) | M2 (dashboard KPI) | Tiempos por página de detalle |
| `index_timing.env` | `var/data/logs/index_timing.env` | Pasos del pipeline (timing index) | M2 (dashboard KPI) | Tiempos por página de índice |
| `monitor_settings.env` | `var/data/config/monitor_settings.env` | UI de settings en M1/M2 (~50 settings) | Todos los monitores vía `setting()` | Config persistente |
| **⚡ `repair_progress.env`** | **NUEVO** `var/data/logs/repair_progress.env` | **NUEVO** `060-repair-failed.sh` | **NUEVO** monitores | Progreso de repair |
| **⚡ `health_result.env`** | **NUEVO** `var/data/logs/health_result.env` | **NUEVO** `150-health-check.sh` | **NUEVO** monitores | Resultado de health check |

**Campos de `run_all_progress.env`** (28 campos):
`PHASE`, `STATUS`, `PERCENT`, `MESSAGE`, `INDEX_LIMIT`, `ETA`, `DETAIL_LIMIT`, `STARTED_AT`, `UPDATED_AT`, `WORKER_PID`, `MODE` (AUTO/MANUAL/TEST), `RUN_TYPE`, `RUN_SOURCE`, `RUN_TRIGGER`, `TEST_AUTORUN`, `STEP_CURRENT`, `STEP_TOTAL`, `ITEM_CURRENT`, `ITEM_TOTAL`, `RECORDS_FOUND`, `RECORDS_NEW`, `RECORDS_EXISTING`, `RECORDS_SAVED`, `RECORDS_FAILED`, `RECORDS_PENDING`, `RECORDS_TEST`, `EXTRA`

### 2C. Archivos Adicionales de Estado

| Archivo | Path | Propósito |
|---|---|---|
| `waha_qr_required.env` | `$PC_RUN_DIR/waha_qr_required.env` | Warning cuando WAHA requiere QR. **NINGÚN monitor lo lee** ⚠️ |
| `index_crawl_result.env` | `$PC_QUEUE_DIR/index_crawl_result.env` | `CRAWL_FAILED_GROUPS` del crawler |
| `index_snapshot_result.env` | `$PC_QUEUE_DIR/index_snapshot_result.env` | `SNAPSHOT_UNHEALTHY_GROUPS`, `SNAPSHOT_RECOVERY_PAGES` del snapshot |
| `priority-run.state` | `$PC_QUEUE_DIR/priority-run.state` | Estado del despachador de prioridad |
| `priority-pending/` | `$PC_QUEUE_DIR/priority-pending/` | Solicitudes pendientes del priority dispatcher |
| `monitor_web.env` | `$PC_RUN_DIR/monitor_web.env` | URL publicada del web monitor (local + LAN) |
| `run_all_current.log` | `var/data/logs/run_all_current.log` | Log iteración actual del worker |
| `run_all_history.log` | `var/data/logs/run_all_history.log` | Historial acumulado de iteraciones |
| `run_all_worker.log` | `var/data/logs/run_all_worker.log` | Log completo del worker |
| `run_all_requests.log` | `var/data/logs/run_all_requests.log` | Log de solicitudes de ejecución |
| `manual_actions.log` | `var/data/logs/manual_actions.log` | Log de acciones manuales |
| `webhook_listener.log` | `var/data/logs/webhook_listener.log` | Log del listener webhook |
| `update_monitor_queue.log` | `var/data/logs/update_monitor_queue.log` | Log de actualizaciones encoladas |
| `monitor_server.log` | `var/data/logs/monitor_server.log` | Log del servidor web monitor |
| `monitor_tk.log` | `var/data/logs/monitor_tk.log` | Log del monitor Tk |
| `next_run_timer.log` | `var/data/logs/next_run_timer.log` | Log del timer Tk |
| `monitor_open.log` | `var/data/logs/monitor_open.log` | Log del launcher |

### 2D. Cursor de Base de Datos

| Estado | Dónde | Leído por | Propósito |
|---|---|---|---|
| `closed_crawl_state` (SQLite, 1 row) | `panamacompra_archive.db` | Solo `001b-monitor-web.py:1058`, `037-collect-closed-index.py` | Cursor reanudable backfill Cerradas: `backfill_page`, `backfill_complete`, `last_forward_run_at` |

### 2E. Lock Files (flock)

| Lock | Quién lo adquiere | Propósito |
|---|---|---|
| `/tmp/panamacompra_run_all_worker.lock` | `100-run-worker.sh:292` | Exclusividad worker principal |
| `/tmp/panamacompra_closed_new_worker.lock` | `070-run-collector-closed-new.sh:48` | Exclusividad crawler nuevas Cerradas |
| `/tmp/panamacompra_closed_backfill_worker.lock` | `080-run-collector-closed-backfill.sh:46`, `039-run-closed-backfill.sh:54` | Exclusividad backfill Cerradas |
| `/tmp/panamacompra_monitor_window.lock` | `001c-monitor-terminal.sh:9` | Singleton terminal TUI |
| `/tmp/panamacompra_cli_timer_desktop.lock` | `000b-open-cli-timer.sh:8` | Singleton timer CLI |
| `/tmp/panamacompra_repair_worker.lock` | **⚡ NUEVO** `060-repair-failed.sh` | Exclusividad repair worker |
| `/tmp/panamacompra_health_check.lock` | **⚡ NUEVO** `150-health-check.sh` | Exclusividad health check |
| `$PC_QUEUE_DIR/priority-run.lock` | `125-run-priority.sh:154/187` | Exclusividad priority dispatcher |
| `$PC_QUEUE_DIR/priority-run-wait.lock` | `125-run-priority.sh:102` | Cola de espera priority dispatcher |

---

## 3. Pipeline: Fases de `run_all_progress.env`

| Fase `PHASE` | `STATUS` posibles | `PERCENT` | Descripción |
|---|---|---|---|
| `STARTING` | `RUNNING` | 2% | Adquiriendo lock, tocando `in_progress.flag` |
| `UPDATE` | `RUNNING` | 3-5% | `git pull` pre-run |
| `INDEX` | `RUNNING`, `FAILED` | 10-50% | Paso 1/7: snapshot o crawlear índice |
| `MESSAGING` | `RUNNING`, `DONE`, `SKIPPED` | 52-55% | Paso 2/7: WhatsApp del índice |
| `DETAIL` | `RUNNING`, `TIMEOUT`, `FAILED` | 55-90% | Paso 3/7: descarga de detalles |
| `CALENDAR` | `RUNNING`, `FAILED` | 76-94% | Paso 4/7: vistas de detalle/calendario |
| `MESSAGING` (2nd) | `RUNNING`, `DONE`, `SKIPPED` | 80-83% | Paso 5/7: WhatsApp de detalles |
| `VERIFY` | `RUNNING`, `FAILED` | 84-86% | Paso 6/7: compilar + reparar |
| `CALENDAR` (2nd) | `RUNNING`, `FAILED` | 90-94% | Paso 7/7: paquetes .ics |
| `TEST` | `RUNNING`, `FAILED` | 100% | Paso 8: zona de prueba |
| `DONE` | `DONE` | 100% | Completado exitosamente |
| `IDLE` | `DONE` | 100% | Worker detenido |
| `REPEAT` | `RUNNING` | 5% | Nueva solicitud durante ejecución |
| `RESUME_PENDING` | `FAILED` | 0% | Worker terminó abruptamente |
| `STOPPED` | `DONE` | 100% | Detenido con `stop_no_resume` |

---

## 4. Matriz de Feature Parity por Monitor

**Leyenda:** ✅ = implementado, ⬜ = parcial, ❌ = no implementado, **NE** = No Esperado, **⚡** = NUEVO propuesto

| Feature | M1 Tk | M2 Web | M3 Terminal | M4 Timer Tk | M5 Timer CLI | M6 Loader |
|---|---|---|---|---|---|---|
| **Flags** | | | | | | |
| `run_all_requested` badge | ✅ | ✅ | ✅ texto | ✅ texto | ❌ | ❌ |
| `run_all_in_progress` badge | ✅ | ✅ | ✅ texto | ✅ texto | ❌ | ❌ |
| `stop_no_resume` badge | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `update_requested` badge | ✅ | ✅ | ✅ texto | ✅ texto | ❌ | ❌ |
| `update_in_progress` badge | ✅ | ✅ | ✅ texto | ✅ texto | ❌ | ❌ |
| `dev_mode_active` badge | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **⚡** `repair_requested/in_progress` | **⚡** | **⚡** | **⚡** | ❌ | ❌ | ❌ |
| **⚡** `health_requested/in_progress` | **⚡** | **⚡** | **⚡** | ❌ | ❌ | ❌ |
| **Progress en vivo** | | | | | | |
| `run_all_progress` barra+texto | ✅ | ✅ | ❌ | ⬜ texto | ❌ | ❌ |
| ETA | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Contadores records | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Step X/7 actual/total | ⬜ barra | ✅ texto | ❌ | ❌ | ❌ | ❌ |
| **⚡** `repair_progress` barra | **⚡** | **⚡** | **⚡** | ❌ | ❌ | ❌ |
| **Resumen última ejecución** | | | | | | |
| Duración total | ✅ tooltip | ✅ card | ❌ | ✅ texto | ❌ | ❌ |
| Segundos por fase | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Source del índice | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **KPIs** | | | | | | |
| `detail_timing` diagramas | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| `index_timing` diagramas | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Ubicación (torta) | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Estado de detalles | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Cerradas** | | | | | | |
| `closed_crawl_state` | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Process pills Cerradas | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Health Check** | | | | | | |
| **⚡** `health_result` display | **⚡** | **⚡** | **⚡** | ❌ | ❌ | ❌ |
| **⚡** Botón Health Check | **⚡** | **⚡** | **⚡** | ❌ | ❌ | ❌ |
| **WAHA** | | | | | | |
| `waha_qr_required` warning | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Estado sesión WAHA | ✅ icono | ✅ badge | ❌ | ❌ | ❌ | ❌ |
| **Run Controls** | | | | | | |
| Run All | ✅ | ✅ | ✅ menú | ❌ | ❌ | ❌ |
| Stop | ✅ | ✅ | ✅ menú | ❌ | ❌ | ❌ |
| Stop No Resume | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Dev Pause/Resume | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **⚡** Repair Failed | **⚡** | **⚡** | **⚡** | ❌ | ❌ | ❌ |
| **⚡** Health Check | **⚡** | **⚡** | **⚡** | ❌ | ❌ | ❌ |
| Update | ✅ | ✅ | ✅ menú | ❌ | ❌ | ✅ loader |
| Cerradas (backfill) | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Auto-minimize** | | | | | | |
| Minimizar auto-run | ❌ | NE (web) | ❌ | ❌ | ❌ | ❌ |
| **Timer countdown** | | | | | | |
| Countdown próx. ejecución | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| Botones acción en timer | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Update Loader** | | | | | | |
| Barra progreso update | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| Resultado (éxito/fallo) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Otros** | | | | | | |
| Sticky header | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Calendario ICS 30d | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

---

## 5. Gap Analysis Priorizado

### F1 — Run Controls en Tk
- Mostrar `run_all_stop_no_resume.flag` como badge
- Botón "Stop No Resume" separado
- Botón "Dev Pause/Resume"

### F2 — Progress bar Tk
- Añadir contador "Step X/7" como texto

### F3 — Process chips Tk
- Ya implementado, verificar cobertura

### F4 — Web → Tk parity
- KPIs de ubicación (diagrama torta)
- KPIs de timing (`detail_timing.env`, `index_timing.env`)
- KPIs de estado de detalles
- Estado `closed_crawl_state`

### F5 — Tk → Web parity
- `run_all_stop_no_resume.flag` badge
- `dev_mode_active.flag` ya ✅

### F6 — Terminal expansion
- Leer `run_all_progress.env`: fase + %
- Leer `run_all_last_summary.env`: resumen
- Badge `dev_mode_active`
- Badge `stop_no_resume`
- Botones Cerradas (backfill pause/resume/stop)
- Warning `waha_qr_required.env`

### F7 — KPI diagrams Tk
- Diagrama torta ubicaciones
- Gráfico timing detalles
- Estado Cerradas

### F8 — Timer action buttons (Tk)
- Badges: `requested/in_progress/stop_no_resume/update/dev_mode`
- Botones: "Run All", "Stop", "Update"
- Leer `run_all_progress.env`

### F9 — Update loader result
- Pantalla resultado: éxito/fallo/ya actualizado
- Tiempo transcurrido
- Botón reabrir monitor

### F10 — Auto-minimize + CLI timer
- Leer flags queue
- Leer `run_all_progress.env` + `run_all_last_summary.env`
- Badge `dev_mode_active`
- Auto-minimizar en auto-runs

### ⚡ F11 — Repair Mode (Fail Recovery)

**Objetivo:** Recuperar registros con `detail_status='failed'` re-descargándolos con reintentos.

**Estado actual:**
- `125-run-priority.sh` ya reconoce `source=repair` → `run_type=REPAIR` (priority 90) ✅
- `050-repair-missing-deadlines.py` cubre deadlines, no failed general ⬜

**Archivos nuevos:**

| Archivo | Propósito |
|---|---|
| `src/20_pipeline/060-repair-failed.py` | Re-descarga registros failed con reintentos (rotar UA, backoff 3s/10s/30s) |
| `src/20_pipeline/060b-repair-failed.sh` | Wrapper shell: lock + flags + invocación |

**Pipeline Repair:**
1. Adquirir `/tmp/panamacompra_repair_worker.lock`
2. Crear `repair_requested.flag` + `repair_in_progress.flag`
3. Escribir `repair_progress.env`: `PHASE`, `STATUS`, `PERCENT`, `RECORDS_TOTAL`, `RECORDS_PROCESSED`, `RECORDS_FIXED`, `RECORDS_STILL_FAILED`, `STRATEGY`
4. Consultar DB: `SELECT * FROM opportunities WHERE detail_status='failed'`
5. Por cada registro: re-descargar con Playwright, reintentar con backoff, actualizar DB
6. Al finalizar: escribir `repair_last_summary.env`
7. Limpiar flags

**Integración:**
```bash
125-run-priority.sh repair 90 repair -- src/20_pipeline/060b-repair-failed.sh
```

### ⚡ F12 — Health Check Mode (System Review)

**Objetivo:** Revisión integral del sistema — 19 verificaciones.

**Estado actual:**
- `140-full-report.py` existe pero es read-only, Markdown, no interactivo ⬜

**Archivos nuevos:**

| Archivo | Propósito |
|---|---|
| `src/50_tools/150-health-check.py` | 19 verificaciones del sistema |
| `src/50_tools/150b-health-check.sh` | Wrapper shell: lock + flags + invocación |

**Verificaciones:**

| # | Check | Qué revisa | Criterio fallo |
|---|---|---|---|
| HC1 | DB Integrity | `PRAGMA integrity_check` | Cualquier error |
| HC2 | DB Orphan records | `record_folder` sin carpeta en disco | > 0 |
| HC3 | DB Orphan folders | Carpeta sin registro en DB | > 0 |
| HC4 | Failed records | `COUNT WHERE detail_status='failed'` | > 0 (warning) |
| HC5 | Missing detail.json | Carpetas sin detail.json | > 0 |
| HC6 | Missing .ics | `notified_at` sin .ics en carpeta | > 0 |
| HC7 | systemd services | `systemctl is-active` | No active |
| HC8 | systemd timers | `systemctl list-timers` | No listed |
| HC9 | Docker containers | `docker compose ps` | Not Up |
| HC10 | Portal connectivity | HTTPS `panamacompra.gob.pa` | No responde |
| HC11 | Webhook listener | Puerto 8765 escuchando | No open |
| HC12 | Webhook token | `.webhook_token` presente | Missing |
| HC13 | Web monitor | Puerto Flask escuchando | No open |
| HC14 | WAHA status | Sesión WAHA connected | Not connected |
| HC15 | Disk space | `df` en partición data | < 1GB |
| HC16 | Disk inodes | `df -i` en partición data | < 1000 |
| HC17 | Config files | `monitor_settings.env`, tokens, chat_id | Missing |
| HC18 | Git status | `git status --short` | Uncommitted changes |
| HC19 | Recent errors | `grep -i error/failed/traceback` en logs | Cualquier match |

**State file:** `health_result.env` — `OVERALL_STATUS` (PASS/WARN/FAIL), `CHECKS_TOTAL`, `CHECKS_PASSED`, `CHECKS_WARNED`, `CHECKS_FAILED`, más campos por check (`CHECK_XX_NAME`, `CHECK_XX_STATUS`, `CHECK_XX_DETAIL`).

**Integración:**
```bash
125-run-priority.sh health 85 health -- src/50_tools/150b-health-check.sh
```

---

## 6. Dependencias entre Fases (F1–F12)

```
F1 (Run Controls Tk) ──► F2 (Progress bar Tk) ──► F3 (Process chips Tk)
                                                       │
                                                       ▼
                                              F7 (KPI diagrams Tk)
                                                       │
              ┌────────────────────────────────────────┘
              ▼                                   
      F4 (Web → Tk parity) ◄──► F5 (Tk → Web parity)
              │
      ┌───────┴───────┐
      ▼               ▼
F6 (Terminal)   F8 (Timer buttons) ──► F10 (Auto-minimize + CLI)
      │               │
      ▼               ▼
F11 (Repair Mode)  F9 (Update loader result)
      │
      ▼
F12 (Health Check Mode)
```

- F1–F3: independientes, paralelizables
- F4: requiere F3 completo
- F5: independiente de F4, paralelizable
- F7: requiere F3 (datos de ubicación de chips)
- F6: independiente de F7, requiere F4 (progressive enrichment)
- F8: requiere F4 (lógica de flags en monitor_common.py)
- F9: independiente
- F10: requiere F8
- F11: requiere F6 (botones terminal + flags comunes)
- F12: requiere F11 (infraestructura de flags + state env)

---

## 7. Resumen de Gaps por Monitor

| Monitor | Gaps |
|---|---|
| **M1 Tk** (3,292 lines) | stop_no_resume badge, dev_mode badge, KPI diagrams, closed_crawl_state, WAHA QR warning, step X/7 text, timing KPIs, **⚡ repair button+progress, ⚡ health button+result** |
| **M2 Web** (~6,191 lines) | stop_no_resume badge, WAHA QR warning, **⚡ repair progress bar, ⚡ health result card** |
| **M3 Terminal** (543 lines) | progress env, last_summary, dev_mode, stop_no_resume, Cerradas buttons, WAHA QR, **⚡ repair, ⚡ health** |
| **M4 Timer Tk** (932 lines) | flags queue, dev_mode, progress env, action buttons, WAHA QR |
| **M5 Timer CLI** (235 lines) | TODO: flags, progress, summary, dev_mode, auto-minimize, WAHA QR |
| **M6 Loader** (150 lines) | Result screen, WAHA QR |
| **M7 Launcher** (373 lines) | (orquestador, sin UI — sin gaps) |

---

## 8. Archivos Nuevos Propuestos

| Archivo | Modo | Propósito |
|---|---|---|
| `src/20_pipeline/060-repair-failed.py` | REPAIR | Re-descarga registros failed con reintentos |
| `src/20_pipeline/060b-repair-failed.sh` | REPAIR | Wrapper shell con lock + flags |
| `src/50_tools/150-health-check.py` | HEALTH | 19 verificaciones del sistema |
| `src/50_tools/150b-health-check.sh` | HEALTH | Wrapper shell con lock + flags |