# Auditoría Completa: Sistema de Monitoreo — PanamaCompra Collector

> Generado: 2026-07-28 · Actualizado: 2026-07-31
> Rama: `agent/priority-run-queue`
> Repo: `panamacompra-collector`

> **Nota de vigencia:** las secciones 1–8 documentan el estado al 2026-07-28. Desde entonces se implementó Priority 4 (cola de tareas), se corrigieron dos bugs, y el diseño propuesto para F11/F12 se revisó con datos reales de producción. Ver **§9 Cambios desde 2026-07-28** para el detalle — las secciones §4 (matriz), §5 F11/F12 y §6 (dependencias) ya incorporan esos cambios inline.

---

## 1. Inventario de Monitores (7 archivos)

| # | Archivo | Tipo | Líneas | Propósito |
|---|---|---|---|---|
| M1 | `src/40_monitor/001a-monitor-tk.py` | Tkinter GUI | 3,292 | Monitor principal con barra de progreso, chips, KPIs, calendario |
| M2 | `src/40_monitor/001b-monitor-web.py` | Web (Flask) | ~6,300 (2026-07-31, +task queue UI) | Monitor web con dashboard completo, KPI diagramas, settings |
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
| ✅ `task_queue.json` | `$PC_QUEUE_DIR/task_queue.json` | **IMPLEMENTADO 2026-07-30** — Cola FIFO de tareas condicionadas a estado de DB (no solo lock libre). Ver `145-task-queue.py`. Ticked desde `039-run-closed-backfill.sh` cada 20 min. Solo M2 lo expone (list/add/remove vía `/api/task-queue*`). |
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
| `stop_no_resume` badge | N/A — descartado, ver F1 | N/A | N/A | N/A | N/A | N/A |
| `update_requested` badge | ✅ | ✅ | ✅ texto | ✅ texto | ❌ | ❌ |
| `update_in_progress` badge | ✅ | ✅ | ✅ texto | ✅ texto | ❌ | ❌ |
| `dev_mode_active` badge | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **⚡** `repair_requested/in_progress` | ❌ | ✅ (flags only, no badge yet) | ❌ | ❌ | ❌ | ❌ |
| **⚡** `health_requested/in_progress` | **⚡** | **⚡** | **⚡** | ❌ | ❌ | ❌ |
| **Progress en vivo** | | | | | | |
| `run_all_progress` barra+texto | ✅ | ✅ | ❌ | ⬜ texto | ❌ | ❌ |
| ETA | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Contadores records | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Step X/7 actual/total | ⬜ barra | ✅ texto | ❌ | ❌ | ❌ | ❌ |
| **⚡** `repair_progress` barra | ❌ | ✅ (texto plano, no barra visual) | ❌ | ❌ | ❌ | ❌ |
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
| Process pills Cerradas | ❌ | ✅ (4 pills, incl. priority 4) | ❌ | ❌ | ❌ | ❌ |
| ✅ Task queue (priority 4) — list/add/cancel | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Health Check** | | | | | | |
| **⚡** `health_result` display | **⚡** | **⚡** | **⚡** | ❌ | ❌ | ❌ |
| **⚡** Botón Health Check | **⚡** | **⚡** | **⚡** | ❌ | ❌ | ❌ |
| **WAHA** | | | | | | |
| `waha_qr_required` warning | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Estado sesión WAHA | ✅ icono | ✅ badge | ❌ | ❌ | ❌ | ❌ |
| **Run Controls** | | | | | | |
| Run All | ✅ | ✅ | ✅ menú | ❌ | ❌ | ❌ |
| Stop | ✅ | ✅ | ✅ menú | ❌ | ❌ | ❌ |
| Stop No Resume | N/A — descartado, ver F1 | N/A | N/A | N/A | N/A | N/A |
| Dev Pause/Resume | ✅ (desde antes de esta auditoría, commit 4e707bf) | ✅ | ❌ | ❌ | ❌ | ❌ |
| **⚡** Repair Failed | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
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

### F1 — Run Controls en Tk — ❌ DESCARTADO 2026-07-31 (no era un gap real)

Los tres puntos originales, verificados contra el código actual:

1. **Botón "Dev Pause/Resume"** — ya existía en M1 desde el commit `4e707bf` (2026-07-08), **tres semanas antes** de que se escribiera esta auditoría. Error de la auditoría desde el día uno, no una regresión.
2. **Badge de `run_all_stop_no_resume.flag`** — la bandera es autolimpiante: `120a-stop-everything.sh` y `120b-stop-collectors.sh` la crean (`touch`), esperan ~2-3s mientras hacen `pkill`/`sleep`, y la borran ellos mismos antes de salir. Es una señal interna para el exit trap del worker ("no reanudes"), no un estado persistente. Un badge sondeado cada pocos segundos casi nunca la vería en `true` — daría una falsa sensación de cobertura sin valor real observable.
3. **Botón "Stop No Resume" separado** — sería redundante: el propio comentario de `120b-stop-collectors.sh` dice que "Stop active run" (el botón que YA existe en M1 y M2) "detiene la ejecución actual de inmediato **y evita una reanudación automática**". No existe en el código ningún "stop" que SÍ permita reanudación automática para contrastar — eso solo pasa tras un crash no planeado (`mark_abrupt_exit_for_resume`), nunca como una variante de botón manual. Un segundo botón haría exactamente lo mismo que el primero.

Además, la atribución de la tabla §2A de `110a-request-run.sh:14` como "creador" de la bandera es incorrecta: esa línea solo la **borra** (para que una solicitud de ejecución nueva no quede bloqueada por una marca de "no resume" obsoleta).

**Conclusión:** no hay gap real que llenar aquí. Ver la matriz de §4 y el resumen de §7, corregidos.

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

### ✅ F11 — Repair Mode (Fail Recovery) — IMPLEMENTADO 2026-07-31

> **Diseño revisado 2026-07-31**, con datos reales: se ejecutó un repair manual completo de los 347 registros `detail_status='failed'` acumulados (`/tmp/repair_failed_details.py` + `/tmp/diagnose_failed.py`, no comiteado). Resultado: **347/347 recuperados (100%)**, y **cero** requirió rotación de user-agent — todas las fallas eran conexiones Playwright/Firefox trabadas (`WatchdogTimeout`), resueltas por completo con un reintento simple sobre un browser recién lanzado. Esto invalida la estrategia propuesta originalmente abajo (tachada) y confirma la de reemplazo.

**Objetivo:** Recuperar registros con `detail_status='failed'` re-descargándolos con reintentos.

**Estado actual:**
- `125-run-priority.sh` ya reconoce `source=repair` → `run_type=REPAIR` (priority 90) ✅
- `050-repair-missing-deadlines.py` cubre deadlines, no failed general ⬜
- `common.run_with_watchdog()` + relanzar browser al expirar ya existe y está probado en producción en `030-collect-details.py`, `037b-collect-closed-details.py` y `038-collect-cotizaciones.py` ✅ — F11 debe **reusar este patrón**, no reinventar uno.
- El script ad-hoc de repair necesitó su propio `wait_for_priorities_1_and_2()` (polling de flock) porque nada genérico se lo da — **requisito no negociable** para F11: debe deferir a priority 1/2 con el mismo patrón `flock -n LOCK true` (check-don't-kill) que ya usan priority 3 y 4, no solo confiar en el número de prioridad de `125-run-priority.sh` (que no entiende deferencia entre locks de distintos procesos).

**Archivos nuevos:**

| Archivo | Propósito |
|---|---|
| `src/20_pipeline/060-repair-failed.py` | Re-descarga registros failed reutilizando `run_with_watchdog` + relanzo de browser (mismo patrón que 030/037b/038); ~~rotar UA, backoff 3s/10s/30s~~ — descartado, no hubo un solo caso real que lo necesitara |
| `src/20_pipeline/060b-repair-failed.sh` | Wrapper shell: lock + flags + invocación + deferencia flock a priority 1/2 (igual que `039-run-closed-backfill.sh`) |

**Pipeline Repair:**
1. Adquirir `/tmp/panamacompra_repair_worker.lock`
2. Verificar flock de priority 1 y 2 libres (check-don't-kill); si ocupados, salir sin marcar fallo — se reintenta en el próximo tick del timer
3. Crear `repair_requested.flag` + `repair_in_progress.flag`
4. Escribir `repair_progress.env`: `PHASE`, `STATUS`, `PERCENT`, `RECORDS_TOTAL`, `RECORDS_PROCESSED`, `RECORDS_FIXED`, `RECORDS_STILL_FAILED`
5. Consultar DB: `SELECT * FROM opportunities WHERE detail_status='failed'`
6. Un solo browser Playwright para todo el lote; por registro: `run_with_watchdog(process_detail)`, al expirar cerrar+relanzar browser y continuar (no reintento en el mismo registro más allá de eso — el 100% de los casos reales se resolvió con un solo reintento sobre browser fresco)
7. Al finalizar: escribir `repair_last_summary.env`
8. Limpiar flags

**Integración:**
```bash
125-run-priority.sh repair 90 repair -- src/20_pipeline/060b-repair-failed.sh
```

**Implementado 2026-07-31**, tal como quedó especificado arriba: `060-repair-failed.py` + `060b-repair-failed.sh` (deferencia flock a priority 1/2/3), disparado desde un nuevo botón "Repair failed records" (zona Settings, `MANUAL_ACTIONS`) — reutiliza `125-run-priority.sh repair 90` sin necesitar tocar ese script. Tarjeta de progreso nueva en Operations (`repair_progress.env` vía `/api/repair-status`). No se agregó `repair_requested.flag`/`repair_in_progress.flag` como badges en M1/M3 todavía — verificado extremo a extremo solo el camino "sin registros fallidos" (0 candidatos en producción al momento de implementar); falta una verificación real con un lote de registros `failed` genuino cuando vuelva a haber alguno.

### ⚡ F12 — Health Check Mode (System Review)

**Objetivo:** Revisión integral del sistema — 21 verificaciones (19 originales + HC20/HC21, ver revisión 2026-07-31 abajo).

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
| HC18 | Git stash accumulation | ~~`git status --short` (cualquier diff)~~ → `git stash list \| wc -l` | > 5 stashes |
| HC19 | Recent errors | `grep -i error/failed/traceback` en logs | Cualquier match |
| ✅ HC20 | Task queue estancada | `task_queue.json`: tarea `running` con `started_at` > 24h y sin avanzar | Estancada > 24h |
| ✅ HC21 | Snapshot grupos inesperados | `index_snapshot_result.env` / log de `015-import-index-snapshot.py`: `skipped_unexpected_group` | > 0 |

> **HC18 revisado 2026-07-31:** el diseño original ("cualquier diff sin commitear = FAIL") habría generado falsos positivos constantes — el propio incidente de 258 stashes acumulados (root-caused y corregido esta semana, ver §9) demostró que el árbol de trabajo puede mostrar diffs transitorios legítimos como parte de la operación normal (`000-update-before-run.sh` autostash). La señal real que sí detectó el incidente fue la **acumulación** de stashes, no la existencia de un diff puntual.
>
> **HC20/HC21 nuevos:** cubren los dos subsistemas agregados después de la auditoría original (Priority 4 y grupo Cancelled) — ninguno de los 19 checks originales podía haberlos anticipado. Sin HC20, una tarea de cola trabada (rango de fechas inválido, sin datos en el portal, bug) bloquearía la cola entera sin que nada lo reporte, salvo revisar `task_queue.json` a mano. Sin HC21, filas Cerradas/Canceladas se descartan en silencio si el crawler introdujera algún día un grupo inesperado.

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
- F11: **revisado 2026-07-31** — su dependencia real no es F6 sino el patrón de deferencia flock ya probado en priority 3/4 (`flock -n LOCK true`, check-don't-kill); F6 (botones terminal) solo hace falta para la *exposición* en M3, no para la lógica de repair en sí, que ya se validó de punta a punta sin ningún botón de monitor
- F12: requiere F11 (infraestructura de flags + state env); además ahora depende de que Priority 4 (task queue) y el grupo Cancelled ya existan, por HC20/HC21

---

## 7. Resumen de Gaps por Monitor

| Monitor | Gaps |
|---|---|
| **M1 Tk** (3,292 lines) | ~~stop_no_resume badge~~ (descartado, F1), dev_mode badge, KPI diagrams, closed_crawl_state, WAHA QR warning, step X/7 text, timing KPIs, **⚡ repair button+progress (M2 ya lo tiene, M1 no)**, **⚡ health button+result** |
| **M2 Web** (~6,300 lines) | ~~stop_no_resume badge~~ (descartado, F1), WAHA QR warning, **✅ repair status card (implementado)**, **⚡ health result card** |
| **M3 Terminal** (543 lines) | progress env, last_summary, dev_mode, ~~stop_no_resume~~ (descartado, F1), Cerradas buttons, WAHA QR, **⚡ repair, ⚡ health** |
| **M4 Timer Tk** (932 lines) | flags queue, dev_mode, progress env, action buttons, WAHA QR |
| **M5 Timer CLI** (235 lines) | TODO: flags, progress, summary, dev_mode, auto-minimize, WAHA QR |
| **M6 Loader** (150 lines) | Result screen, WAHA QR |
| **M7 Launcher** (373 lines) | (orquestador, sin UI — sin gaps) |

---

## 8. Archivos Nuevos Propuestos

| Archivo | Modo | Propósito |
|---|---|---|
| `src/20_pipeline/060-repair-failed.py` | REPAIR | Re-descarga registros failed reutilizando `run_with_watchdog` (ver F11 revisado) |
| `src/20_pipeline/060b-repair-failed.sh` | REPAIR | Wrapper shell con lock + flags + deferencia flock a priority 1/2 |
| `src/50_tools/150-health-check.py` | HEALTH | 21 verificaciones del sistema (ver F12 revisado) |
| `src/50_tools/150b-health-check.sh` | HEALTH | Wrapper shell con lock + flags |

---

## 9. Cambios desde 2026-07-28

Todo lo siguiente se implementó y verificó en producción **después** de generada esta auditoría; las secciones 1–8 se actualizaron inline donde correspondía, pero se resume acá para trazabilidad.

### 9A. Funcionalidad nueva
- **Status tracking correcto:** `status_flag`/`status_updated_at`/`closed_at` + tabla `opportunity_status_history`, con `reconcile_legacy_status_history()` para backfill de 3,264 registros existentes. Corrige el bug original de "Programadas/Abiertas no cambian a cerrado".
- **Priority 2 ahora cubre Cancelled, no solo Closed:** `037-collect-closed-index.py` itera `ALL_GROUPS` (Closed + Cancelled); `037b`/`038` procesan `grupo IN ('Closed','Cancelled')` desde la misma cola compartida; script de changedetection actualizado con el segundo radio button.
- **Snapshot importer con guardia de grupo:** `015-import-index-snapshot.py` ahora descarta (y cuenta en `skipped_unexpected_group`) cualquier fila con `grupo` fuera de los esperados, en vez de insertarla con datos potencialmente incorrectos.
- **Watchdog contra cuelgues de browser:** `common.run_with_watchdog()` (SIGALRM) + relanzo de browser, aplicado a `030-collect-details.py`, `037b-collect-closed-details.py` y `038-collect-cotizaciones.py`. Motivado por un cuelgue real de producción de 4+ horas.
- **Priority 4 — cola de tareas persistente:** `145-task-queue.py` + integración en `039-run-closed-backfill.sh` + UI completa en M2 (`/api/task-queue*`, tarjeta "Task queue (priority 4)"). Permite encolar trabajo condicionado a estado de DB (ej. backfill Feb–May 2026 una vez drenado el backlog de pendientes), no solo a que un lock esté libre.
- **Fix de raíz del ciclo de 258 stashes:** 92 archivos `.sh`/`.py` + `bin/pcc` estaban commiteados sin bit ejecutable, causando que `000-update-before-run.sh` generara un diff de chmod en cada ciclo, barrido por el autostash. Corregido commiteando el modo correcto (0 diff verificado).
- **Reorganización de docs:** `docs/audits/` con los 3 reportes de auditoría existentes.

### 9B. Bugs encontrados y corregidos durante esta revisión (2026-07-31)
Al comparar la UI del monitor contra esta misma auditoría, salieron dos bugs reales introducidos por el trabajo de Cancelled/Priority-4 (9A), no cubiertos por los 19 health checks originales — de ahí HC21 arriba:
1. **`145-task-queue.py` `check_wait_condition`** solo contaba `grupo='Closed'` pendientes, ignorando Cancelled — pese a que ambos comparten la misma cola de `037b`/`038`. Un backfill podía activarse mientras aún había Cancelled pendientes, compitiendo por la misma cola que la propia feature dice evitar. **Corregido:** `grupo IN ('Closed','Cancelled')`.
2. **`001b-monitor-web.py` `/api/closed-status`** (`total` y `cotizacion_by_status`) filtraba solo `grupo='Closed'`, subreportando en silencio el total real y ocultando el estado de cotización de Cancelled. **Corregido:** mismo `IN ('Closed','Cancelled')`, y la etiqueta de UI pasó de "Closed records in DB" a "Closed + Cancelled records in DB".

### 9C. Deuda conocida, aún sin resolver
- Cola de tareas: sin botón para cancelar una tarea en estado `running` (solo `queued`); sin detección de estancamiento (cubierto ahora por HC20 propuesto, no implementado); el rango de fechas que escribe una tarea activada comparte las mismas claves `PC_CLOSED_BACKFILL_START_DATE/END_DATE` que el campo manual de la misma tarjeta, y puede sobreescribirlo sin aviso.
- 46 stashes históricos con contenido real (2026-07-07 a 2026-07-28) sin revisar — dejados a criterio del usuario.
- 7 registros Abiertas/Programadas con `status_flag='cerrada'` residual no detectados por `reconcile_legacy_status_history()` (caso borde: `last_notified_status` ya igual a `estado`).
- Backfill Feb–May 2026 aún no arrancó: la tarea en cola espera correctamente a que drene el backlog de pendientes (~9,600 al último chequeo).